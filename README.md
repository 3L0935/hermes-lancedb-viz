# hermes-lancedb-viz

> Visualiseur de mémoire vectorielle pour **Hermes Agent** — graphe de connaissances néon, timeline, tags, clusters, et plus.

![screenshot](https://img.shields.io/badge/stack-LanceDB%20%2B%20Python%20%2B%20vis.js-blueviolet)

## Stack

```
hermes-agent/plugins/memory/lancedb/store.py  ← LanceDBStore (CRUD, embeddings, entities)
                          ↓
hermes-lancedb-viz/server/server.py           ← HTTP API + static files
                          ↓
         Browser ← HTML/CSS/JS (vis-network, SPA vanilla)
```

- **LanceDB** (fichier local) : stockage vectoriel avec embeddings Ollama (nomic-embed-text)
- **Hermes `LanceDBStore`** : extraction d'entités, liens sémantiques, relations typées, Tiers
- **Server Python** : API REST + serveur de fichiers statiques (zero dépendance framework)
- **Frontend** : SPA vanilla avec vis-network pour le graphe interactif

## Quick Start

### Prérequis

- Docker (ou Python 3.11+ sur l'hôte)
- Ollama avec `nomic-embed-text` installé
- Hermes Agent avec un store LanceDB existant (`~/.hermes/lancedb/`)

### Docker (recommandé)

```bash
# Builder
docker build -t lancedb-viz:local .
# Lancer
docker compose up -d
# Ouvrir → http://localhost:7777
```

### À la main (hôte)

```bash
pip install -r requirements.txt
python server/server.py --port 7777 --host 0.0.0.0
```

## Architecture des données

### LanceDB Store (`store.py`)

Le store est importé depuis `hermes-agent/plugins/memory/lancedb/store.py`. C'est lui qui gère :

| Fonction | Détail |
|----------|--------|
| Embeddings | Appel HTTP à Ollama (`nomic-embed-text`, 768d) |
| Entity extraction | Regex CamelCase/TitleCase + liste de 80+ tech keywords |
| Link building | Cosine similarity entre vecteurs |
| Tiers | 1=critique, 2=utile, 3=contextuel |
| Relations typées | `::relations::` block parsé → `memory_edges` table |
| Tags + Quality | Automatiques depuis entités, accès, fraîcheur |

### Server (`server.py`)

16 endpoints REST, sans framework (stdlib `http.server`) :

| Endpoint | Description |
|----------|-------------|
| `GET /api/stats` | Stats globales (total, catégories, tiers, âges, accès) |
| `GET /api/dashboard` | Dashboard complet (catégories, top tags, top accessed, bar charts) |
| `GET /api/memories` | Liste paginée avec filtres (search, category, tier, type) |
| `GET /api/memory?id=` | Détail d'une mémoire (content, entities, tags, relations, edges) |
| `GET /api/search?q=` | Recherche sémantique avec scores |
| `GET /api/graph` | Graphe complet (nodes + edges) avec option seuil |
| `GET /api/tags` | Tous les tags avec leur fréquence |
| `GET /api/timeline` | Mémoires groupées par jour |
| `GET /api/duplicates?threshold=` | Groupes de mémoires similaires |
| `GET /api/clusters` | Clusters par similarité cosinus |
| `GET /api/stale?days=&quality_max=` | Mémoires anciennes + faible qualité |
| `GET /api/projection` | UMAP projection 2D des embeddings |
| `POST /api/delete` | Supprimer une mémoire |
| `POST /api/memories/bulk-delete` | Suppression batch |
| `POST /api/tags/delete` | Supprimer un tag |

### Frontend — Pages

| Page | Fichier | Fonctionnalité |
|------|---------|----------------|
| **Dashboard** | `app.js:loadDashboard()` | Stats cards, bar charts (catégories, tiers, tags), top accessed |
| **Memories** | `app.js:loadMemories()` | Tableau paginé, filtres (search/cat/tier/type), checkbox + bulk delete |
| **Graph** | `graph.js:loadGraph()` | Graphe interactif forceAtlas2Based, catégories hubs, edge types, sidebar détail |
| **Timeline** | `app.js:loadTimeline()` | Ligne temporelle, filtre par catégorie |
| **Tags** | `app.js:loadTags()` | Nuage trié par fréquence, delete |
| **Duplicates** | `app.js:loadDuplicates()` | Groupes de mémoires similaires, delete individuel |
| **Clusters** | `app.js:loadClusters()` | Clusters nommés (common entities), paramétrables (threshold, min_size) |
| **Stale** | `app.js:loadStale()` | Mémoires vieilles + faible qualité avec delete all |
| **Embedding** | `app.js:loadEmbedding()` | UMAP projection 2D sur canvas avec hover/click |

## Configuration

### Variables d'environnement

| Variable | Défaut | Description |
|----------|--------|-------------|
| `OLLAMA_HOST` | `http://localhost:11434` | Host Ollama pour les embeddings |
| `HERMES_HOME` | `~/.hermes` | Chemin du store LanceDB |
| `LANCE_EMBED_MODEL` | `nomic-embed-text` | Modèle d'embedding Ollama |

### Bind mounts Docker

| Hôte | Conteneur | Mode |
|------|-----------|------|
| `~/.hermes/lancedb` | `/home/hermes/.hermes/lancedb` | **rw** |
| `~/.hermes/hermes-agent` | `/home/hermes/.hermes/hermes-agent` | ro |
| `~/.hermes/lancedb-viz/static` | `/app/static` | ro |
| `~/.hermes/lancedb-viz/server.py` | `/app/server.py` | ro |

> **Note**: Le static et le server sont en `ro` — pour déployer un changement, éditer le fichier source sur l'hôte et **redémarrer** le conteneur (`docker restart lancedb-viz`).

## Format des mémoires

### Structure canonique

```
Domaine:Sujet Contexte autosuffisant. clé=valeur. [Tier=N]
::relations:: type=cible | type=cible2
```

Exemple :
```
Tech:Ollama Embeddings nomic-embed-text=768d keep_alive=30s. [Tier=1]
::relations:: depends=Ollama | part_of=Hermes Memory
```

### Règles

| Règle | Pourquoi |
|-------|----------|
| `Domaine:Sujet` en tête | Clustering par domaine |
| Autosuffisant | Chaque entrée est compréhensible sans contexte externe |
| `[Tier=N]` **obligatoire** | 1=critique, 2=utile, 3=contextuel |
| Max ~200 chars | Lit en 2 secondes |
| Pas d'extension fichier | Les `.py`, `.yaml`, `.js` tuent l'extraction d'entités |
| Pas de parenthèses | `get_enabled()` → `get_enabled` pour les entités |

### Catégories disponibles

| Catégorie | Usage | Priorité |
|-----------|-------|----------|
| `correction` | Bugs, erreurs, fixes, pièges | 1 (toujours si bug) |
| `pattern` | Workflows récurrents, procédures | 2 |
| `decision` | Décisions architecturales | 3 |
| `user_pref` | Style, préférences | 4 |
| `reference` | Liens, docs externes | 5 |
| `insight` | Découvertes, observations | 6 |
| `project` | Contexte projet actif | 7 |
| `tech` | Technique pure (ports, commandes) | 8 |
| `fact` | Faits stables, architecture | 9 |
| `question` | Questions ouvertes, todo | 10 |

### Relations typées

```
::relations:: type=cible | type=cible | ...
```

Types disponibles : `requires`, `depends`, `runs_on`, `connects_to`, `part_of`, `uses`, `extends`

Les relations sont parsées par le store au moment de `lancedb_add()` et stockées :
1. Dans le champ `relations` (JSON array)
2. Dans la table `memory_edges` pour le graphe

## Maintenance

### Compacter la base

```bash
docker exec lancedb-viz python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, '/home/hermes/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore(Path('/home/hermes/.hermes/lancedb'))
table = store._table
table.compact()
print(f'Compacted. Now size: {table.count_rows()} rows')
"
```

### Ré-embedding

```bash
docker exec lancedb-viz python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, '/home/hermes/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore(Path('/home/hermes/.hermes/lancedb'))
store._reembed_all()
print('Re-embedding done')
"
```

### Vider le cache serveur

Le serveur a un cache TTL de 30s. Pour forcer un refresh : `docker restart lancedb-viz`

## Pitfalls

### 1. Fichiers en RO — toujours restart

Les edits sur `server.py` ou les fichiers `static/` ne prennent effet qu'après `docker restart lancedb-viz`. Pas de hot-reload.

### 2. Entités vides sur contenu technique

Les tokens avec `.`, `<`, `>`, `()`, ou `/` ne passent pas l'extracteur d'entités. Reformuler :
```
❌ config.yaml collectors.<name>.enabled:true/false
✅ collectors enables dans config yaml
```

### 3. Cache navigateur

Les assets frontend ont `Cache-Control: no-cache` mais les navigateurs peuvent ignorer. Faire Ctrl+Shift+R (hard refresh) après un déploiement statique.

### 4. DB size qui gonfle

LanceDB crée une nouvelle version à chaque écriture. Compacter régulièrement si la base dépasse 100 MB. Après compact : 2-5 MB pour ~200 entrées.

### 5. Ollama timeout

Si Ollama est lent, les embeddings peuvent timeout. Vérifier `keep_alive` dans Ollama (`ollama serve --keep-alive 30m`).

## Références

- [Hermes Agent](https://github.com/3L0935/hermes-agent)
- [LanceDB](https://lancedb.github.io/lancedb/)
- [vis-network](https://visjs.github.io/vis-network/docs/network/)

---

*Maintenu par [@3L0935] pour usage personnel. Les données de mémoire ne sont pas commitées dans ce repo.*