# Setup Guide

This guide installs the local-only LanceDB memory provider and its Docker
visualizer managed by Hermes Hub. LanceDB remains the only persistent database.
Ollama is used only for embeddings; the disabled systemd service on port 7778
is a recovery fallback.

## 1. Prerequisites

- Hermes Agent with a Python 3.11+ virtual environment
- Ollama listening on `127.0.0.1:11434`
- `nomic-embed-text`
- Docker and the Hermes Hub checkout for the canonical visualizer

```bash
ollama pull nomic-embed-text
cd ~/github/hermes-lancedb-viz
"$HOME/.hermes/hermes-agent/venv/bin/pip" install -r requirements.txt
```

`requirements.txt` pins `lancedb==0.34.0`. Keep that pin aligned in the host
venv and Hub image: BM25 scores are engine-specific, so an engine upgrade can
invalidate the abstention threshold without changing this repository's code.

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

Start or recreate the canonical container from the Hub service, then sync this
repository:

```bash
cd ~/github/hermes-hub/services/lancedb-viz
docker compose up -d

cd ~/github/hermes-lancedb-viz
./scripts/deploy-local.sh --dry-run
./scripts/deploy-local.sh
```

The Hub definition uses image `lancedb-viz:local`, the external
`hub-services` network, and bind mounts under `~/.hermes/lancedb-viz/` for
`server.py`, `maintenance.py`, and `static/`. `deploy-local.sh` synchronizes
those files and both plugin copies, then restarts the existing container. Static
files are read per request; Python server changes require the container restart.

Dashboard (Docker via Hermes Hub, primary): `http://localhost:7777`

A user systemd unit (`lancedb-viz.service`, port 7778) is installed but left
disabled. Use it only for deliberate recovery without the canonical container.

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

## 4. Restart Hermes

The visualizer is restarted by the deploy script. Restart the Hermes gateway separately so its process reloads the provider and exposes the current tool schemas:

```bash
systemctl --user restart hermes-gateway.service
```

If issuing that command from inside a running Hermes gateway is blocked, execute it from another shell or use the client restart command.

This step is not optional after plugin changes. `deploy-local.sh` rewrites the
runtime module but cannot make an already-running Python gateway reload it; the
container can otherwise serve the new server while the gateway silently keeps
the old threshold or embedder.

## 5. Verify

```bash
cd ~/github/hermes-hub/services/lancedb-viz
docker compose ps
cd ~/github/hermes-lancedb-viz
curl -fsS http://127.0.0.1:7777/api/stats | python3 -m json.tool
curl -fsS http://127.0.0.1:7777/api/health | python3 -m json.tool
curl -fsS 'http://127.0.0.1:7777/api/conflicts?status=open' | python3 -m json.tool
./scripts/verify-setup.sh
```

Expected properties:

- the `lancedb-viz` container reports `running` and healthy;
- `/api/stats` reports the existing memory count;
- `/api/health` reports bounded read-only table, FTS, Ollama, and pipeline diagnostics;
- every edge returned by `/api/typed-edges` has `from` and `to` memory IDs;
- `/api/conflicts` returns a JSON array;
- the plugin exposes eight tools, including `lancedb_conflicts`;
- canonical and runtime plugin files are byte-identical to `plugin/`.

Dashboard: `http://127.0.0.1:7777`

## 6. Retrieval behavior

`lancedb_search` defaults to `mode=auto`:

- quoted strings, UUIDs, paths, and exact-query markers use lexical retrieval;
- relationship-shaped questions use hybrid seed retrieval plus one-hop typed-edge traversal;
- other queries use BM25/vector hybrid retrieval;
- `relation_depth=0` disables traversal;
- explicit `mode=lexical|hybrid|graph` overrides routing.

This routing is deterministic and local. It adds no model call or daemon.

Hybrid retrieval abstains when every candidate has both BM25 score below
`SEARCH_MIN_BM25_SCORE = 12.80` and cosine distance above
`SEARCH_MAX_COSINE_DISTANCE = 0.30`. Request `/api/search?diagnostics=1` to see
the route, abstention reason, thresholds, and `calibrated_engine`,
`running_engine`, and `matches`. Treat `matches=false` as an invalid BM25
calibration, not as a comparable measurement.

Run the retention harness only through a disposable `/tmp` fixture:

```bash
cd ~/github/hermes-lancedb-viz
PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent" \
OLLAMA_HOST=http://127.0.0.1:11434 \
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  audit/repro/benchmark-retrieval.py \
  --prepare-fixture --replace-fixture \
  --output /tmp/bench-retrieval.json
```

The final gate treats zero false no-answer results and preservation of all
`old_critical_correction` questions as strict invariants. MRR and recall are
only anti-collapse guardrails against the frozen lexical control. Targets and
their measured noise rationale are in
`audit/repro/retrieval-baseline-reference.json`; results record engine, table
versions, and row counts.

## 7. Maintenance and compaction

`GET /api/maintenance/compact/plan` is read-only. The confirmed POST creates an
atomically published backup before compaction, retains two managed backups, and
verifies every table afterward. Run it only during a write pause:

```bash
curl -fsS http://127.0.0.1:7777/api/maintenance/compact/plan \
  | python3 -m json.tool
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  --data '{"confirmed":true}' \
  http://127.0.0.1:7777/api/maintenance/compact \
  | python3 -m json.tool
```

Failures report `failed_step`. If backup creation completed, the response also
keeps `backup_created` available for recovery.

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

### Canonical visualizer does not start

```bash
cd ~/github/hermes-hub/services/lancedb-viz
docker compose ps
docker compose logs --tail 100 lancedb-viz
```

### Legacy relations remain unresolved

Run the dry-run and inspect `ambiguous_or_missing`. Those rows are intentionally not guessed. Update them with a concrete `target_id` or a unique target label.

### Ollama is unavailable

Current writes fail with a retryable error and do not persist a zero vector.
Restore Ollama before retrying. Use the re-embedding script only to repair
legacy zero-vector rows:

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/reembed-entries.py
```

### Optional systemd recovery fallback

Port 7778 is not a second canonical deployment. If Docker recovery is not
possible, explicitly start the fallback and point verification at it:

```bash
systemctl --user start lancedb-viz.service
LANCEDB_VIZ_MODE=systemd ./scripts/verify-setup.sh
```
