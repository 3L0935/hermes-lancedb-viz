# Setup — Install LanceDB Memory + Visualizer

Complete guide from zero to a local vector memory store with interactive visualization.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Install the Plugin](#2-install-the-plugin)
3. [Configure Hermes](#3-configure-hermes)
4. [Ollama Embeddings](#4-ollama-embeddings)
5. [Database](#5-database)
6. [Visualizer (Docker)](#6-visualizer-docker)
7. [Smoke Test](#7-smoke-test)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites

| Tool | Min Version | Why |
|------|-------------|-----|
| Python | 3.11+ | LanceDB + Arrow |
| Docker | 24+ | Viz container |
| Ollama | 0.3+ | Local embeddings |
| Hermes Agent | 1.x+ | Memory provider plugin |

```bash
python3 --version
docker --version
ollama --version
ollama pull nomic-embed-text
```

## 2. Install the Plugin

Copy the plugin files to Hermes plugins directory:

```bash
mkdir -p ~/.hermes/hermes-agent/plugins/memory/lancedb/
cp plugin/store.py plugin/__init__.py plugin/plugin.yaml \
   ~/.hermes/hermes-agent/plugins/memory/lancedb/
```

Install Python dependencies in Hermes venv:

```bash
cd ~/.hermes/hermes-agent
venv/bin/pip install lancedb pyarrow httpx numpy
```

Verify:

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -c "
from plugins.memory.lancedb import LanceDBMemoryProvider
from plugins.memory.lancedb.store import LanceDBStore
print('Plugin OK — imports successful')
"
```

## 3. Configure Hermes

In `~/.hermes/config.yaml`, add:

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

# Verify tools are loaded
hermes tools | grep lancedb
# → lancedb_search  lancedb_add  lancedb_graph  lancedb_delete  lancedb_update  lancedb_get  lancedb_list
```

## 4. Ollama Embeddings

The store calls Ollama via HTTP for embeddings.

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `LANCE_EMBED_MODEL` | `nomic-embed-text` | Model (768d) |

Keep the model warm:

```bash
ollama run nomic-embed-text --keep-alive 30m
```

### From Docker (viz)

The container needs `host.docker.internal` to reach Ollama on the host:

```yaml
# docker-compose.yml
environment:
  - OLLAMA_HOST=http://host.docker.internal:11434
```

On Linux without Docker Desktop, use your host's LAN IP instead.

## 5. Database

The store creates the database automatically on first launch.

```
~/.hermes/lancedb/
├── memories.lance/     # Main table (LanceDB columnar format)
└── memory_edges/       # Typed relations table
```

### Compact old versions

LanceDB keeps every version on every write. Compact after batches > 20:

```python
import lancedb
from datetime import timedelta
db = lancedb.connect("~/.hermes/lancedb")
tbl = db.open_table("memories")
tbl.cleanup_old_versions(timedelta(seconds=0))
```

### Re-embedding

After batch content modifications (>5 entries), re-embed:

```bash
~/.hermes/hermes-agent/venv/bin/python3 scripts/reembed-entries.py
```

## 6. Visualizer (Docker)

### Build

```bash
cd hermes-lancedb-viz
docker build -t lancedb-viz:local .
```

### Run

```bash
docker compose up -d
```

Or with the wrapper script:

```bash
./scripts/docker-run.sh
```

### Bind mounts

| Host | Container | Mode |
|------|-----------|------|
| `~/.hermes/lancedb` | `/home/hermes/.hermes/lancedb` | rw |
| `~/.hermes/hermes-agent` | `/home/hermes/.hermes/hermes-agent` | ro |
| `./static` | `/app/static` | ro |
| `./server/server.py` | `/app/server.py` | ro |

### Access

```
http://localhost:7777
```

### Healthcheck

```bash
curl -s http://localhost:7777/api/stats | python3 -m json.tool
```

## 7. Smoke Test

```bash
./scripts/verify-setup.sh
```

Checks:
- Container running
- HTTP 200 on `/`
- API `/api/stats` returns JSON with `total_memories > 0`
- API `/api/dashboard` works
- All static files served (app.js, graph.js, style.css, vis-network.min.js)

## 8. Troubleshooting

### Plugin not found

```
/api/stats → {"error": "LanceDB plugin not found"}
```

Causes:
1. `store.py` / `__init__.py` missing → copy from `plugin/` directory
2. Python deps missing → `venv/bin/pip install lancedb pyarrow`
3. `__init__.py` missing `from .store import LanceDBStore` → add the import
4. Venv misconfigured → verify with `venv/bin/python -c "from plugins.memory.lancedb import LanceDBMemoryProvider; print('OK')"`

LanceDB data is never lost — the columnar format persists even if plugin files are deleted.

### Ollama unreachable from Docker

```bash
docker exec lancedb-viz curl -s http://host.docker.internal:11434/api/tags
```

If it fails, use your host's LAN IP: `OLLAMA_HOST=http://192.168.x.x:11434`

### DB size bloating

LanceDB keeps old versions. See [Compact old versions](#compact-old-versions) above.

### Tags return null

```json
{"error": "'NoneType' object is not iterable"}
```

Some entries have `tags: null`. Backfill:

```python
store = LanceDBStore(Path("~/.hermes/lancedb"))
table = store._table
df = table.to_arrow().to_pandas()
for _, row in df.iterrows():
    if row.get("tags") is None:
        table.update(where=f'id="{row["id"]}"', values={"tags": "[]"})
```

### Dashboard stuck on "Loading..."

Open the browser console (F12). Common causes:
1. `SyntaxError: Identifier 'catColors' has already been declared` → const duplicated between graph.js and app.js
2. `await is only valid in async function` → function missing `async`
3. HTTP error on API fetch → check server logs: `docker logs lancedb-viz`

---

*Repo: [hermes-lancedb-viz](https://github.com/3L0935/hermes-lancedb-viz)*