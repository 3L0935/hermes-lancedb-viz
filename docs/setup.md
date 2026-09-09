# Setup Guide

This guide installs the local-only LanceDB memory provider and its lightweight systemd visualizer. LanceDB remains the only persistent database. Ollama is used only for embeddings.

## 1. Prerequisites

- Hermes Agent with a Python 3.11+ virtual environment
- Ollama listening on `127.0.0.1:11434`
- `nomic-embed-text`
- A user systemd session for the visualizer

```bash
ollama pull nomic-embed-text
cd ~/.hermes/hermes-agent
venv/bin/pip install lancedb pyarrow httpx numpy
```

## 2. Configure Hermes

In `~/.hermes/config.yaml`:

```yaml
memory:
  provider: lancedb
  lancedb:
    db_path: ~/.hermes/lancedb
    embed_model: nomic-embed-text
```

The plugin does not auto-ingest conversations. Writes remain explicit through `lancedb_add`. Optional LLM relation extraction is not part of the default path and must remain disabled unless deliberately implemented and enabled in a future release.

## 3. Preview and deploy

From the repository root:

```bash
./scripts/deploy-local.sh --dry-run
./scripts/deploy-local.sh
systemctl --user enable lancedb-viz.service
```

The deployment synchronizes:

```text
plugin/       -> ~/.hermes/plugins/lancedb/
plugin/       -> ~/.hermes/hermes-agent/plugins/memory/lancedb/
server/static -> ~/.hermes/lancedb-viz/
systemd unit  -> ~/.config/systemd/user/lancedb-viz.service
```

The canonical user plugin survives Hermes source-tree updates. The runtime copy is retained for bundled-first compatibility.

## 4. Migrate an existing database

Always preview first. Before applying, create a complete filesystem backup while writers are quiet.

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/migrate_graph_retention.py
cp -a ~/.hermes/lancedb ~/.hermes/lancedb-backup-$(date +%Y%m%d-%H%M%S)
~/.hermes/hermes-agent/venv/bin/python scripts/migrate_graph_retention.py --apply
```

The migration:

- adds `target_id` to legacy `memory_edges` schemas;
- resolves exact `Domain:Subject` targets;
- resolves short subjects only when exactly one memory matches;
- leaves ambiguous or missing targets unresolved;
- removes edges whose source memory no longer exists;
- creates or backfills `memory_conflicts` from explicit same-subject `key=value` disagreements.

It makes no network or LLM call. It does not merge, replace, or delete memories.

## 5. Restart Hermes

The visualizer is restarted by the deploy script. Restart the Hermes gateway separately so its process reloads the provider and exposes the current tool schemas:

```bash
systemctl --user restart hermes-gateway.service
```

If issuing that command from inside a running Hermes gateway is blocked, execute it from another shell or use the client restart command.

## 6. Verify

```bash
systemctl --user is-active lancedb-viz.service
curl -fsS http://127.0.0.1:7778/api/stats | python -m json.tool
curl -fsS http://127.0.0.1:7778/api/graph | python -m json.tool
curl -fsS 'http://127.0.0.1:7778/api/conflicts?status=open' | python -m json.tool
./scripts/verify-setup.sh
```

Expected properties:

- the service reports `active`;
- `/api/stats` reports the existing memory count;
- every typed edge returned by `/api/graph` has `from` and `to` memory IDs;
- `/api/conflicts` returns a JSON array;
- the plugin exposes eight tools, including `lancedb_conflicts`;
- canonical and runtime plugin files are byte-identical to `plugin/`.

Dashboard: `http://127.0.0.1:7778`

## 7. Retrieval behavior

`lancedb_search` defaults to `mode=auto`:

- quoted strings, UUIDs, paths, and exact-query markers use lexical retrieval;
- relationship-shaped questions use hybrid seed retrieval plus one-hop typed-edge traversal;
- other queries use BM25/vector hybrid retrieval;
- `relation_depth=0` disables traversal;
- explicit `mode=lexical|hybrid|graph` overrides routing.

This routing is deterministic and local. It adds no model call or daemon.

## 8. Contradiction behavior

The detector is intentionally conservative. It records a conflict only when two supported memory categories share the same `Domain:Subject` and the same explicit key with different values.

```text
Project:Alpha port=7777 [Tier=2]
Project:Alpha port=7778 [Tier=2]
```

Both memories remain intact. Updating a claim closes stale open conflicts and rechecks the current content. Use `lancedb_conflicts` or the visualizer's Conflicts page for review.

## 9. Troubleshooting

### Plugin tools are missing

Verify both copies and restart the gateway:

```bash
diff -u plugin/store.py ~/.hermes/plugins/lancedb/store.py
diff -u plugin/store.py ~/.hermes/hermes-agent/plugins/memory/lancedb/store.py
systemctl --user restart hermes-gateway.service
```

### Visualizer does not start

```bash
systemctl --user status lancedb-viz.service
journalctl --user -u lancedb-viz.service -n 100 --no-pager
```

### Legacy relations remain unresolved

Run the dry-run and inspect `ambiguous_or_missing`. Those rows are intentionally not guessed. Update them with a concrete `target_id` or a unique target label.

### Ollama is unavailable

Writes can fall back to a zero vector, which reduces retrieval quality. Restore Ollama and run:

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/reembed-entries.py
```

### Optional Docker deployment

Docker files remain available for portable setups, but the supported low-footprint local pipeline is the user systemd service on port 7778.
