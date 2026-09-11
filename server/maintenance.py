"""Read diagnostics and manual compaction primitives for the viz server."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAINTENANCE_TABLES = (
    "memories",
    "memory_edges",
    "memory_conflicts",
    "memory_conflicts_archive",
)


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
