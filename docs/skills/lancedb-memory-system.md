---
name: lancedb-memory-system
description: "LanceDB memory architecture, schema, viz, scripts, and pitfalls."
version: 5.0.0
triggers:
  - "lancedb memory"
  - "vector memory"
  - "lancedb rebuild"
  - "lance db"
  - "lancedb plugin wipe"
  - "graph shows nothing"
---

# LanceDB Memory System

Architecture and deployment of the local vector memory store.

## Architecture

```
~/.hermes/hermes-agent/plugins/memory/lancedb/   <- Plugin (store.py + __init__.py + plugin.yaml)
~/.hermes/lancedb/                                <- DB files (persistent data)
~/.hermes/lancedb-viz/                            <- Viz server + static files (bind-mounted in Docker)
```

- **Viz**: Docker container (`lancedb-viz:local`). Bind-mounts the DB (rw), plugin code (ro), and static files (ro).
- **Port**: 7777 (set via Docker CMD `--port 7777 --host 0.0.0.0`).
- **Store**: Hermes memory plugin. Enable via `memory.provider: lancedb` in `config.yaml`.
- **Frontend**: Vanilla JS SPA, 10 pages. Files: `static/index.html`, `static/app.js`, `static/graph.js`, `static/style.css`.

## Schema (17 columns)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| id | string | UUID | Unique identifier |
| content | string | - | Clean content (`::relations::` stripped) |
| category | string | "fact" | One of 10 categories |
| entities | string | "[]" | Extracted entities (JSON array) |
| links | string | "[]" | Links to other memories (JSON array) |
| relations | string | "[]" | Typed relations (JSON array) |
| tags | string | "[]" | Free tags (JSON array) |
| quality | float64 | 0.5 | Relevance score (0-1) |
| type | string | category | Granular sub-type |
| source | string | "" | Source |
| session_id | string | "" | Hermes session |
| user_id | string | "" | User |
| created_at | float64 | now | Creation timestamp |
| updated_at | float64 | now | Last update timestamp |
| access_count | int64 | 0 | Access counter |
| accessed_at | float64 | now | Last access |
| vector | list&lt;float32&gt;(768) | zeros | Embedding vector |

## Relations Table (`memory_edges`)

Separate LanceDB table, written automatically on each `add()`.

```
memory_edges
├── source_id: str          <- Source memory UUID
├── relation_type: str      <- "requires", "depends", "part_of", "runs_on", "connects_to", "uses", "extends"
├── target_label: str       <- Target memory label
├── created_at: float64     <- Timestamp
```

## FTS (BM25) + Hybrid Search

LanceDB 0.33+ native hybrid search via Tantivy FTS + vector cosine, fused with Reciprocal Rank Fusion (RRF).

- FTS index created automatically in `_init_table()` on the `content` column
- `search()` uses `query_type='hybrid'` with `.text(query).vector(vec)`
- Recall: ~97.9% (hybrid) vs 66.5% (BM25 only) vs 17.7% (vector only)

## Auto-Tagging

Tags extracted from entities at write time via `_select_tags()`:

| Score | Type | Examples |
|-------|------|----------|
| 3 | Known tech keywords | hermes, docker, python, godot, steam |
| 2 | CamelCase projects / ALL CAPS acronyms | BloodReaver, VIGIL |
| 1 | Other non-blocked words | kept if space available |

A noise filter (~140+ words) blocks generic FR/EN words.

## Quality Score

Dynamic score (0-1) computed at each access:

| Factor | Effect |
|--------|--------|
| Access frequency | +0.1 to +0.2 (>=2/>=5/>=10 accesses) |
| Entity links | +0.05 per link (max +0.15) |
| Freshness | +0.1 (<7d) / +0.05 (<30d) |
| Decay curve | 0.985^days since last access (~50% after 46d) |

Base = 0.5, clamped [0.1, 1.0]. Re-accessing resets the decay clock.

## Viz (localhost:7777)

### Frontend files

```
static/
├── style.css     — Neon OLED theme, glow system, Fira Code
├── index.html    — HTML skeleton: nav, topbar, 10 pages
├── graph.js      — Graph engine: vis-network, physics, typed edges, sidebar
└── app.js        — Navigation + pages: dashboard, memories, timeline, etc.
```

### 10 pages

Dashboard, Memories, Timeline, Tags, Duplicates, Embeddings (UMAP), Clusters, Stale, Graph (vis-network).

### Graph view

- Modes: Raw links (default), By category (hubs), By entity tags (hubs)
- Threshold slider: 0.30-1.0, step 0.05, default 0.8
- Tier checkboxes: T1/T2/T3 local filter
- Physics: forceAtlas2Based, gravConst=-40, centralGravity=0.005, springLength=160, damping=0.98
- 10 category colors (neon cyberpunk palette on OLED #080810 background)
- Freshness halo: glow intensity based on age (recent = large + opaque, old = small + faint)
- Typed edges visible only on node selection (purple glow on connected nodes)
- Ghost mode: tier-filtered nodes stay at 30% opacity if connected to visible nodes

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
GET  /api/graph              — full graph (nodes + edges + typed_edges)
GET  /api/stats              — raw stats
GET  /api/typed-edges        — typed relations
GET  /api/search             — search memories
GET  /api/export             — export all memories
POST /api/memories/:id       — update memory
POST /api/memories/:id/access — increment access count
POST /api/memories/bulk-delete
POST /api/memories/bulk-tag
POST /api/memories/bulk-type
POST /api/tags/rename | delete | merge
POST /api/import             — import memories
GET  /api/refresh            — reset store singleton
```

### Restart the container

```bash
docker restart lancedb-viz
```

Files are bind-mounted — changes to static files are instant. Restart needed only for server.py changes.

## Scripts

### reembed-entries.py

Re-embed all entries after batch content modifications (>5 entries). Ollama must be running.

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

## DB Maintenance

### Compact old versions

LanceDB keeps every version on every write. Compact after batches > 20:

```python
import lancedb
from datetime import timedelta
db = lancedb.connect("~/.hermes/lancedb")
tbl = db.open_table("memories")
tbl.cleanup_old_versions(timedelta(seconds=0))
```

## Pitfalls

- `tbl.update()` with a list → AttributeError. Always loop with `where=`.
- `rename_table()` not supported in LanceDB OSS (NotImplementedError). To migrate schema: create new table, copy data, drop old.
- `tags: null` crashes `get_tags()`. Always null-guard: `if tags is None: tags = []`.
- LanceDB is not fork-safe — suppress the warning via `warnings.filterwarnings("ignore")` before import.
- Docker container can't reach Ollama on `localhost:11434`. Use `OLLAMA_HOST=http://host.docker.internal:11434` or the host's LAN IP.
- Missing deps in venv → plugin initializes silently with `self._store = None`. All tools return `NoneType` errors. Diagnose with `venv/bin/python -c "import lancedb; import pyarrow; print('OK')"`.
- If `__init__.py` loses the `from .store import LanceDBStore` import, same silent NoneType failure. Check agent logs for `name 'LanceDBStore' is not defined`.
- `::relations::` can appear in content text as a word. The parser uses the LAST occurrence (after `[Tier=N]`) to avoid false positives.
- Frontend: if `const` is declared in both `graph.js` and `app.js` (e.g. `catColors`), the browser throws `SyntaxError: Identifier has already been declared` and ALL JS dies silently. `app.js` must NOT redefine consts from `graph.js`.
- Frontend: any function using `await` must be `async function`. Missing `async` kills the entire JS silently.
- Frontend: orphaned `document.getElementById()` refs (IDs removed during layout changes) cause silent TypeError that blocks rendering.