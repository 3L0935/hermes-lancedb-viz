# Hermes LanceDB Memory — Plugin + Visualizer

Local-first vector memory for [Hermes Agent](https://github.com/nousresearch/hermes-agent).
SQLite-free, LanceDB-only storage with Ollama embeddings, entity extraction,
hybrid search (BM25 + vector), and an interactive web dashboard.

## What this is

A complete memory system for Hermes Agent that persists across sessions:

- **Plugin** (`plugin/`) - LanceDB memory provider for Hermes. 8 MCP tools:
  search, add, update, delete, get, list, graph, conflicts. Auto entity extraction,
  auto-tagging, quality decay, target-ID relations, local query routing, one-hop
  graph recall, and deterministic contradiction detection.
- **Visualizer** (`server/` + `static/`) - lightweight web UI, normally run in
  Docker on port 7777, with an optional systemd fallback on port 7778. 10 pages:
  Dashboard, Memories, Timeline,
  Tags, Duplicates, Conflicts, Embeddings, Clusters, Stale, and Graph.
- **Scripts** (`scripts/`) - relation migration, contradiction backfill,
  re-embedding, duplicate consolidation, and verification.
- **Docs** (`docs/`) - Setup guide and memory writing reference.

## Architecture

```
~/.hermes/plugins/lancedb/                       <- Canonical plugin copy
~/.hermes/hermes-agent/plugins/memory/lancedb/   <- Runtime compatibility copy
~/.hermes/lancedb/                               <- Persistent LanceDB tables
~/.hermes/lancedb-viz/                           <- Deployed visualizer files
```

The canonical user plugin survives Hermes Agent source-tree updates. The runtime
copy is retained for bundled-first discovery compatibility. Both are synced from
this repository's `plugin/` directory.

**Data flow:**

```
Agent → MCP tools (lancedb_search, lancedb_add, lancedb_update, ...)
  → MemoryWrite / MemoryPatch
    → memory_contract.py (normalize, validate, fingerprint, render)
      → LanceDBStore preflight (idempotency, subject match, conflicts)
        → Ollama /api/embed (nomic-embed-text, 768-dim)
          → Entity extraction + auto-tagging
            → LanceDB table (memories + vectors)
            → BM25 index (Tantivy FTS)
            → memory_edges table (typed relations with concrete target IDs)
            → memory_conflicts table (non-destructive key=value contradictions)
```

Search defaults to a deterministic local router: exact strings and identifiers
use lexical retrieval, relationship questions use hybrid seeds plus one-hop edge
traversal, and all other queries use hybrid BM25/vector retrieval. It adds no
model call and no service.

## Requirements

- **Hermes Agent** installed and running
- **Ollama** running on localhost:11434
- **nomic-embed-text** model pulled: `ollama pull nomic-embed-text`
- **Python 3.11+** with `lancedb`, `pyarrow`, `httpx`, `numpy`
- **Docker** for the primary visualizer deployment
- **systemd user services** only for the optional fallback deployment

## Quick Start

### 1. Install Ollama + embedding model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nomic-embed-text
```

### 2. Install the plugin

```bash
mkdir -p ~/.hermes/plugins/lancedb \
  ~/.hermes/hermes-agent/plugins/memory/lancedb
cp plugin/{store.py,memory_contract.py,__init__.py,plugin.yaml} \
  ~/.hermes/plugins/lancedb/
cp plugin/{store.py,memory_contract.py,__init__.py,plugin.yaml} \
  ~/.hermes/hermes-agent/plugins/memory/lancedb/

cd ~/.hermes/hermes-agent
venv/bin/pip install lancedb pyarrow httpx numpy
```

### 3. Configure Hermes

In `~/.hermes/config.yaml`:

```yaml
memory:
  provider: lancedb
  lancedb:
    db_path: ~/.hermes/lancedb
    embed_model: nomic-embed-text
```

Restart Hermes:

```bash
systemctl --user restart hermes-gateway
hermes tools | grep lancedb
```

### 4. Deploy the visualizer

The primary UI is the `lancedb-viz` Docker container on port 7777.
`deploy-local.sh` synchronizes repository files into both plugin locations and
the visualizer directory, then restarts that container. Use
`--systemd-fallback` to target the optional service on port 7778 instead.

```bash
./scripts/deploy-local.sh --dry-run
./scripts/deploy-local.sh
```

Dashboard: `http://localhost:7777`

For the full setup guide, see [docs/setup.md](docs/setup.md).

## MCP Tools

The plugin exposes 8 tools to the Hermes agent:

| Tool | Purpose |
|------|---------|
| `lancedb_search` | Auto-routed lexical, hybrid, or one-hop graph recall. Supports explicit `mode` and `relation_depth`. |
| `lancedb_add` | Create a validated memory from structured fields. Exact retries are idempotent; same-subject writes are preflighted before embedding. |
| `lancedb_update` | Replace a memory by ID from its complete structured form, re-embed changed content, and echo the replaced content. |
| `lancedb_delete` | Delete a memory and clean incoming and outgoing typed edges. |
| `lancedb_get` | Get a single memory by ID with all fields. |
| `lancedb_list` | Exact listing with category, tier, and quality filters. |
| `lancedb_graph` | Export the memory graph. |
| `lancedb_conflicts` | List deterministic same-subject `key=value` contradictions. |

### Strict write contract

`lancedb_add` and `lancedb_update` accept structured data. Callers must not
construct or parse the persisted string themselves.

```json
{
  "domain": "Project",
  "subject": "ApiGateway",
  "facts": ["transport=SSE", "port=7777", "reconnect uses exponential backoff"],
  "tier": 2,
  "category": "tech",
  "relations": [{"type": "depends", "target_id": "memory-id"}]
}
```

The five fields `domain`, `subject`, `facts`, `tier`, and `category` are
required. Facts may be dense `key=value` claims or concise single-sentence
prose. Current limits are 12 facts, 1,000 characters per fact, and 2,000
characters across all facts. These limits were selected against existing data;
the measured distribution and rationale are documented in
`plugin/memory_contract.py`.

- **Tier 1**: correction, safety rule, critical command, or recurring failure.
- **Tier 2**: reusable configuration, architecture, endpoint, or workflow fact.
- **Tier 3**: durable background context.
- **Categories**: `project`, `tech`, `fact`, `correction`, `user_pref`,
  `decision`, `insight`, `reference`, `pattern`, and `question`.
- **Relations**: `part_of`, `depends`, `requires`, `runs_on`, `connects_to`,
  `uses`, `extends`, `supersedes`, `invalidates`, and `contradicts`.

The contract normalizes labels and whitespace, rejects embedded tier or
relation markers, validates categories and relations, and renders the internal
canonical representation:

```
Domain:Subject fact one. fact two. [Tier=N]
```

Typed relations are persisted separately. The legacy `::relations::` syntax is
accepted only by fenced import and migration adapters, not by agent tools.

#### Non-destructive add behavior

`write_mode` defaults to `create`:

| Preflight result | Tool result |
|------------------|-------------|
| Exact canonical fingerprint already exists | `success: true`, `status: idempotent`, existing `memory_id` |
| Same subject, no conflicting explicit claim | `success: false`, `status: update_suggested`, existing ID and content |
| Same subject, conflicting `key=value` claim | Blocked with `error.code: conflicting_claims` |
| New subject | `success: true`, `status: created` |

`write_mode="upsert_subject"` is explicit and only succeeds when exactly one
non-conflicting same-subject memory exists. Its response includes
`replaced_content`. For intentional claim corrections, use `lancedb_update`
with a known memory ID. No write path silently merges or overwrites content.

Generic subjects are accepted with an `overly_broad_subject` warning because
they increase false conflict matches. Embedding failures are retryable errors
and never create a zero-vector row.

Validation failures use a stable machine-readable shape:

```json
{
  "success": false,
  "error": {
    "code": "invalid_category",
    "field": "category",
    "message": "category is not supported",
    "received": "note",
    "expected": ["project", "tech", "fact"]
  },
  "retryable": false
}
```

The `expected` value may contain the full supported set; it is abbreviated in
this example. Successful and preflight responses echo `canonical_content`.

See [docs/skills/memory-writing.md](docs/skills/memory-writing.md) for the full writing guide.

### Quality score

Dynamic score (0-1) computed at each access:

| Factor | Effect |
|--------|--------|
| Access frequency | +0.1 to +0.2 (>=2/>=5/>=10 accesses) |
| Entity links | +0.05 per link (max +0.15) |
| Creation freshness | +0.1 (<7d) / +0.05 (<30d) |
| **Decay curve** | **0.985^days since last access** (~50% after 46d, ~25% after 93d) |

Stale memories drop toward 0.1 but never hit 0. Re-accessing a memory resets the decay clock.
The stale view recomputes this score through the same `_compute_quality()`
function as detail access and exposes the previous stored value as
`persisted_quality`; the read-only maintenance view does not write it back.

### Mutation concurrency

All public store mutations share a process-local re-entrant lock keyed by the
resolved database path. The lock covers structured preflight and commit, so two
store instances in one process cannot both create the same canonical memory.
`_fresh()` keeps MVCC handles current, but neither mechanism provides atomicity
between separate processes. Cross-process writers must be externally
serialized; this limitation is explicit rather than inferred from MVCC.

### Auto-tagging

Tags are auto-extracted from entities at write time:

| Score | Type | Examples |
|-------|------|----------|
| 3 | Known tech keywords | hermes, docker, python, godot, steam |
| 2 | CamelCase projects / ALL CAPS acronyms | BloodReaver, VIGIL |
| 1 | Other non-blocked words | kept if space available |

A noise filter (~140+ words) blocks generic FR/EN words.

### Hybrid search

LanceDB native hybrid retrieval combines BM25 (Tantivy FTS) and vector cosine through Reciprocal Rank Fusion. A precision gate filters obvious low-score noise. Exact and relationship-shaped queries are routed locally without an LLM call.

## Visualizer

Lightweight web UI with 10 pages:

- **Dashboard** — total memories, category breakdown, tier distribution, top accessed
- **Memories** — paginated list (20/page) with filters (category, type, tag, quality, date, search)
- **Timeline** — memories grouped by day
- **Tags** — all tags with counts, rename/merge/delete operations
- **Duplicates** — near-duplicate groups by cosine similarity (threshold slider)
- **Conflicts** - explicit same-subject claim contradictions, with open/resolved filtering
- **Embeddings** — UMAP 2D projection of all memory vectors
- **Clusters** — semantic clusters (threshold + min size controls)
- **Stale** — old + low-quality memories (cleanup candidates)
- **Graph** — vis-network entity graph with freshness halo, tier filtering, typed edges

### API endpoints

```
GET  /api/dashboard          — enriched stats
GET  /api/memories           — paginated + filtered list
GET  /api/tags               — all tags with counts
GET  /api/timeline           — memories by day
GET  /api/duplicates         — duplicate groups (threshold param)
GET  /api/projection         — UMAP 2D projection
GET  /api/clusters           — semantic clusters
GET  /api/stale              — stale memories
GET  /api/conflicts          - contradiction ledger (`status`, `memory_id`, `limit`)
GET  /api/graph              — full graph (nodes + edges + typed_edges)
GET  /api/stats              — raw stats
POST /api/memories/:id       — update memory
POST /api/memories/:id/access — increment access count
POST /api/memories/bulk-delete
POST /api/memories/bulk-tag
POST /api/memories/bulk-type
POST /api/tags/rename | delete | merge
GET  /api/refresh            — reset store singleton
POST /api/export | import
```

## Scripts

### deploy-local.sh

Synchronizes the repository into the canonical user plugin, the runtime
compatibility copy, and the visualizer deployment. It does not restart the
Hermes gateway. By default it restarts and verifies Docker on port 7777; pass
`--systemd-fallback` to restart and verify the optional service on port 7778.

```bash
./scripts/deploy-local.sh --dry-run
./scripts/deploy-local.sh
./scripts/deploy-local.sh --dry-run --systemd-fallback
```

### audit-memory-format.py

Reads `id`, `content`, and `category` directly from an existing LanceDB table
and emits a JSON drift report. It never repairs rows. Use `--fail-on-drift` to
return exit code 1 when invalid or non-canonical content is found.

```bash
python3 scripts/audit-memory-format.py --db-path /path/to/lancedb --pretty
python3 scripts/audit-memory-format.py --db-path /path/to/lancedb --fail-on-drift
```

### migrate-memory-format.py

Reads projected fields and classifies rows as `canonical`, `auto_fix`,
`quarantine`, or `warning_only`. It is always a dry-run: `auto_fix` means a
change is mechanically unambiguous, not that the script applies it. There is
no write or apply mode.

```bash
python3 scripts/migrate-memory-format.py \
  --source-db /path/to/lancedb \
  --dry-run --pretty
```

Review the JSON report before planning a separately approved migration on a
verified copy. This hardening does not promote `domain`, `subject`, or `facts`
to LanceDB columns; the current table schema is unchanged. Any future schema
migration must use create, copy, verify, and drop—not `rename_table()`.

### migrate_graph_retention.py

Backfills concrete target IDs on legacy relations, removes source-orphaned edges on apply, and builds the deterministic conflict ledger. Dry-run is the default. Back up the database before `--apply`.

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/migrate_graph_retention.py
~/.hermes/hermes-agent/venv/bin/python scripts/migrate_graph_retention.py --apply
```

### reembed-entries.py

Re-embed all entries after batch content modifications (>5 entries).
Ollama must be running.

```bash
~/.hermes/hermes-agent/venv/bin/python3 scripts/reembed-entries.py [--dry-run]
```

### auto-merge-duplicates.py

Detect and merge duplicate memories by vector similarity.

```bash
# Dry run (default)
python3 scripts/auto-merge-duplicates.py --threshold 0.92

# Actually merge
python3 scripts/auto-merge-duplicates.py --threshold 0.92 --apply
```

Behavior:
- Detects groups with cosine similarity >= threshold (default 0.92)
- Picks the best entry (quality > access_count > content length > oldest)
- Merges tags from all duplicates into the keeper
- Deletes duplicates with exact content match (safe auto-merge)
- Skips duplicates with different content (needs manual review)

## Docker deployment

Docker on port 7777 is the primary visualizer deployment. The repository also
ships `systemd/lancedb-viz.service` as an optional local fallback on port 7778.

```yaml
# docker-compose.yml
services:
  lancedb-viz:
    build: .
    container_name: lancedb-viz
    ports:
      - "7777:7777"
    volumes:
      - ~/.hermes/lancedb:/home/hermes/.hermes/lancedb:rw
      - ~/.hermes/hermes-agent:/home/hermes/.hermes/hermes-agent:ro
      - ./static:/app/static:ro
      - ./server/server.py:/app/server.py:ro
    command: python3 server/server.py --port 7777 --host 0.0.0.0
    restart: unless-stopped
```

## License

MIT — see [LICENSE](LICENSE).
