"""Read diagnostics and manual compaction primitives for the viz server."""

from __future__ import annotations

from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


MAINTENANCE_TABLES = (
    "memories",
    "memory_edges",
    "memory_conflicts",
    "memory_conflicts_archive",
)
COMPACTION_BACKUP_PREFIX = "lancedb-pre-compact-"
COMPACTION_BACKUPS_TO_KEEP = 2
MAINTENANCE_MAX_VERSIONS = 64
MAINTENANCE_MAX_FRAGMENTS = 64
MAINTENANCE_MAX_ORPHAN_INDEX_DIRECTORIES = 4


def maintenance_lock_path(db_path: Path) -> Path:
    """Return the lock shared with `LanceDBStore.write_batch()`."""
    db_path = Path(db_path).resolve()
    return db_path / ".write.lock"


def _value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _active_index_uuids(table: Any) -> set[str] | None:
    """Return index UUIDs referenced by current Lance metadata, or None.

    ``table.list_indices()`` is tried first because it is the only path that
    works without ``pylance``: the production container has no ``lance``
    module, so ``to_lance()`` raises there and every caller of this helper
    would silently get ``None`` (no orphan detection, no bounded trigger).
    Measured on a real database: both paths return the same active set.
    """
    indices = None
    try:
        indices = list(table.list_indices())
    except Exception:
        indices = None
    if indices is None:
        try:
            dataset = table.to_lance()
            if hasattr(dataset, "describe_indices"):
                descriptions = list(dataset.describe_indices())
                segment_lists = [
                    _value(description, "segments") for description in descriptions
                ]
                if descriptions and any(segments is None for segments in segment_lists):
                    indices = dataset.list_indices()
                else:
                    indices = [
                        segment
                        for segments in segment_lists
                        for segment in (segments or [])
                    ]
            else:
                indices = dataset.list_indices()
        except Exception:
            return None
    return _uuid_set(indices)


def _uuid_set(indices: Any) -> set[str] | None:
    """Collect normalized UUIDs from index descriptors, or None when unreadable."""
    active = set()
    for index in indices:
        value = _value(index, "uuid")
        if value is None:
            value = _value(index, "index_uuid")
        if value is None:
            return None
        try:
            active.add(str(UUID(str(value))))
        except (TypeError, ValueError, AttributeError):
            return None
    return active


def _physical_index_uuids(db_path: Path, table_name: str) -> set[str]:
    """List UUID-shaped physical index directories without opening the table."""
    root = Path(db_path) / f"{table_name}.lance" / "_indices"
    if not root.is_dir():
        return set()
    found = set()
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            found.add(str(UUID(path.name)))
        except (TypeError, ValueError, AttributeError):
            continue
    return found


def _orphan_index_uuids(
    db_path: Path,
    table_name: str,
    table: Any,
) -> set[str] | None:
    active = _active_index_uuids(table)
    if active is None:
        return None
    return _physical_index_uuids(db_path, table_name) - active


def _stat_value(stats: Any, key: str, default: int = 0) -> int:
    """Read one table statistic across LanceDB versions.

    LanceDB 0.30.2 (the container) returns a plain dict from ``table.stats()``,
    while 0.34.0 returns an object with attributes. Support both.
    """
    if stats is None:
        return default
    if isinstance(stats, dict):
        value = stats.get(key, default)
    else:
        value = getattr(stats, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fragment_count(table: Any) -> int | None:
    """Count table fragments, or None when the API is unavailable."""
    try:
        return len(table.to_lance().get_fragments())
    except Exception:
        pass
    stats = None
    try:
        stats = table.stats()
    except Exception:
        return None
    if isinstance(stats, dict):
        fragment_stats = stats.get("fragment_stats") or {}
        if isinstance(fragment_stats, dict):
            count = fragment_stats.get("num_fragments")
            if count is not None:
                try:
                    return int(count)
                except (TypeError, ValueError):
                    return None
    return None


def _fts_index_stats(table: Any) -> dict[str, int] | None:
    """Return FTS row counts, or None when unavailable.

    LanceDB 0.34.0 exposes ``num_indexed_rows``/``num_unindexed_rows`` directly on
    the object returned by ``list_indices()``; 0.30.2 exposes those counts only
    through ``table.index_stats(<name>)``. Never invent the numbers: return None
    when neither path works.
    """
    try:
        indices = list(table.list_indices())
    except Exception:
        return None
    for index in indices:
        try:
            if str(index.index_type).upper() != "FTS" or list(index.columns) != ["content"]:
                continue
        except Exception:
            continue
        indexed = getattr(index, "num_indexed_rows", None)
        unindexed = getattr(index, "num_unindexed_rows", None)
        if indexed is not None and unindexed is not None:
            return {"num_indexed_rows": int(indexed), "num_unindexed_rows": int(unindexed)}
        name = getattr(index, "name", None)
        if not name:
            continue
        try:
            statistics = table.index_stats(name)
            return {
                "num_indexed_rows": int(statistics.num_indexed_rows),
                "num_unindexed_rows": int(statistics.num_unindexed_rows),
            }
        except Exception:
            return None
    return None


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
    diagnostics: dict[str, Any] | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, Any]:
    """Describe the exact next backup and retention action without writing."""
    db_path = Path(db_path)
    backups_path = Path(backups_path)
    backup_to_create = _timestamped_backup_path(backups_path, now)
    existing = _compaction_backups(backups_path)
    delete_count = max(0, len(existing) + 1 - COMPACTION_BACKUPS_TO_KEEP)
    trigger_reasons = []
    diagnostics = diagnostics or {}
    for name, table in (diagnostics.get("tables") or {}).items():
        versions = table.get("versions") if isinstance(table, dict) else None
        fragments = table.get("fragments") if isinstance(table, dict) else None
        if versions is not None and int(versions) > MAINTENANCE_MAX_VERSIONS:
            trigger_reasons.append(
                f"{name}.versions={int(versions)}>{MAINTENANCE_MAX_VERSIONS}"
            )
        if fragments is not None and int(fragments) > MAINTENANCE_MAX_FRAGMENTS:
            trigger_reasons.append(
                f"{name}.fragments={int(fragments)}>{MAINTENANCE_MAX_FRAGMENTS}"
            )
    fts = diagnostics.get("fts") or {}
    orphan_directories = (
        fts.get("orphan_index_directories") if isinstance(fts, dict) else None
    )
    if (
        orphan_directories is not None
        and int(orphan_directories) > MAINTENANCE_MAX_ORPHAN_INDEX_DIRECTORIES
    ):
        trigger_reasons.append(
            "memories.orphan_index_directories="
            f"{int(orphan_directories)}>{MAINTENANCE_MAX_ORPHAN_INDEX_DIRECTORIES}"
        )
    return {
        "manual": True,
        "recommended": bool(trigger_reasons),
        "trigger_reasons": trigger_reasons,
        "thresholds": {
            "max_versions": MAINTENANCE_MAX_VERSIONS,
            "max_fragments": MAINTENANCE_MAX_FRAGMENTS,
            "max_orphan_index_directories": MAINTENANCE_MAX_ORPHAN_INDEX_DIRECTORIES,
        },
        "size_before_bytes": directory_size(db_path),
        "estimated_after_bytes": max(0, int(estimated_after_bytes)),
        "backup_to_create": str(backup_to_create),
        "backups_to_delete": [str(path) for path in existing[:delete_count]],
        "retained_backup_count": COMPACTION_BACKUPS_TO_KEEP,
        "concurrency_warning": (
            "Cooperating LanceDBStore writers share an advisory lock with compaction; "
            "pause any raw external writer that does not use the store batch API."
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
    """Acquire the writer lock, then run backup-first compaction."""
    started = time.perf_counter()
    resolved_db_path = Path(db_path).resolve()
    if not resolved_db_path.is_dir():
        return _failure(
            "preflight", f"database directory does not exist: {resolved_db_path}", started
        )
    lock_file = None
    try:
        lock_file = maintenance_lock_path(resolved_db_path).open("a+")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if lock_file is not None:
            lock_file.close()
        return _failure(
            "lock",
            "another cooperating writer or compaction holds the database lock",
            started,
            code="maintenance_lock_busy",
        )
    except Exception as error:
        if lock_file is not None:
            lock_file.close()
        return _failure("lock", f"maintenance lock failed: {error}", started)
    try:
        return _compact_lancedb_locked(
            resolved_db_path,
            backups_path,
            disk_usage=disk_usage,
            connect=connect,
            now=now,
            started=started,
        )
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _compact_lancedb_locked(
    db_path: Path,
    backups_path: Path,
    *,
    disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
    connect: Callable[[str], Any] | None = None,
    now: Callable[[], datetime] = datetime.now,
    started: float | None = None,
) -> dict[str, Any]:
    """Back up, retain two managed backups, compact, then verify every table."""
    if started is None:
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

    orphan_index_directories_removed: dict[str, int | None] = {}
    for name in MAINTENANCE_TABLES:
        try:
            table = database.open_table(name)
            orphan_uuids = _orphan_index_uuids(db_path, name, table)
            if orphan_uuids is None:
                orphan_index_directories_removed[name] = None
                continue
            index_root = (db_path / f"{name}.lance" / "_indices").resolve()
            removed = 0
            for orphan_uuid in sorted(orphan_uuids):
                target = (index_root / orphan_uuid).resolve()
                if target.parent != index_root:
                    raise ValueError(f"unsafe index cleanup target: {target}")
                if target.is_dir():
                    shutil.rmtree(target)
                    removed += 1
            orphan_index_directories_removed[name] = removed
        except Exception as error:
            return _failure(
                f"cleanup_indices.{name}",
                f"orphan index cleanup failed for {name}: {error}",
                started,
                backup_created=str(backup_created),
                backups_deleted=backups_deleted,
                compacted_tables=compacted_tables,
                orphan_index_directories_removed=orphan_index_directories_removed,
                rows=rows,
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
            active_uuids = _active_index_uuids(table)
            if active_uuids is not None:
                physical_uuids = _physical_index_uuids(db_path, name)
                missing_active = active_uuids - physical_uuids
                remaining_orphans = physical_uuids - active_uuids
                if missing_active or remaining_orphans:
                    return _failure(
                        f"verify.{name}.indices",
                        (
                            f"index directory verification failed for {name}: "
                            f"missing_active={sorted(missing_active)}, "
                            f"remaining_orphans={sorted(remaining_orphans)}"
                        ),
                        started,
                        backup_created=str(backup_created),
                        backups_deleted=backups_deleted,
                        compacted_tables=compacted_tables,
                        orphan_index_directories_removed=orphan_index_directories_removed,
                        rows=rows,
                    )
            if after != rows[name]["before"]:
                return _failure(
                    f"verify.{name}.rows",
                    f"row count changed for {name}: {rows[name]['before']} -> {after}",
                    started,
                    backup_created=str(backup_created), backups_deleted=backups_deleted,
                    compacted_tables=compacted_tables, rows=rows,
                )
            if name == "memories":
                fts_stats = _fts_index_stats(table)
                if fts_stats is None:
                    return _failure(
                        "verify.memories.fts", "content FTS index is missing", started,
                        backup_created=str(backup_created), backups_deleted=backups_deleted,
                        compacted_tables=compacted_tables, rows=rows,
                    )
                fts_unindexed = fts_stats["num_unindexed_rows"]
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
        "orphan_index_directories_removed": orphan_index_directories_removed,
        "fts_num_unindexed_rows": fts_unindexed,
        "manual_concurrency_limit": (
            "Cooperating store writers are locked; pause raw external writers."
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
    fts = {
        "state": "missing",
        "num_indexed_rows": 0,
        "num_unindexed_rows": None,
        "active_index_directories": None,
        "physical_index_directories": 0,
        "orphan_index_directories": None,
    }

    for name in selected:
        try:
            table = database.open_table(name)
            stats = table.stats()
            table_bytes = _stat_value(stats, "total_bytes")
            useful_bytes += table_bytes
            fragments = _fragment_count(table)
            tables[name] = {
                "state": "ok",
                "rows": int(table.count_rows()),
                "current_version": int(table.version),
                "versions": len(table.list_versions()),
                "fragments": fragments,
                "useful_bytes": table_bytes,
            }
            if name == "memories":
                fts_stats = _fts_index_stats(table)
                active_uuids = _active_index_uuids(table)
                physical_uuids = _physical_index_uuids(db_path, name)
                orphan_uuids = (
                    None if active_uuids is None else physical_uuids - active_uuids
                )
                if fts_stats is None:
                    fts = {
                        "state": "missing",
                        "num_indexed_rows": 0,
                        "num_unindexed_rows": None,
                        "active_index_directories": (
                            None if active_uuids is None else len(active_uuids)
                        ),
                        "physical_index_directories": len(physical_uuids),
                        "orphan_index_directories": (
                            None if orphan_uuids is None else len(orphan_uuids)
                        ),
                    }
                else:
                    fts = {
                        "state": "current" if fts_stats["num_unindexed_rows"] == 0 else "lagging",
                        "num_indexed_rows": fts_stats["num_indexed_rows"],
                        "num_unindexed_rows": fts_stats["num_unindexed_rows"],
                        "active_index_directories": (
                            None if active_uuids is None else len(active_uuids)
                        ),
                        "physical_index_directories": len(physical_uuids),
                        "orphan_index_directories": (
                            None if orphan_uuids is None else len(orphan_uuids)
                        ),
                    }
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
