#!/usr/bin/env python3
"""Deterministic local retrieval benchmark; live LanceDB is read-only.

Question files contain public metadata only. Private memory content is copied
to an explicitly temporary LanceDB fixture and never serialized in results.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable


QUESTION_CATEGORIES = {
    "project_name", "id", "exact_path", "config", "paraphrase",
    "dependency", "old_critical_correction", "contradiction", "no_answer",
}
TABLES = (
    "memories", "memory_edges", "memory_conflicts", "memory_conflicts_archive",
)
VARIANTS = ("lexical", "current_hybrid", "corrected_hybrid", "corrected_one_hop")


def load_question_set(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError(f"{path}: unsupported schema_version")
    split = data.get("split")
    if split not in {"calibration", "final"}:
        raise ValueError(f"{path}: split must be calibration or final")
    if split == "final" and data.get("frozen") is not True:
        raise ValueError(f"{path}: final split must be frozen")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"{path}: questions must be a non-empty array")
    seen = set()
    for question in questions:
        required = {
            "id", "language", "category", "query", "expected_ids",
            "avoid_ids", "expect_abstain",
        }
        if set(question) != required:
            raise ValueError(f"{path}: invalid keys for {question.get('id')}")
        if question["id"] in seen:
            raise ValueError(f"{path}: duplicate question id {question['id']}")
        seen.add(question["id"])
        if question["language"] not in {"fr", "en"}:
            raise ValueError(f"{path}: invalid language for {question['id']}")
        if question["category"] not in QUESTION_CATEGORIES:
            raise ValueError(f"{path}: invalid category for {question['id']}")
        if bool(question["expected_ids"]) == bool(question["expect_abstain"]):
            raise ValueError(f"{path}: expected_ids/expect_abstain disagree for {question['id']}")
    return data


def _is_tmp_path(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    return resolved != Path("/tmp") and Path("/tmp") in resolved.parents


def copy_read_only_fixture(
    source_path: Path,
    fixture_path: Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Copy source tables into /tmp without constructing a store on source."""
    import lancedb

    source_path = source_path.expanduser().resolve()
    fixture_path = fixture_path.expanduser().resolve()
    if not _is_tmp_path(fixture_path):
        raise ValueError("fixture path must be a dedicated directory below /tmp")
    if fixture_path == source_path:
        raise ValueError("fixture path must differ from source path")
    if fixture_path.exists():
        if not replace:
            raise FileExistsError(f"fixture already exists: {fixture_path}")
        shutil.rmtree(fixture_path)
    fixture_path.mkdir(parents=True)

    source = lancedb.connect(
        str(source_path), read_consistency_interval=timedelta(seconds=0)
    )
    destination = lancedb.connect(str(fixture_path))
    listed = source.list_tables()
    available = set(getattr(listed, "tables", listed))
    versions = {}
    rows = {}
    for table_name in TABLES:
        if table_name not in available:
            continue
        table = source.open_table(table_name)
        version_before = table.version
        arrow = table.to_arrow()
        versions[table_name] = version_before
        rows[table_name] = arrow.num_rows
        if arrow.num_rows:
            destination.create_table(table_name, data=arrow)
        else:
            destination.create_table(table_name, schema=table.schema)
        latest_version = source.open_table(table_name).version
        if latest_version != version_before:
            raise RuntimeError(
                f"source table {table_name} changed during copy: "
                f"{version_before} -> {latest_version}"
            )
    return {
        "fixture_path": str(fixture_path),
        "source_versions": versions,
        "rows_by_table": rows,
    }


def compute_metrics(
    questions: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {row["question_id"]: row for row in observations}
    recalls = []
    reciprocal_ranks = []
    false_results = []
    avoided = []
    context_chars = []
    result_counts = []
    for question in questions:
        row = by_id[question["id"]]
        result_ids = list(row["result_ids"][:5])
        context_chars.append(int(row["context_chars"]))
        result_counts.append(len(row["result_ids"]))
        if question["expect_abstain"]:
            false_results.append(bool(result_ids))
            continue
        expected = set(question["expected_ids"])
        recalls.append(len(expected.intersection(result_ids)) / len(expected))
        ranks = [result_ids.index(item) + 1 for item in expected if item in result_ids]
        reciprocal_ranks.append(1.0 / min(ranks) if ranks else 0.0)
        avoided.append(bool(set(question["avoid_ids"]).intersection(result_ids)))
    embedding_ms = sum(float(row["embedding_ms"]) for row in observations)
    search_ms = sum(float(row["search_ms"]) for row in observations)
    return {
        "question_count": len(questions),
        "answerable_count": len(recalls),
        "no_answer_count": len(false_results),
        "recall_at_5": round(statistics.fmean(recalls), 6) if recalls else 0.0,
        "mrr": round(statistics.fmean(reciprocal_ranks), 6) if reciprocal_ranks else 0.0,
        "no_answer_false_result_rate": round(statistics.fmean(false_results), 6) if false_results else 0.0,
        "avoid_hit_rate": round(statistics.fmean(avoided), 6) if avoided else 0.0,
        "mean_context_chars": round(statistics.fmean(context_chars), 3) if context_chars else 0.0,
        "mean_result_count": round(statistics.fmean(result_counts), 3) if result_counts else 0.0,
        "embedding_calls": sum(int(row["embedding_calls"]) for row in observations),
        "embedding_ms": round(embedding_ms, 3),
        "search_ms": round(search_ms, 3),
        "mean_search_ms": round(search_ms / len(observations), 3) if observations else 0.0,
    }


def _current_hybrid(store, query: str, top_k: int) -> list[dict[str, Any]]:
    """Frozen pre-C2 behavior: RRF ranking with the ineffective 0.005 gate."""
    vector = store._require_embedding(query, field="query")
    rows = (
        store._table.search(query_type="hybrid")
        .text(query)
        .vector(vector.tolist())
        .limit(top_k)
        .to_list()
    )
    results = []
    for row in rows:
        score = float(row.get("_relevance_score", 1.0 - row.get("_distance", 0.0)))
        if score >= 0.005:
            results.append({"id": row["id"], "content": row.get("content", ""), "score": score})
    return results


def _variant_runner(store, variant: str) -> Callable[[str, int], list[dict[str, Any]]]:
    if variant == "lexical":
        return lambda query, top_k: store._search_lexical(query, top_k, None)
    if variant == "current_hybrid":
        return lambda query, top_k: _current_hybrid(store, query, top_k)
    if variant == "corrected_hybrid":
        return lambda query, top_k: store.search(
            query, top_k=top_k, mode="hybrid", relation_depth=0
        )
    if variant == "corrected_one_hop":
        return lambda query, top_k: store.search(
            query, top_k=top_k, mode="graph", relation_depth=1
        )
    raise ValueError(f"unknown variant: {variant}")


def evaluate_variant(store, questions: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    runner = _variant_runner(store, variant)
    observations = []
    for question in questions:
        embedding_calls = 0
        embedding_seconds = 0.0
        original_embed = store._embed

        def measured_embed(text: str):
            nonlocal embedding_calls, embedding_seconds
            started = time.perf_counter()
            try:
                return original_embed(text)
            finally:
                embedding_calls += 1
                embedding_seconds += time.perf_counter() - started

        store._embed = measured_embed
        started = time.perf_counter()
        try:
            results = runner(question["query"], 5)
            error = ""
        except Exception as exc:  # benchmark must record failures, not hide them
            results = []
            error = f"{type(exc).__name__}: {exc}"
        finally:
            elapsed = time.perf_counter() - started
            store._embed = original_embed
        observations.append({
            "question_id": question["id"],
            "result_ids": [str(row.get("id", "")) for row in results],
            "context_chars": sum(len(str(row.get("content", ""))) for row in results),
            "embedding_calls": embedding_calls,
            "embedding_ms": round(embedding_seconds * 1000, 3),
            "search_ms": round(elapsed * 1000, 3),
            "error": error,
        })
    return {"metrics": compute_metrics(questions, observations), "observations": observations}


def promotion_decision(results: dict[str, Any], final_dataset: dict[str, Any]) -> dict[str, Any]:
    """Apply the frozen, auditable gate to final-split metrics only."""
    variants = results["splits"]["final"]
    current = variants["current_hybrid"]
    corrected = variants["corrected_hybrid"]
    one_hop = variants["corrected_one_hop"]

    critical_ids = {
        question["id"]: set(question["expected_ids"])
        for question in final_dataset["questions"]
        if question["category"] == "old_critical_correction"
    }

    def critical_hits(variant: dict[str, Any]) -> dict[str, bool]:
        observations = {
            row["question_id"]: set(row["result_ids"][:5])
            for row in variant["observations"]
        }
        return {
            question_id: bool(expected.intersection(observations.get(question_id, set())))
            for question_id, expected in critical_ids.items()
        }

    current_critical = critical_hits(current)
    corrected_critical = critical_hits(corrected)
    no_critical_regression = all(
        not was_found or corrected_critical.get(question_id, False)
        for question_id, was_found in current_critical.items()
    )
    cm = current["metrics"]
    hm = corrected["metrics"]
    om = one_hop["metrics"]
    recall_delta = hm["recall_at_5"] - cm["recall_at_5"]
    hybrid_promoted = bool(
        hm["no_answer_false_result_rate"] < cm["no_answer_false_result_rate"]
        and hm["mrr"] >= cm["mrr"]
        and recall_delta >= -0.05
        and no_critical_regression
    )
    one_hop_promoted = bool(
        om["no_answer_false_result_rate"] <= hm["no_answer_false_result_rate"]
        and (
            om["recall_at_5"] > hm["recall_at_5"]
            or om["mrr"] > hm["mrr"]
        )
    )
    return {
        "basis": "frozen final split only",
        "corrected_hybrid": {
            "promoted": hybrid_promoted,
            "recall_at_5_delta": round(recall_delta, 6),
            "mrr_delta": round(hm["mrr"] - cm["mrr"], 6),
            "no_answer_false_result_rate_delta": round(
                hm["no_answer_false_result_rate"] - cm["no_answer_false_result_rate"], 6
            ),
            "critical_no_regression": no_critical_regression,
            "decision": "keep corrected routing" if hybrid_promoted else "revert corrected routing",
        },
        "corrected_one_hop_as_default": {
            "promoted": one_hop_promoted,
            "decision": (
                "promote as default" if one_hop_promoted
                else "retain bounded typed route without expanding default routing"
            ),
        },
    }


def run_benchmark(fixture_path: Path, datasets: list[dict[str, Any]]) -> dict[str, Any]:
    if not _is_tmp_path(fixture_path):
        raise ValueError("benchmark fixture must be below /tmp")
    from plugin.store import EMBED_MODEL, LanceDBStore
    import lancedb

    store = LanceDBStore(fixture_path)
    output = {
        "schema_version": 1,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "lancedb": getattr(lancedb, "__version__", "unknown"),
            "embedding_model": EMBED_MODEL,
            "thread_limits": {
                name: os.environ.get(name, "")
                for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
            },
        },
        "fixture": str(fixture_path.resolve()),
        "splits": {},
    }
    for dataset in datasets:
        output["splits"][dataset["split"]] = {
            variant: evaluate_variant(store, dataset["questions"], variant)
            for variant in VARIANTS
        }
    final_dataset = next(dataset for dataset in datasets if dataset["split"] == "final")
    output["promotion_decision"] = promotion_decision(output, final_dataset)
    return output


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=Path("~/.hermes/lancedb"))
    parser.add_argument("--fixture", type=Path, default=Path("/tmp/hermes-retrieval-fixture"))
    parser.add_argument("--prepare-fixture", action="store_true")
    parser.add_argument("--replace-fixture", action="store_true")
    parser.add_argument("--output", type=Path, default=root / "audit/repro/retrieval-benchmark-results.json")
    args = parser.parse_args(argv)

    calibration = load_question_set(root / "audit/repro/retrieval-questions-calibration.json")
    final = load_question_set(root / "audit/repro/retrieval-questions-final.json")
    preparation = None
    if args.prepare_fixture:
        preparation = copy_read_only_fixture(
            args.source_db,
            args.fixture,
            replace=args.replace_fixture,
        )
    if not args.fixture.exists():
        parser.error("fixture does not exist; pass --prepare-fixture")
    results = run_benchmark(args.fixture, [calibration, final])
    if preparation:
        results["preparation"] = preparation
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        split: {variant: data["metrics"] for variant, data in variants.items()}
        for split, variants in results["splits"].items()
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
