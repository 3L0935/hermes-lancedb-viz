"""Read diagnostics and manual compaction primitives for the viz server."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


MAINTENANCE_TABLES = (
    "memories",
    "memory_edges",
    "memory_conflicts",
    "memory_conflicts_archive",
)
COMPACTION_BACKUP_PREFIX = "lancedb-pre-compact-"
COMPACTION_BACKUPS_TO_KEEP = 2


def directory_size(path: Path) -> int:
    """Return bytes occupied by regular files below ``path``."""
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _timestamped_backup_path(
    backups_path: Path,
    now: Callable[[], datetime],
) -> Path:
    stamp = now().strftime("%Y%m%d-%H%M%S")
    return backups_path / f"{COMPACTION_BACKUP_PREFIX}{stamp}"


def _compaction_backups(backups_path: Path) -> list[Path]:
    if not backups_path.is_dir():
        return []
    candidates = [
        path for path in backups_path.iterdir()
        if path.is_dir() and path.name.startswith(COMPACTION_BACKUP_PREFIX)
    ]
    return sorted(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def compaction_plan(
    db_path: Path,
    backups_path: Path,
    *,
    estimated_after_bytes: int,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, Any]:
    """Describe the exact next backup and retention action without writing."""
    db_path = Path(db_path)
    backups_path = Path(backups_path)
    backup_to_create = _timestamped_backup_path(backups_path, now)
    existing = _compaction_backups(backups_path)
    delete_count = max(0, len(existing) + 1 - COMPACTION_BACKUPS_TO_KEEP)
    return {
        "manual": True,
        "size_before_bytes": directory_size(db_path),
        "estimated_after_bytes": max(0, int(estimated_after_bytes)),
        "backup_to_create": str(backup_to_create),
        "backups_to_delete": [str(path) for path in existing[:delete_count]],
        "retained_backup_count": COMPACTION_BACKUPS_TO_KEEP,
        "concurrency_warning": (
            "Only duplicate compactions in this server are blocked; writes from another "
            "process are not protected. Run this manual maintenance action during a write pause."
        ),
    }


def _failure(step: str, error: str, started: float, **details) -> dict[str, Any]:
    return {
        "success": False,
        "failed_step": step,
        "error": error,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        **details,
    }


def compact_lancedb(
    db_path: Path,
    backups_path: Path,
    *,
    disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
    connect: Callable[[str], Any] | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, Any]:
    """Back up, retain two managed backups, compact, then verify every table."""
    started = time.perf_counter()
    db_path = Path(db_path).resolve()
    backups_path = Path(backups_path).resolve()
    if not db_path.is_dir():
        return _failure("preflight", f"database directory does not exist: {db_path}", started)
    try:
        backups_path.relative_to(db_path)
    except ValueError:
        pass
    else:
        return _failure("preflight", "backup directory must be outside the database", started)

    size_before = directory_size(db_path)
    usage_path = backups_path if backups_path.exists() else backups_path.parent
    try:
        free_bytes = int(disk_usage(usage_path).free)
    except Exception as error:
        return _failure("disk_space", f"disk-space check failed: {error}", started)
    if free_bytes < size_before:
        return _failure(
            "disk_space",
            f"insufficient disk space: need {size_before} bytes, have {free_bytes}",
            started,
            size_before_bytes=size_before,
            free_bytes=free_bytes,
            required_backup_bytes=size_before,
        )

    backup_created = _timestamped_backup_path(backups_path, now)
    temp_backup = backups_path / f".{backup_created.name}.tmp-{uuid4().hex}"
    if backup_created.exists():
        return _failure("backup", f"backup already exists: {backup_created}", started)
    try:
        backups_path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(db_path, temp_backup, symlinks=True, copy_function=shutil.copy2)
        os.replace(temp_backup, backup_created)
    except Exception as error:
        if temp_backup.exists():
            shutil.rmtree(temp_backup)
        return _failure("backup", f"backup failed: {error}", started)

    backups_deleted = []
    try:
        candidates = _compaction_backups(backups_path)
        for old_backup in candidates[:-COMPACTION_BACKUPS_TO_KEEP]:
            shutil.rmtree(old_backup)
            backups_deleted.append(str(old_backup))
    except Exception as error:
        return _failure(
            "backup_retention", f"backup retention failed: {error}", started,
            backup_created=str(backup_created), backups_deleted=backups_deleted,
        )

    if connect is None:
        import lancedb
        connect = lancedb.connect
    try:
        database = connect(str(db_path))
        available = set(database.list_tables().tables)
        missing = [name for name in MAINTENANCE_TABLES if name not in available]
        if missing:
            return _failure(
                "table_precheck", f"required tables are missing: {', '.join(missing)}", started,
                backup_created=str(backup_created), backups_deleted=backups_deleted,
            )
        tables = {name: database.open_table(name) for name in MAINTENANCE_TABLES}
        rows = {
            name: {"before": int(table.count_rows())}
            for name, table in tables.items()
        }
    except Exception as error:
        return _failure(
            "table_precheck", f"table precheck failed: {error}", started,
            backup_created=str(backup_created), backups_deleted=backups_deleted,
        )

    compacted_tables = []
    for name in MAINTENANCE_TABLES:
        try:
            tables[name].optimize(cleanup_older_than=timedelta(seconds=0))
            compacted_tables.append(name)
        except Exception as error:
            return _failure(
                f"compact.{name}", f"compaction failed for {name}: {error}", started,
                backup_created=str(backup_created), backups_deleted=backups_deleted,
                compacted_tables=compacted_tables, rows=rows,
            )

    fts_unindexed = None
    for name in MAINTENANCE_TABLES:
        try:
            table = database.open_table(name)
            after = int(table.count_rows())
            readable_version = int(table.version)
            rows[name].update({
                "after": after,
                "version": readable_version,
                "version_readable": True,
            })
            if after != rows[name]["before"]:
                return _failure(
                    f"verify.{name}.rows",
                    f"row count changed for {name}: {rows[name]['before']} -> {after}",
                    started,
                    backup_created=str(backup_created), backups_deleted=backups_deleted,
                    compacted_tables=compacted_tables, rows=rows,
                )
            if name == "memories":
                fts_indices = [
                    index for index in table.list_indices()
                    if str(index.index_type).upper() == "FTS" and list(index.columns) == ["content"]
                ]
                if not fts_indices:
                    return _failure(
                        "verify.memories.fts", "content FTS index is missing", started,
                        backup_created=str(backup_created), backups_deleted=backups_deleted,
                        compacted_tables=compacted_tables, rows=rows,
                    )
                fts_unindexed = int(fts_indices[0].num_unindexed_rows)
                if fts_unindexed != 0:
                    return _failure(
                        "verify.memories.fts",
                        f"content FTS has {fts_unindexed} unindexed rows", started,
                        backup_created=str(backup_created), backups_deleted=backups_deleted,
                        compacted_tables=compacted_tables, rows=rows,
                        fts_num_unindexed_rows=fts_unindexed,
                    )
        except Exception as error:
            return _failure(
                f"verify.{name}", f"post-compaction verification failed for {name}: {error}",
                started, backup_created=str(backup_created), backups_deleted=backups_deleted,
                compacted_tables=compacted_tables, rows=rows,
            )

    return {
        "success": True,
        "size_before_bytes": size_before,
        "size_after_bytes": directory_size(db_path),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "backup_created": str(backup_created),
        "backups_deleted": backups_deleted,
        "rows": rows,
        "compacted_tables": compacted_tables,
        "fts_num_unindexed_rows": fts_unindexed,
        "manual_concurrency_limit": (
            "Writes from another process are not blocked; schedule a manual write pause."
        ),
    }


def probe_ollama(embed_url: str, timeout: float = 2.0) -> dict[str, Any]:
    """Probe Ollama without embedding text or mutating model state."""
    parsed = urlsplit(embed_url)
    tags_url = f"{parsed.scheme}://{parsed.netloc}/api/tags"
    try:
        request = Request(tags_url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return {"state": "ok", "status": response.status}
    except Exception as error:
        return {"state": "error", "error": str(error)[:300]}


def collect_health_diagnostics(
    db_path: Path,
    database,
    *,
    pipeline: dict[str, Any],
    ollama_probe: Callable[[], dict[str, Any]],
    table_names: Iterable[str] = MAINTENANCE_TABLES,
) -> dict[str, Any]:
    """Collect bounded, read-only health and pre-maintenance estimates."""
    db_path = Path(db_path)
    available = set(database.list_tables().tables)
    selected = [name for name in table_names if name in available]
    tables: dict[str, dict[str, Any]] = {}
    useful_bytes = 0
    fts = {"state": "missing", "num_indexed_rows": 0, "num_unindexed_rows": None}

    for name in selected:
        try:
            table = database.open_table(name)
            stats = table.stats()
            table_bytes = int(stats.total_bytes)
            useful_bytes += table_bytes
            tables[name] = {
                "state": "ok",
                "rows": int(table.count_rows()),
                "current_version": int(table.version),
                "versions": len(table.list_versions()),
                "fragments": len(table.to_lance().get_fragments()),
                "useful_bytes": table_bytes,
            }
            if name == "memories":
                for index in table.list_indices():
                    if str(index.index_type).upper() != "FTS" or list(index.columns) != ["content"]:
                        continue
                    fts = {
                        "state": "current" if int(index.num_unindexed_rows) == 0 else "lagging",
                        "num_indexed_rows": int(index.num_indexed_rows),
                        "num_unindexed_rows": int(index.num_unindexed_rows),
                    }
                    break
        except Exception as error:
            tables[name] = {"state": "error", "error": str(error)[:300]}

    disk_bytes = directory_size(db_path)
    history_bytes = max(0, disk_bytes - useful_bytes)
    ollama = ollama_probe()
    return {
        "read_only": True,
        "tables": tables,
        "fts": fts,
        "storage": {
            "disk_bytes": disk_bytes,
            "useful_bytes": useful_bytes,
            "history_bytes": history_bytes,
        },
        "ollama": ollama,
        "pipeline": dict(pipeline),
        "maintenance_estimate": {
            "current_bytes": disk_bytes,
            "backup_bytes": disk_bytes,
            "estimated_after_bytes": useful_bytes,
            "estimated_reclaimable_bytes": history_bytes,
            "estimate_only": True,
        },
        "budgets": {"tables": len(selected)},
    }
