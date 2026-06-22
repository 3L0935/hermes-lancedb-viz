# Hermes LanceDB Memory — Plugin + Visualizer

Local-first vector memory for [Hermes Agent](https://github.com/nousresearch/hermes-agent). 
SQLite-free, LanceDB-only storage with Ollama embeddings, entity extraction, 
hybrid search (BM25 + vector), and an interactive web dashboard.

## What this is

A complete memory system for Hermes Agent that persists across sessions:

- **Plugin** (`plugin/`) — LanceDB memory provider for Hermes. 7 MCP tools: 
  search, add, update, delete, get, list, graph. Auto entity extraction, 
  auto-tagging, quality scoring with decay curve, typed relations, hybrid search.
- **Visualizer** (`server/` + `static/`) — Docker-hosted web UI on port 7777. 
  10 pages: Dashboard, Memories, Timeline, Tags, Duplicates, Embeddings (UMAP), 
  Clusters, Stale, Graph (vis-network). Neon OLED theme.
- **Scripts** (`scripts/`) — Re-embedding, auto-merge duplicates, verification.

## Architecture

```
~/.hermes/hermes-agent/plugins/memory/lancedb/   <- Plugin (store.py + __init__.py + plugin.yaml)
~/.hermes/lancedb/                                 <- LanceDB database files (persistent data)
~/.hermes/lancedb-viz/                             <- Viz server + static files (bind-mounted in Docker)
```

**Data flow:**

```
Agent → MCP tools (lancedb_search, lancedb_add, lancedb_update, ...) 
  → LanceDBStore (store.py)
    → LanceDB table (memories + vectors)
    → Ollama /api/embed (nomic-embed-text, 768-dim)
    → Entity extraction + auto-tagging
    → BM25 index (Tantivy FTS)
    → memory_edges table (typed relations)
```

## Requirements

- **Hermes Agent** installed and running
- **Ollama** running on localhost:11434
- **nomic-embed-text** model pulled: `ollama pull nomic-embed-text`
- **Python 3.11+** with `lancedb`, `pyarrow`, `httpx`, `numpy`
- **Docker** (for the visualizer only)

## Setup

### 1. Install Ollama + embedding model

```bash
# Install Ollama (if not already)
curl -fsSL https://ollama.com/install.sh | sh

# Pull the embedding model
ollama pull nomic-embed-text
```

### 2. Install the plugin

Copy the plugin files to Hermes plugins directory:

```bash
cp plugin/store.py plugin/__init__.py plugin/plugin.yaml \
   ~/.hermes/hermes-agent/plugins/memory/lancedb/
```

Install Python dependencies in Hermes venv:

```bash
cd ~/.hermes/hermes-agent
venv/bin/pip install lancedb pyarrow httpx numpy
```

### 3. Configure Hermes

In `~/.hermes/config.yaml`, add:

```yaml
memory:
  provider: lancedb
  lancedb:
    db_path: ~/.hermes/lancedb
    embed_model: nomic-embed-text
```

### 4. Deploy the visualizer (optional)

```bash
cd ~/github/hermes-lancedb-viz
docker build -t lancedb-viz:local .
docker compose up -d
```

The dashboard runs on `http://localhost:7777`.

## MCP Tools

The plugin exposes 7 tools to the Hermes agent:

| Tool | Purpose |
|------|---------|
| `lancedb_search` | Hybrid search (BM25 + vector + RRF fusion). Returns ranked results with quality score and relations. |
| `lancedb_add` | Store a new memory. Structured fields (domain, subject, tier, category) or legacy content string. Auto-extracts entities, auto-tags, builds links. |
| `lancedb_update` | Edit an existing memory in-place by ID. Updates content (re-embeds), category, tags, quality, or type. Prefer over delete+recreate. |
| `lancedb_delete` | Delete a memory by ID. Rebuilds remaining links. |
| `lancedb_get` | Get a single memory by ID with all fields (content, category, quality, tier, tags, entities, relations, links). |
| `lancedb_list` | List all memories with filters (category, tier, quality_min). No approximate search — exact listing. |
| `lancedb_graph` | Export the full memory knowledge graph as nodes + edges. |

### Memory format

```
Domain:Subject key=value key=value. [Tier=N]
::relations:: type=target | type=target2
```

- **Domain**: Namespace (Hermes, Projet, Tech, User, Correction, Config, etc.)
- **Subject**: Specific subject within the domain
- **Tier**: 1=critical (bugs, corrections), 2=useful (stack, URLs), 3=contextual
- **Category**: project, tech, fact, correction, user_pref, decision, insight, reference, pattern, question
- **Relations**: Optional typed links (part_of, depends, requires, runs_on, connects_to, uses, extends)

### Quality score

Dynamic score (0-1) computed at each access:

| Factor | Effect |
|--------|--------|
| Access frequency | +0.1 to +0.2 (≥2/≥5/≥10 accesses) |
| Entity links | +0.05 per link (max +0.15) |
| Creation freshness | +0.1 (<7d) / +0.05 (<30d) |
| **Decay curve** | **0.985^days since last access** (~50% after 46d, ~25% after 93d) |

Stale memories drop toward 0.1 but never hit 0. Re-accessing a memory resets the decay clock.

### Auto-tagging

Tags are auto-extracted from entities at write time via `_select_tags()`:

| Score | Type | Examples |
|-------|------|----------|
| 3 | Known tech keywords | hermes, docker, python, godot, steam |
| 2 | CamelCase projects / ALL CAPS acronyms | BloodReaver, SpawnDirector, VIGIL |
| 1 | Other non-blocked words | kept if space available |

A noise filter (~140+ words) blocks generic FR/EN words (verbs, adverbs, common nouns).

### Hybrid search

LanceDB 0.33+ native hybrid: BM25 (Tantivy FTS) + vector cosine, fused via Reciprocal Rank Fusion (RRF). 
Precision gate filters results with RRF score < 0.005 (noise threshold). 
Recall: ~97.9% (vector + BM25 combined) vs 66.5% BM25-only vs 17.7% vector-only.

## Visualizer

Docker container with:

- **Dashboard** — total memories, category breakdown, tier distribution, top accessed
- **Memories** — paginated list (20/page) with filters (category, type, tag, quality, date, search)
- **Timeline** — memories grouped by day
- **Tags** — all tags with counts, rename/merge/delete operations
- **Duplicates** — near-duplicate groups by cosine similarity (threshold slider)
- **Embeddings** — UMAP 2D projection of all memory vectors
- **Clusters** — semantic clusters (threshold + min size controls)
- **Stale** — old + low-quality memories (cleanup candidates)
- **Graph** — vis-network entity graph with freshness halo, tier filtering, typed edges, ghost mode

### API endpoints

```
GET  /api/dashboard          — enriched stats
GET  /api/memories           — paginated + filtered list
GET  /api/tags               — all tags with counts
GET  /api/timeline           — memories by day
GET  /api/duplicates          — duplicate groups (threshold param)
GET  /api/projection          — UMAP 2D projection
GET  /api/clusters            — semantic clusters
GET  /api/stale               — stale memories
GET  /api/graph               — full graph (nodes + edges + typed_edges)
GET  /api/stats               — raw stats
POST /api/memories/:id        — update memory
POST /api/memories/:id/access — increment access count
POST /api/memories/bulk-delete
POST /api/memories/bulk-tag
POST /api/memories/bulk-type
POST /api/tags/rename | delete | merge
GET  /api/refresh             — reset store singleton
POST /api/export | import
```

## Scripts

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

## Docker

```yaml
# docker-compose.yml
services:
  lancedb-viz:
    build: .
    container_name: lancedb-viz
    ports:
      - "7777:7777"
    volumes:
      - ~/.hermes/lancedb:/home/hermes/.hermes/lancedb
      - ~/.hermes/hermes-agent:/home/hermes/.hermes/hermes-agent:ro
      - ./server:/app/server:ro
      - ./static:/app/static:ro
    command: python3 server/server.py --port 7777 --host 0.0.0.0
    restart: unless-stopped
```

Bind-mounts: the DB, Hermes plugin code, and viz files are mounted read-only 
(except the DB which needs write access for the refresh endpoint).

## License

Private project. Not for redistribution.