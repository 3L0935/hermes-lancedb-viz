#!/usr/bin/env python3
"""Classify legacy memory formats without modifying any database."""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load_file_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_file_module(
    "hermes_memory_format_audit",
    ROOT / "scripts" / "audit-memory-format.py",
)
contract = audit.contract


def classify_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate provable formatting fixes from rows requiring human review."""
    classifications = []
    counts: Counter[str] = Counter()
    for row in rows:
        memory_id = str(row.get("id") or "")
        content = row.get("content")
        category = row.get("category")
        try:
            canonical_content = contract.canonicalize_content(
                content,
                category=category,
            )
        except contract.MemoryContractError as original_error:
            try:
                canonical_content = contract.canonicalize_unambiguous_legacy_content(
                    content,
                    category=category,
                )
            except contract.MemoryContractError:
                classification = "quarantine"
                entry = {
                    "memory_id": memory_id,
                    "classification": classification,
                    "category": category,
                    "error": original_error.to_dict(),
                }
            else:
                classification = "auto_fix"
                memory = contract.parse_content(canonical_content, category=category)
                entry = {
                    "memory_id": memory_id,
                    "classification": classification,
                    "category": category,
                    "canonical_content": canonical_content,
                    "warnings": [
                        warning.to_dict()
                        for warning in contract.contract_warnings(memory)
                    ],
                }
        else:
            memory = contract.parse_content(canonical_content, category=category)
            warnings = [
                warning.to_dict()
                for warning in contract.contract_warnings(memory)
            ]
            if content != canonical_content:
                classification = "auto_fix"
            elif warnings:
                classification = "warning_only"
            else:
                classification = "canonical"
            entry = {
                "memory_id": memory_id,
                "classification": classification,
                "category": category,
                "warnings": warnings,
            }
            if classification == "auto_fix":
                entry["canonical_content"] = canonical_content

        counts[classification] += 1
        classifications.append(entry)

    return {
        "summary": dict(sorted(counts.items())),
        "rows": classifications,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db",
        type=Path,
        default=Path.home() / ".hermes" / "lancedb",
        help="Existing LanceDB directory opened for projected reads only.",
    )
    parser.add_argument("--table", default="memories")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit no-op flag; classification is always read-only.",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = audit.read_rows(args.source_db, args.table)
        report = {
            "schema_version": 2,
            "mode": "dry_run",
            "read_only": True,
            "source_db": str(args.source_db.expanduser().resolve()),
            **classify_rows(rows),
        }
        print(json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
        ))
        return 0
    except Exception as error:
        print(json.dumps({
            "schema_version": 2,
            "mode": "dry_run",
            "read_only": True,
            "operational_error": {
                "code": "migration_plan_failed",
                "message": str(error),
            },
        }, ensure_ascii=False, indent=2 if args.pretty else None))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
