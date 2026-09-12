#!/usr/bin/env python3
"""Plan or explicitly apply a bounded LanceDB re-embedding pass."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


MODEL = os.environ.get("LANCE_EMBED_MODEL", "nomic-embed-text")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/embed"
BATCH_SIZE = 10
_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def default_db_path() -> Path:
    hermes_root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_root / "lancedb"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=default_db_path())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read and report only (default).")
    mode.add_argument("--apply", action="store_true", help="Explicitly update vectors in the selected database.")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    return parser


def read_entries(db_path: Path):
    import lancedb

    db = lancedb.connect(str(db_path.expanduser().resolve()))
    table = db.open_table("memories")
    rows = table.to_arrow().select(["id", "content"]).to_pylist()
    return table, rows


def _sql_literal(value: str) -> str:
    if not _MEMORY_ID_RE.fullmatch(value):
        raise ValueError(f"invalid memory ID: {value!r}")
    return "'" + value.replace("'", "''") + "'"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 100:
        print("ERROR: --batch-size must be between 1 and 100", file=sys.stderr)
        return 2

    try:
        table, entries = read_entries(args.db_path)
    except Exception as error:
        print(f"ERROR: cannot read {args.db_path}: {error}", file=sys.stderr)
        return 2

    print(f"Found {len(entries)} entries in {args.db_path}.")
    if not args.apply:
        print("--dry-run mode: no Ollama request and no database write.")
        for entry in entries[:3]:
            print(f"  Would embed: {str(entry['id'])[:16]}...")
        print(f"Would embed {len(entries)} entries total. Use --apply only after a verified backup.")
        return 0

    import httpx
    from plugin.store import LanceDBStore

    store = LanceDBStore(args.db_path)
    table = store._table
    updated = 0
    failed = 0
    with store.write_batch():
        for start in range(0, len(entries), args.batch_size):
            batch = entries[start:start + args.batch_size]
            texts = [
                re.sub(r"\n::relations::.*", "", str(entry["content"])).strip()
                or str(entry["content"])
                for entry in batch
            ]
            try:
                response = httpx.post(
                    OLLAMA_URL,
                    json={"model": MODEL, "input": texts, "keep_alive": "30s"},
                    timeout=60,
                )
                response.raise_for_status()
                embeddings = response.json().get("embeddings", [])
                if len(embeddings) != len(batch):
                    raise ValueError("embedding response cardinality mismatch")
                vectors = [np.asarray(vector, dtype=np.float32) for vector in embeddings]
                if any(vector.shape != (768,) or not np.isfinite(vector).all() for vector in vectors):
                    raise ValueError("embedding response contains invalid vectors")
            except Exception as error:
                failed += len(batch)
                print(f"ERROR batch {start}: {error}", file=sys.stderr)
                continue

            for entry, vector in zip(batch, vectors):
                memory_id = str(entry["id"])
                table.update(
                    where=f"id = {_sql_literal(memory_id)}",
                    values={"vector": vector.tolist()},
                )
                updated += 1
            print(f"[{min(start + args.batch_size, len(entries))}/{len(entries)}] re-embedded")

    print(f"Done: {updated} updated, {failed} failed with {MODEL}.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
