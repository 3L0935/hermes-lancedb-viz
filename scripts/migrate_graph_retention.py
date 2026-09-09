#!/usr/bin/env python3
"""Backfill relation IDs and deterministic conflicts.

Dry-run is the default. Pass --apply only after backing up the LanceDB directory.
No network or LLM call is made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugin.store import LanceDBStore, canonical_subject


def _label_key(label: str) -> str:
    return canonical_subject(label) or (label or "").strip().rstrip(":,.;").lower()


def plan_edge_resolution(memories: list[dict], edges: list[dict]) -> tuple[list[dict], dict]:
    """Resolve legacy labels conservatively and classify every edge."""
    memory_ids = {str(memory["id"]) for memory in memories}
    exact: dict[str, list[str]] = {}
    short: dict[str, list[str]] = {}
    for memory in memories:
        memory_id = str(memory["id"])
        key = canonical_subject(memory.get("content", ""))
        if not key:
            continue
        exact.setdefault(key, []).append(memory_id)
        subject = key.partition(":")[2]
        if subject:
            short.setdefault(subject, []).append(memory_id)

    summary = {"resolved": 0, "ambiguous_or_missing": 0, "orphaned_source": 0}
    planned = []
    for original in edges:
        row = dict(original)
        source_id = str(row.get("source_id") or "")
        if source_id not in memory_ids:
            row["resolution"] = "orphaned_source"
            summary["orphaned_source"] += 1
            planned.append(row)
            continue

        target_id = str(row.get("target_id") or "")
        if target_id in memory_ids:
            matches = [target_id]
        else:
            key = _label_key(str(row.get("target_label") or ""))
            matches = exact.get(key, []) if ":" in key else short.get(key, [])

        if len(matches) == 1:
            row["target_id"] = matches[0]
            row["resolution"] = "resolved"
            summary["resolved"] += 1
        else:
            row["target_id"] = ""
            row["resolution"] = "ambiguous_or_missing"
            summary["ambiguous_or_missing"] += 1
        planned.append(row)
    return planned, summary


def _sql(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def migrate(db_path: Path, apply: bool = False) -> dict:
    import lancedb

    db = lancedb.connect(str(db_path))
    memories = db.open_table("memories").to_arrow().to_pylist()
    try:
        edge_table = db.open_table("memory_edges")
        edges = edge_table.to_arrow().to_pylist()
    except Exception:
        edges = []

    planned, summary = plan_edge_resolution(memories, edges)
    result = {
        "mode": "apply" if apply else "dry-run",
        "memories": len(memories),
        "edges": len(edges),
        **summary,
        "conflicts_created_or_reopened": 0,
    }
    if not apply:
        return result

    store = LanceDBStore(db_path)
    edge_table = store._ensure_edges_table()
    for row in planned:
        source_id = str(row.get("source_id") or "")
        if row["resolution"] == "orphaned_source":
            edge_table.delete(f"source_id = {_sql(source_id)}")
            continue
        if row["resolution"] != "resolved":
            continue
        relation_type = str(row.get("relation_type") or "")
        target_label = str(row.get("target_label") or "")
        where = (
            f"source_id = {_sql(source_id)} AND "
            f"relation_type = {_sql(relation_type)} AND "
            f"target_label = {_sql(target_label)}"
        )
        edge_table.update(where, {"target_id": row["target_id"]})

    conflict_count = 0
    for memory in memories:
        conflict_count += len(store.detect_conflicts_for(str(memory["id"])))
    result["conflicts_created_or_reopened"] = conflict_count
    return result


def main() -> int:
    default_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=default_home / "lancedb")
    parser.add_argument("--apply", action="store_true", help="Write the planned migration")
    args = parser.parse_args()

    result = migrate(args.db_path.expanduser(), apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.apply:
        print("Dry-run only. Back up the database, then rerun with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
