#!/usr/bin/env python3
"""Plan or apply safe memory-format normalization on a verified DB copy only."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
from typing import Any, Callable


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


def validate_work_root(work_root: Path) -> Path:
    resolved = work_root.expanduser().resolve()
    tmp_root = Path("/tmp").resolve()
    if resolved == tmp_root or not resolved.is_relative_to(tmp_root):
        raise ValueError("work directory must be a dedicated child of /tmp")
    return resolved


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(str(item.relative_to(path)).encode("utf-8"))
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def prepare_working_copy(source_db: Path, work_root: Path) -> dict[str, Any]:
    source = source_db.expanduser().resolve()
    work = validate_work_root(work_root)
    if not source.is_dir():
        raise FileNotFoundError(f"source database does not exist: {source}")
    work.mkdir(parents=True, exist_ok=True)
    destination = work / "lancedb-copy"
    if destination.exists():
        raise FileExistsError(f"working copy already exists: {destination}")

    source_before = tree_digest(source)
    shutil.copytree(source, destination)
    source_after = tree_digest(source)
    copied = tree_digest(destination)
    if source_before != source_after or copied != source_after:
        raise RuntimeError("source changed during copy or copy verification failed")
    return {
        "source_db": str(source),
        "working_copy": str(destination),
        "source_digest": source_after,
        "copy_digest": copied,
        "copy_verified": True,
    }


def backup_working_copy(working_copy: Path, work_root: Path) -> dict[str, Any]:
    work = validate_work_root(work_root)
    backup = work / "backup-before-apply"
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    before = tree_digest(working_copy)
    shutil.copytree(working_copy, backup)
    after = tree_digest(backup)
    if before != after:
        raise RuntimeError("backup verification failed")
    return {
        "backup_path": str(backup),
        "backup_digest": after,
        "backup_verified": True,
    }


def classify_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    classifications = []
    counts: Counter[str] = Counter()
    for row in rows:
        memory_id = str(row.get("id") or "")
        content = row.get("content")
        category = row.get("category")
        try:
            memory = contract.parse_content(content, category=category)
            canonical_content = contract.render_content(memory)
            warnings = [item.to_dict() for item in contract.contract_warnings(memory)]
            if content != canonical_content:
                classification = "safe_normalize"
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
            if classification == "safe_normalize":
                entry["canonical_content"] = canonical_content
        except contract.MemoryContractError as error:
            classification = "manual_review"
            entry = {
                "memory_id": memory_id,
                "classification": classification,
                "category": category,
                "error": error.to_dict(),
            }
        counts[classification] += 1
        classifications.append(entry)
    return {
        "summary": dict(sorted(counts.items())),
        "rows": classifications,
    }


def _load_store_class():
    package_name = "hermes_memory_migration_plugin"
    module_name = f"{package_name}.store"
    if module_name in sys.modules:
        return sys.modules[module_name].LanceDBStore
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "plugin")]
    package.__package__ = package_name
    sys.modules[package_name] = package
    store = _load_file_module(module_name, ROOT / "plugin" / "store.py")
    return store.LanceDBStore


def apply_plan(
    working_copy: Path,
    plan: dict[str, Any],
    *,
    store_factory: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    factory = store_factory or _load_store_class()
    store = factory(working_copy)
    updated = 0
    errors = []
    for row in plan["rows"]:
        if row["classification"] != "safe_normalize":
            continue
        try:
            ok = store.update(
                row["memory_id"],
                legacy=True,
                content=row["canonical_content"],
                category=row["category"],
            )
            if ok:
                updated += 1
            else:
                errors.append({"memory_id": row["memory_id"], "code": "update_failed"})
        except Exception as error:
            errors.append({
                "memory_id": row["memory_id"],
                "code": getattr(getattr(error, "issue", None), "code", "update_failed"),
                "message": str(error),
            })
    return {"updated": updated, "errors": errors}


def run_migration(
    source_db: Path,
    work_root: Path,
    *,
    apply: bool = False,
    store_factory: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    copy_report = prepare_working_copy(source_db, work_root)
    working_copy = Path(copy_report["working_copy"])
    before = classify_rows(audit.read_rows(working_copy))
    report = {
        "schema_version": 1,
        "mode": "apply_on_copy" if apply else "dry_run_on_copy",
        **copy_report,
        "before": before,
        "source_mutated": False,
    }
    if not apply:
        return report

    report["backup"] = backup_working_copy(working_copy, work_root)
    report["apply"] = apply_plan(
        working_copy,
        before,
        store_factory=store_factory,
    )
    report["after"] = classify_rows(audit.read_rows(working_copy))
    report["source_mutated"] = tree_digest(source_db.expanduser().resolve()) != copy_report["source_digest"]
    if report["source_mutated"]:
        raise RuntimeError("source database changed while migration copy was processed")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db",
        type=Path,
        default=Path.home() / ".hermes" / "lancedb",
        help="Source DB is copied and never opened for writes.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Dedicated /tmp child. Defaults to a new persistent temp directory.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Plan only (default).")
    mode.add_argument("--apply", action="store_true", help="Apply safe normalization to the copy.")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    work_root = args.work_dir or Path(tempfile.mkdtemp(prefix="hermes-memory-migration-"))
    try:
        report = run_migration(
            args.source_db,
            work_root,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
        if args.apply and report["apply"]["errors"]:
            return 1
        return 0
    except Exception as error:
        print(json.dumps({
            "schema_version": 1,
            "mode": "apply_on_copy" if args.apply else "dry_run_on_copy",
            "operational_error": {"code": "migration_failed", "message": str(error)},
        }, ensure_ascii=False, indent=2 if args.pretty else None))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
