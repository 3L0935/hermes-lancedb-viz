# Contributing

## Setup

```bash
git clone https://github.com/3L0935/hermes-lancedb-viz.git
cd hermes-lancedb-viz
```

You need Hermes Agent, Ollama, and Docker. See [docs/setup.md](docs/setup.md) for the full guide.

## Reproducible tests from a clean checkout

Hermes supplies the provider base classes and the tested Python environment.
Replace `/path/to/hermes-agent` with that checkout, then run from this repository:

```bash
PYTHONPATH=$PWD:/path/to/hermes-agent /path/to/hermes-agent/venv/bin/python -m pytest -q tests
node --check static/app.js
node --check static/graph.js
```

The Python command is the release gate. Ollama, the visualizer container, and a
live LanceDB database are not required; tests create temporary synthetic data.

## Project structure

```
plugin/          LanceDB memory provider (store.py, __init__.py, plugin.yaml)
server/          Viz backend (Python HTTP server)
static/          Viz frontend (vanilla JS SPA: app.js, graph.js, index.html, style.css)
scripts/         Utils (reembed, auto-merge duplicates, verify setup, docker-run)
docs/            Setup guide + skill references
```

## Making changes

### Plugin (store.py, __init__.py)

The plugin runs inside Hermes' venv. Test imports after any change:

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -c "from plugins.memory.lancedb import LanceDBMemoryProvider; print('OK')"
```

### Visualizer (server/ + static/)

The canonical viz runs in the Hub-managed Docker container on port 7777. Start
or recreate it from `~/github/hermes-hub/services/lancedb-viz/` with
`docker compose up -d`. Synchronize repository changes with:

```bash
./scripts/deploy-local.sh --dry-run
./scripts/deploy-local.sh
```

After changes:

- `static/` files (HTML/CSS/JS): instant, just refresh the browser
- `server/server.py` or `server/maintenance.py`: the deploy script restarts `lancedb-viz`
- `plugin/`: restart `hermes-gateway` separately; the deploy script cannot reload an imported Python module

The disabled systemd service on port 7778 is a recovery fallback, not the
normal contributor deployment.

### Frontend pitfalls

The SPA is split across 4 files. Two rules to avoid silent JS deaths:

1. **Never duplicate `const` declarations** between `graph.js` and `app.js`. If `catColors` is declared in `graph.js`, don't redeclare it in `app.js` — the browser throws `SyntaxError: Identifier has already been declared` and ALL JS dies silently.
2. **Any function using `await` must be `async function`**. Missing `async` kills the entire script with no visible error in the UI.

### Scripts

Test scripts against your local DB before committing:

```bash
~/.hermes/hermes-agent/venv/bin/python3 scripts/reembed-entries.py --dry-run
python3 scripts/auto-merge-duplicates.py --threshold 0.92
```

## Commit style

```
type: concise subject line

Types: fix, feat, refactor, docs, chore
```

## Before pushing

1. `./scripts/deploy-local.sh --dry-run` — inspect the exact sync/restart actions
2. `./scripts/deploy-local.sh` — synchronize and restart the canonical container
3. `systemctl --user restart hermes-gateway.service` — required when `plugin/` changed
4. `curl -s http://localhost:7777/api/stats | python3 -m json.tool` — API responds
5. `./scripts/verify-setup.sh` — smoke test passes
6. No personal references (paths, usernames, project names) in docs or code comments

## License

By contributing, you agree your changes are licensed under MIT.
