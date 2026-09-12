#!/usr/bin/env python3
"""Read-only, machine-readable audit of the LanceDB memory contract."""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable


def _candidate_contract_paths() -> list[Path]:
    """Every place memory_contract.py may live, in priority order."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "plugin" / "memory_contract.py",
        # Deployed layout: deploy-local.sh ships the contract beside the script, so this
        # resolves without depending on any path outside the deployment.
        here.parent / "memory_contract.py",
    ]
    # Container layout: the memory plugin is reachable through the mount.
    for base in (
        Path("/home/hermes/.hermes/hermes-agent/plugins/memory/lancedb"),
        Path("/home/hermes/.hermes/plugins/lancedb"),
        Path("/app/plugin"),
    ):
        candidates.append(base / "memory_contract.py")
    # The operator's canonical plugin copy, independent of HERMES_HOME overrides.
    candidates.append(Path.home() / ".hermes" / "plugins" / "lancedb" / "memory_contract.py")
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        home = Path(env_home)
        candidates.append(home / "plugins" / "lancedb" / "memory_contract.py")
        candidates.append(home / "hermes-agent" / "plugins" / "memory" / "lancedb" / "memory_contract.py")
    return candidates


def _find_contract_path() -> Path:
    """Locate memory_contract.py wherever this script happens to be deployed.

    In the repository the script sits at scripts/ with plugin/ as a sibling. In the
    container only scripts/ is mounted, and the contract lives in the memory plugin
    tree instead, so a single hardcoded relative path raised FileNotFoundError at
    import time and took the whole Review page down with it.
    """
    candidates = _candidate_contract_paths()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "memory_contract.py not found; looked in: "
        + ", ".join(str(c) for c in candidates)
    )


CONTRACT_PATH = _find_contract_path()
CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "hermes_memory_contract", CONTRACT_PATH
)
contract = importlib.util.module_from_spec(CONTRACT_SPEC)
sys.modules[CONTRACT_SPEC.name] = contract
CONTRACT_SPEC.loader.exec_module(contract)

MemoryContractError = contract.MemoryContractError
CONTRACT_VERSION = contract.CONTRACT_VERSION
contract_warnings = contract.contract_warnings
parse_content = contract.parse_content
render_content = contract.render_content


def audit_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    findings = []
    counts: Counter[str] = Counter()
    total = 0
    canonical = 0
    drift = 0
    invalid = 0
    warning_rows = 0

    for row in rows:
        total += 1
        memory_id = str(row.get("id") or "")
        content = row.get("content")
        category = row.get("category")
        try:
            memory = parse_content(content, category=category)
            rendered = render_content(memory)
            warnings = [warning.to_dict() for warning in contract_warnings(memory)]
            if warnings:
                warning_rows += 1
                counts.update(warning["code"] for warning in warnings)
            if content == rendered:
                status = "canonical"
                canonical += 1
            else:
                status = "drift"
                drift += 1
                counts["noncanonical_content"] += 1
            if status != "canonical" or warnings:
                finding = {
                    "memory_id": memory_id,
                    "status": status,
                    "warnings": warnings,
                }
                if status == "drift":
                    finding["canonical_content"] = rendered
                findings.append(finding)
        except MemoryContractError as error:
            invalid += 1
            counts[error.issue.code] += 1
            findings.append({
                "memory_id": memory_id,
                "status": "invalid",
                "error": error.to_dict(),
            })

    return {
        "schema_version": CONTRACT_VERSION,
        "read_only": True,
        "summary": {
            "total": total,
            "canonical": canonical,
            "drift": drift,
            "invalid": invalid,
            "warning_rows": warning_rows,
        },
        "counts_by_code": dict(sorted(counts.items())),
        "findings": findings,
    }


def read_rows(db_path: Path, table_name: str = "memories") -> list[dict[str, Any]]:
    """Open an existing table and return Arrow rows without store side effects."""
    if not db_path.is_dir():
        raise FileNotFoundError(f"database directory does not exist: {db_path}")
    import lancedb

    database = lancedb.connect(str(db_path))
    tables = database.list_tables().tables
    if table_name not in tables:
        raise LookupError(f"table does not exist: {table_name}")
    table = database.open_table(table_name)
    columns = [name for name in ("id", "content", "category") if name in table.schema.names]
    if set(columns) != {"id", "content", "category"}:
        raise LookupError("memories table is missing id, content, or category")
    return table.to_arrow().select(columns).to_pylist()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / ".hermes" / "lancedb",
        help="Existing LanceDB directory (opened read-only by convention).",
    )
    parser.add_argument("--table", default="memories")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit 1 when invalid or noncanonical rows are found (CI mode).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_rows(read_rows(args.db_path, args.table))
    except Exception as error:
        report = {
            "schema_version": CONTRACT_VERSION,
            "read_only": True,
            "operational_error": {
                "code": "audit_read_failed",
                "message": str(error),
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    summary = report["summary"]
    if args.fail_on_drift and (summary["invalid"] or summary["drift"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
