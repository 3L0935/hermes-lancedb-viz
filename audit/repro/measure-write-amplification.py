#!/usr/bin/env python3
"""Measure LanceDB read amplification and bounded cleanup on a synthetic /tmp DB."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugin.store import LanceDBStore
from server.maintenance import (
    collect_health_diagnostics,
    compact_lancedb,
    compaction_plan,
)


def fake_embed(_self, text: str) -> np.ndarray:
    vector = np.zeros(768, dtype=np.float32)
    for token in {part.lower().strip(".,:=") for part in text.split()}:
        vector[hash(token) % 768] += 1.0
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


def snapshot(db_path: Path) -> dict[str, int]:
    table_path = db_path / "memories.lance"
    indices_path = table_path / "_indices"
    return {
        "database_bytes": sum(
            item.stat().st_size for item in db_path.rglob("*") if item.is_file()
        ),
        "memory_versions": len(list((table_path / "_versions").glob("*.manifest"))),
        "memory_fragments": len(list((table_path / "data").glob("*.lance"))),
        "memory_index_directories": (
            len([item for item in indices_path.iterdir() if item.is_dir()])
            if indices_path.is_dir() else 0
        ),
    }


def run_measurement() -> dict:
    import lancedb

    with tempfile.TemporaryDirectory(prefix="lancedb-write-amplification-", dir="/tmp") as tmp:
        root = Path(tmp)
        db_path = root / "lancedb"
        backups_path = root / "backups"
        original_embed = LanceDBStore._embed
        LanceDBStore._embed = fake_embed
        try:
            store = LanceDBStore(db_path)
            memory_id = store.add(
                "Project:SyntheticAudit state=active marker=baseline [Tier=2]",
                category="project",
                legacy=True,
            )
            with store.write_batch():
                new_memory_id = store.add(
                    "Project:FreshIndex lexeme=ultrararetoken [Tier=2]",
                    category="project",
                    legacy=True,
                )
                before_refresh_rows = store._table.search(
                    "ultrararetoken", query_type="fts"
                ).limit(10).to_list()
            after_refresh_rows = store._table.search(
                "ultrararetoken", query_type="fts"
            ).limit(10).to_list()
            for index in range(7):
                store.update_tags(memory_id, [f"tag-{index}"])
            store._ensure_edges_table()
            store._ensure_conflicts_table()
            store._ensure_conflicts_archive_table()

            before_reads = snapshot(db_path)
            provenance = {
                "engine": f"lancedb=={getattr(lancedb, '__version__', 'unknown')}",
                "versions": {"memories": int(store._table.version)},
                "rows": {"memories": int(store._table.count_rows())},
            }
            for _ in range(6):
                reader = LanceDBStore(db_path)
                reader._embed = fake_embed.__get__(reader, LanceDBStore)
                reader.search("SyntheticAudit baseline", mode="lexical")
            after_reads = snapshot(db_path)

            health = collect_health_diagnostics(
                db_path,
                store._db,
                pipeline={"model": "synthetic", "dimension": 768, "version": 1},
                ollama_probe=lambda: {"state": "not_used"},
            )
            plan = compaction_plan(
                db_path,
                backups_path,
                estimated_after_bytes=health["maintenance_estimate"]["estimated_after_bytes"],
                diagnostics=health,
            )
            compacted = compact_lancedb(db_path, backups_path)
            after_compaction = snapshot(db_path)
        finally:
            LanceDBStore._embed = original_embed

        return {
            "temporary_database": True,
            "provenance": provenance,
            "bm25_recall": {
                "new_memory_id": new_memory_id,
                "before_writer_refresh": {
                    "found": new_memory_id in [row["id"] for row in before_refresh_rows],
                    "score": next(
                        (float(row["_score"]) for row in before_refresh_rows
                         if row["id"] == new_memory_id),
                        None,
                    ),
                },
                "after_writer_refresh": {
                    "found": new_memory_id in [row["id"] for row in after_refresh_rows],
                    "score": next(
                        (float(row["_score"]) for row in after_refresh_rows
                         if row["id"] == new_memory_id),
                        None,
                    ),
                },
            },
            "before_reads": before_reads,
            "after_reads": after_reads,
            "read_delta": {
                key: after_reads[key] - before_reads[key] for key in before_reads
            },
            "plan": {
                "recommended": plan["recommended"],
                "trigger_reasons": plan["trigger_reasons"],
            },
            "compaction": compacted,
            "after_compaction": after_compaction,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_measurement()
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)
    return 0 if report["compaction"].get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
