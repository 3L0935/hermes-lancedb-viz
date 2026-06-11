# Setup — Installer LanceDB + Viz dans Hermes Agent

Guide complet de zéro à un store vectoriel local avec visualisation interactive.

---

## Table des matières

1. [Prérequis](#1-prérequis)
2. [Plugin LanceDB](#2-plugin-lancedb)
3. [Config Hermes](#3-config-hermes)
4. [Ollama Embeddings](#4-ollama-embeddings)
5. [Base de données](#5-base-de-données)
6. [Visualiseur Docker](#6-visualiseur-docker)
7. [Smoke Test](#7-smoke-test)
8. [Dépannage](#8-dépannage)

---

## 1. Prérequis

| Outil | Version min | Raison |
|-------|-------------|--------|
| Python | 3.11+ | LanceDB + Arrow |
| Docker | 24+ | Viz container |
| Ollama | 0.3+ | Embeddings locaux |
| Hermes Agent | 1.x+ | Provider memory plugin |

Vérifications :

```bash
python3 --version
docker --version
ollama --version
ollama pull nomic-embed-text    # Modèle d'embedding
```

## 2. Plugin LanceDB

Les fichiers plugin se trouvent dans `hermes-agent/plugins/memory/lancedb/`.

### Fichiers requis

```
hermes-agent/plugins/memory/lancedb/
├── plugin.yaml         # Metadata du plugin
├── __init__.py         # MemoryProvider — expose 4 tools Hermes
└── store.py            # LanceDBStore — CRUD, embeddings, entités, liens
```

### Installation

```bash
cd ~/.hermes/hermes-agent

# S'assurer que les fichiers existent
ls plugins/memory/lancedb/
# → plugin.yaml  __init__.py  store.py

# Installer les dépendances Python
uv pip install lancedb pyarrow numpy pyyaml
```

### Vérification

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -c "
from plugins.memory.lancedb import LanceDBMemoryProvider
from plugins.memory.lancedb.store import LanceDBStore
print('Plugin OK — imports réussis')
"
```

## 3. Config Hermes

### `~/.hermes/config.yaml`

```yaml
memory:
  provider: lancedb
  lancedb:
    path: ~/.hermes/lancedb          # Où stocker la DB
    embed_model: nomic-embed-text     # Modèle Ollama
    embed_dim: 768                    # Dimension du vecteur
```

> **Attention :** Le changement de provider nécessite un restart du gateway Hermes.

### Redémarrer Hermes

```bash
systemctl --user restart hermes-gateway

# Vérifier que les 4 tools sont chargés
hermes tools | grep lancedb
# → lancedb_search  lancedb_add  lancedb_graph  lancedb_delete
```

## 4. Ollama Embeddings

Le store appelle Ollama via HTTP pour générer les embeddings. La config par défaut :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `OLLAMA_HOST` | `http://localhost:11434` | URL du serveur Ollama |
| `LANCE_EMBED_MODEL` | `nomic-embed-text` | Modèle (768d) |

### Optimisations

```bash
# Garder le modèle chaud pour éviter les latences
ollama run nomic-embed-text --keep-alive 30m

# Ou via service systemd — ajouter dans la config Ollama
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment=OLLAMA_KEEP_ALIVE=30m
```

### Depuis Docker (viz)

Si le conteneur viz tourne sur la même machine, utiliser `host.docker.internal` :

```yaml
# docker-compose.yml
environment:
  - OLLAMA_HOST=http://host.docker.internal:11434
```

## 5. Base de données

Le store crée automatiquement la base au premier lancement.

### Emplacement

```
~/.hermes/lancedb/
├── memories.lance/     # Table principale (LanceDB columnar format)
└── memory_edges/       # Table des relations typées
```

### Schéma (17 colonnes)

| Champ | Type | Description |
|-------|------|-------------|
| `id` | string | UUID v4 |
| `content` | string | Texte nettoyé (`::relations::` strippé) |
| `category` | string | `tech`, `correction`, `fact`, `project`, `pattern`, `user_pref`, `decision`, `insight`, `reference`, `question` |
| `entities` | string | JSON array — extraites automatiquement |
| `links` | string | JSON array — liens cosine similarity |
| `relations` | string | JSON array — relations typées parsées |
| `tags` | string | JSON array — tags auto-générés depuis entités |
| `quality` | float64 | Score 0-1 (auto : accès + liens + fraîcheur) |
| `type` | string | Sous-type (default = category) |
| `source` | string | Source Hermes |
| `session_id` | string | Session d'origine |
| `user_id` | string | Utilisateur |
| `created_at` | float64 | Timestamp création |
| `updated_at` | float64 | Timestamp MAJ |
| `access_count` | int64 | Compteur de lectures |
| `accessed_at` | float64 | Dernier accès |
| `vector` | float32[768] | Embedding vectoriel |

### Compactage

LanceDB crée une version à chaque écriture. Compacter régulièrement :

```python
import lancedb
from datetime import timedelta

db = lancedb.connect("~/.hermes/lancedb")
tbl = db.open_table("memories")
tbl.cleanup_old_versions(timedelta(seconds=0))
# 596 MB → ~4 MB pour 224 entrées
```

À faire après tout batch > 20 écritures.

### Ré-embedding

```bash
cd ~/.hermes/hermes-agent
venv/bin/python scripts/reembed-entries.py
```

> Ce script est livré dans le repo sous `scripts/reembed-entries.py`. Nécessaire après des modifications batch du contenu (les vecteurs ne se mettent pas à jour automatiquement).

## 6. Visualiseur Docker

### Build

```bash
cd hermes-lancedb-viz
docker build -t lancedb-viz:local .
```

### Run

```bash
docker compose up -d
```

Ou avec le script wrapper :

```bash
./scripts/docker-run.sh
```

### Bind mounts

Le conteneur a besoin de 3 chemins hôtes :

| Hôte | Conteneur | Mode |
|------|-----------|------|
| `~/.hermes/lancedb` | `/home/hermes/.hermes/lancedb` | **rw** |
| `~/.hermes/hermes-agent` | `/home/hermes/.hermes/hermes-agent` | ro |
| `~/.hermes/lancedb-viz/static` | `/app/static` | ro |

> **Important :** Les fichiers static sont en `ro`. Pour déployer une modif, éditer sur l'hôte puis `docker restart lancedb-viz`.

### Accès

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

Tests effectués :
- ✓ Conteneur tourne
- ✓ HTTP 200 sur `/`
- ✓ API `/api/stats` retourne JSON avec `total_memories > 0`
- ✓ API `/api/dashboard` fonctionne
- ✓ Tous les static files servis (app.js, graph.js, style.css, vis-network.min.js)

## 8. Dépannage

### Plugin non trouvé

```
/api/stats → {"error": "LanceDB plugin not found"}
```

**Causes :**
1. `store.py` / `__init__.py` supprimés → reconstruire les 3 fichiers
2. Dépendances Python manquantes → `uv pip install lancedb pyarrow`
3. `__init__.py` sans `from .store import LanceDBStore` → ajouter l'import
4. Venv mal configuré → vérifier avec `venv/bin/python -c "from plugins.memory.lancedb import LanceDBMemoryProvider; print('OK')"`

**Les données LanceDB ne sont jamais perdues** — format columnaire Rust, persistant même si les fichiers plugin sont effacés.

### Ollama injoignable depuis Docker

```bash
# Tester depuis le conteneur
docker exec lancedb-viz curl -s http://host.docker.internal:11434/api/tags

# Si ça échoue, vérifier le host
docker exec lancedb-viz ping -c 1 host.docker.internal
```

Solution : utiliser `OLLAMA_HOST=http://<IP_LAN>:11434` si `host.docker.internal` n'est pas disponible (Linux pur sans Docker Desktop).

### DB size qui gonfle

Causes : versions LanceDB non compactées + écritures batch.

Fix : voir [section Compactage](#compactage).

Cycle recommandé : compacter 1×/semaine si usage quotidien.

### Tags retournent null

```json
{"error": "'NoneType' object is not iterable"}
```

Backfill :

```python
store = LanceDBStore(Path("~/.hermes/lancedb"))
table = store._table
df = table.to_arrow().to_pandas()
for _, row in df.iterrows():
    if row.get("tags") is None:
        ids = row["id"]
        table.update(where=f'id="{ids}"', values={"tags": "[]"})
```

### Le dashboard reste sur "Loading..."

Ouvrir la console navigateur (F12). Causes possibles :
1. `SyntaxError: Identifier 'catColors' has already been declared` → doublon `const` entre `graph.js` et `app.js`
2. `await is only valid in async function` → une fonction avec `await` sans `async`
3. Erreur HTTP sur un fetch API → vérifier que le serveur répond

---

*Documentation du repo [hermes-lancedb-viz](https://github.com/3L0935/hermes-lancedb-viz).*