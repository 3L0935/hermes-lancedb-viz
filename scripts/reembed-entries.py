#!/usr/bin/env python3
"""
Re-embed all LanceDB entries using nomic-embed-text via Ollama.
Run after any mass content modification (reformat, correction, bulk update).

Usage:
    ~/.hermes/hermes-agent/venv/bin/python3 reembed-entries.py [--dry-run]

What it does:
    - Reads every entry in ~/.hermes/lancedb
    - Sends content to local Ollama nomic-embed-text (batch size 10)
    - Updates vector column in-place (keeps all other fields intact)

Why this exists:
    Changing 'content' doesn't auto-update vectors. The graph cosine similarity
    becomes stale. Run this after any content migration to restore proper links.
"""

import sys
import lancedb
import requests

DB_PATH = '/home/elo/.hermes/lancedb'
OLLAMA_URL = 'http://localhost:11434/api/embed'
MODEL = 'nomic-embed-text'
BATCH_SIZE = 10


def main():
    dry_run = '--dry-run' in sys.argv

    # ── Check Ollama ───────────────────────────────────────────
    try:
        r = requests.get('http://localhost:11434/api/tags', timeout=5)
        models = [m['name'] for m in r.json().get('models', [])]
    except Exception as e:
        print(f"ERROR: Ollama not reachable at {OLLAMA_URL}: {e}")
        sys.exit(1)

    if not any('nomic' in m for m in models):
        print(f"Pulling {MODEL} (first time)...")
        requests.post('http://localhost:11434/api/pull',
                      json={'name': MODEL}, timeout=300)

    # ── Read DB ────────────────────────────────────────────────
    print(f"Connecting to {DB_PATH}...")
    db = lancedb.connect(DB_PATH)
    tbl = db.open_table('memories')
    data = tbl.to_arrow().to_pydict()
    entries = [{k: data[k][i] for k in data} for i in range(len(data['id']))]
    total = len(entries)
    print(f"Found {total} entries.\n")

    if dry_run:
        print("--dry-run mode: no changes will be made.")
        for e in entries[:3]:
            print(f"  Would embed: {e['id'][:16]}... -> {e['content'][:60]}...")
        print(f"\nWould embed {total} entries total. Run without --dry-run to apply.")
        return

    # ── Embed + update ─────────────────────────────────────────
    import re
    updated = 0
    for start in range(0, total, BATCH_SIZE):
        batch = entries[start:start + BATCH_SIZE]
        # Strip ::relations:: lines before embedding (vector noise)
        texts = [re.sub(r'\n::relations::.*', '', e['content']).strip() or e['content'] for e in batch]
        ids = [e['id'] for e in batch]

        resp = requests.post(OLLAMA_URL, json={
            'model': MODEL,
            'input': texts,
            'keep_alive': '30s'
        }, timeout=60)

        if resp.status_code != 200:
            print(f"  ERROR batch {start}: {resp.status_code} {resp.text[:200]}")
            continue

        embeddings = resp.json()['embeddings']

        for i, (eid, emb) in enumerate(zip(ids, embeddings)):
            tbl.update(
                where=f"id = '{eid}'",
                values={"vector": emb}
            )
            updated += 1

        done = min(start + BATCH_SIZE, total)
        print(f"  [{done}/{total}] re-embedded")

    print(f"\nDone! {updated}/{total} entries re-embedded with {MODEL}.")


if __name__ == '__main__':
    main()
