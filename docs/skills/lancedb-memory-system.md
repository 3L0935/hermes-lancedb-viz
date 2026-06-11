---
name: lancedb-memory-system
description: "Architecture LanceDB locale — store, viz, scripts. Pas de format ici (voir memory-writing)."
version: 4.2.0
triggers:
  - "lancedb memory"
  - "vector memory"
  - "lancedb rebuild"
  - "lance db"
  - "lancedb plugin wipe"
  - "graph shows nothing"
---

# LanceDB Memory System

Architecture et déploiement du stockage vectoriel local.

## Architecture

```
~/.hermes/hermes-agent/plugins/memory/lancedb/   <- store.py + provider + plugin.yaml (repo)
~/.hermes/lancedb/                                <- DB files (donnees persistantes)
~/.hermes/lancedb-viz/                            <- viz server.py + static/index.html (bind-mountes dans Docker)
```

**Viz** : Docker (lancedb-viz:local). Bind-mounts de `~/.hermes/lancedb-viz/` et `~/.hermes/hermes-agent/` et `~/.hermes/lancedb/`.
**Port** : Le container tourne sur **7777** (overridé par `--port 7777 --host 0.0.0.0` dans le CMD Docker). Le code source dit `PORT=7778` mais le CMD l'override.
**Store** : plugin memory du repo Hermes — `memory.provider: lancedb` dans config.yaml.
**Frontend** : SPA 10 pages en vanilla JS dans `static/index.html`. Liste paginée (20/page), timeline, tags, duplicates, embedding scatter plot, clusters, stale, graph (vis-network).

## Schema (17 colonnes)

Le store.py definit ces champs dans `_get_schema()` :

| Champ | Type | Defaut | Description |
|-------|------|--------|-------------|
| id | string | UUID | Identifiant unique |
| content | string | - | Le contenu nettoye (::relations:: strippe) |
| category | string | "fact" | Categorie : 10 valeurs possibles |
| entities | string | "[]" | Entites extraites (JSON array) |
| links | string | "[]" | Liens vers d'autres memoires (JSON array) |
| relations | string | "[]" | Relations type-cible (JSON array) |
| **tags** | string | "[]" | Tags libres (JSON array) — depuis juin 2026 |
| **quality** | float64 | 0.5 | Score de pertinence (0-1) — depuis juin 2026 |
| **type** | string | category | Sous-type granulaire — depuis juin 2026 |
| source | string | "" | Source de l'entree |
| session_id | string | "" | Session Hermes |
| user_id | string | "" | Utilisateur |
| created_at | float64 | now | Timestamp creation |
| updated_at | float64 | now | Timestamp MAJ |
| access_count | int64 | 0 | Compteur d'acces |
| accessed_at | float64 | now | Dernier acces |
| vector | list<float32>(768) | zeros | Embedding vectoriel |

**Migration des 3 nouveaux champs** : faite le 2026-06-11 via script de recovery (voir `references/schema-migration-recovery.md`). Les 224 entrees existantes ont recu `tags=[]`, `quality=0.5`, `type=<category>`.

## Pitfall: `_init_table()` ne migre PAS

Le store.py avait une methode `_migrate_table()` qui utilisait `rename_table()` — **cassée sur LanceDB OSS** (NotImplementedError). Le code a ete simplifie pour juste logger un warning si les colonnes manquent. La migration DOIT etre faite manuellement via le pattern dans `references/schema-migration-recovery.md`.

## Viz (localhost:7777)

### Frontend : architecture multi-fichiers (juin 2026)
Le viz est un **SPA vanilla JS** reparti sur **4 fichiers séparés** sous `~/.hermes/lancedb-viz/static/` :

```
static/
├── style.css     (22 KB) — Thème néon OLED, glow system, btn-neon, Fira Code, tout le CSS
├── index.html    (15 KB) — Squelette HTML : nav, topbar, pages (dashboard, graph, etc.)
├── graph.js      (27 KB) — Moteur graph : vis-network, physique, typed_edges, sidebar, filtres
└── app.js        (26 KB) — Navigation + pages : dashboard, memories, timeline, tags, duplicates,
                            embeddings, clusters, stale, modals, import/export
```

**Pourquoi cette archi :** le fichier monolithique `index.html` (2000+ lignes) etait devenu inmaintenable — 4 couches de patchs par-dessus patchs. Separer CSS/HTML/JS graph/JS pages empeche qu'un changement sur le graph casse le dashboard ou vice-versa.

**10 pages** : Dashboard, Memories, Timeline, Tags, Duplicates, Embedding, Clusters, Stale, Graph (vis-network).

**Navigation** : Sidebar nav a gauche (responsive — se reduit a icones sur mobile). Les pages sont des `<div class="page">` montrees/cachees via `switchPage()`.

**⚠️ Pitfall: monolithe → patchs desynchronises.** Ne JAMAIS faire 5+ petits patchs sur le HTML/CSS/JS graph quand il est dans un fichier monolithe. Les patchs desynchronisent IDs HTML, classes CSS, event handlers, et references DOM. Si le fichier est trop gros ou deja corrompu : reecrire les sections propres a zero (style.css, graph.js, app.js, index.html) plutot que de continuer a patcher. Un rewrite propre prend 10 minutes, 20 patchs prennent 2 heures et cassent tout.

**⚠️ Pitfall: doublons `const` entre graph.js et app.js → JS mort.** `graph.js` definit `const catColors = {...}`, `const catLabels = {...}`, `const catColorsG`, `const defaultColorG`. Si `app.js` redefinit ces memes `const` dans le scope global (Script mode), le navigateur jette une `SyntaxError: Identifier 'X' has already been declared` et **TOUT le JS meurt** — dashboard vide, rien cliquable, zero message d'erreur dans l'UI. Ce bug est invisible sans ouvrir la console navigateur. **Fix:** `app.js` ne doit PAS redefinir `catColors`/`catLabels`/etc. Mettre un commentaire `// catColors and catLabels are defined in graph.js (loaded first)` a la place. Verifier avec `browser_console` apres deploy — si `console.error()` ou `js_errors` sont vides mais que le dashboard reste en "Loading...", c'est probablement ce bug.

**⚠️ Pitfall: .bak = piège à régressions.** Les `.bak` sont un snapshot gelé qui ignore toutes les features et corrections ajoutées depuis sa création. S'en servir comme référence réintroduit des bugs déjà corrigés et supprime des features. La vraie source de vérité est le code qui tourne, pas un snapshot périmé. **Règle :** ne JAMAIS utiliser un .bak comme référence pour restaurer du code.

### Graph View (page Graph)
Liens vectoriels cosine similarity.

Modes: Raw links (defaut), By category (hubs par categorie DB), By entity tags (hubs par entite frequente).

Threshold slider : 0.30-1, step 0.05, **defaut 0.8**. Onchange reload le graph avec le nouveau seuil. Le oninput update juste l'affichage.

Tier checkboxes : T1/T2/T3 filtrent les noeuds par `n.tier` (valeur extraite du content `[Tier=N]`). Le filtre est local (pas de reload).

Physique: forceAtlas2Based, gravConst=-40, centralGravity=0.005, springLength=160, springConstant=0.004, damping=0.98.

Couleurs (fond OLED #080810) — 10 categories, theme neon cyberpunk saturé :
| Categorie | Border | Label | Hex (canvas) | Glow |
|-----------|--------|-------|--------------|------|
| user_pref | #ff2d8d | #ff6db3 | #ec4899 | rgba(255,45,141,1.0) |
| project | #00ff88 | #5cffb0 | #10b981 | rgba(0,255,136,1.0) |
| tech | #ff9d00 | #ffc04d | #f59e0b | rgba(255,157,0,1.0) |
| correction | #ff2244 | #ff6682 | #ef4444 | rgba(255,34,68,1.0) |
| fact | #00ddff | #66eeff | #06b6d4 | rgba(0,221,255,1.0) |
| decision | #aa44ff | #cc88ff | #8b5cf6 | rgba(170,68,255,1.0) |
| insight | #44ff66 | #88ff99 | #22c55e | rgba(68,255,102,1.0) |
| reference | #3399ff | #77bbff | #3b82f6 | rgba(51,153,255,1.0) |
| pattern | #ff6611 | #ff9944 | #f97316 | rgba(255,102,17,1.0) |
| question | #dd33ff | #ee77ff | #a855f7 | rgba(221,51,255,1.0) |
| default | #6677ff | #99aaff | #6366f1 | rgba(102,119,255,0.8) |

**Freshness — halo BASÉ sur la couleur de catégorie :** Les nodes ne prennent pas un halo blanc — le glow est la couleur de leur catégorie, plus ou moins opaque selon l'âge. Plus c'est récent, plus le `shadow.size` est grand (30px → 3px) et l'opacité élevée (1.0 → 0.08). Un symbole ◉ apparaît dans le label pour <1h. Voir `references/freshness-model.md` pour les seuils exacts.

**Typed edges VISIBLES UNIQUEMENT sur selection :** Les relations (`part_of`, `connected_to`...) ne sont PAS affichees comme des edges visibles sur le graphe. Elles sont materialisees par un glow violet (#a78bfa) sur les nodes connectes quand le node source est clique. Voir `references/typed-edges-viz.md`.

**Ghost mode : tier filtering → opacity 30%** Si un node est masque par le filtre tier (T1/T2/T3) mais reste connecte a un node visible, il passe en opacity 0.3 avec son glow reduit — il n'est pas totalement cache. Les edges vers/depuis ce node restent visibles.

### API Endpoints (backend server.py, port 7777)

**⚠️ Pitfall: API handlers retournent DUPLICATE wrapping.** Les handlers `api_get_tags()`, `api_get_timeline()`, `api_get_duplicates()`, `api_get_projection()`, `api_get_clusters()`, `api_get_stale()` renvoient **directement** le resultat (liste ou dict), PAS `{"tags": store.get_tags()}`. L'ancien wrapping cassait le frontend qui attendait un format plat.

Le frontend a des defenses (unwrapping) pour gerer les deux formats en transit, mais les nouveaux handlers doivent rester plats.

**Dashboard** renvoie `{"total": N, "total_memories": N, "categories": {...}, ...}`. `total` est utilise par le frontend (stats cards), `total_memories` est historique.

**Legacy (preserved):** `/api/graph`, `/api/stats`, `/api/typed-edges`, `/api/search`, `/api/export`, `/api/memory`, `/api/delete`, `/api/update`, `/api/update_entities`, `/api/import`.

**New memviz endpoints:**
- `GET /api/dashboard` — stats enrichies (categories, types, tags, top accessed)
- `GET /api/memories?category=&type=&tag=&quality_min=&quality_max=&date_from=&date_to=&search=&offset=&limit=` — liste paginee filtrable
- `GET /api/tags` — tous les tags avec counts
- `GET /api/timeline` — memories groupees par jour
- `GET /api/duplicates?threshold=0.9` — groupes de doublons par similarite vectorielle
- `GET /api/projection?n_neighbors=15&min_dist=0.1` — projection UMAP 2D
- `GET /api/clusters?threshold=0.6&min_size=2` — clusters semantiques
- `GET /api/stale?days=90&quality_max=0.3` — memories vieilles + low quality
- `POST /api/memories/:id` — update content, category, tags, quality, type
- `POST /api/memories/:id/access` — increment access count
- `POST /api/memories/bulk-delete` — `{memory_ids: [...]}`
- `POST /api/memories/bulk-tag` — `{memory_ids, add_tags, remove_tags}`
- `POST /api/memories/bulk-type` — `{memory_ids, type}`
- `POST /api/tags/rename`, `delete`, `merge`

### Redemarrer le container
```bash
docker restart lancedb-viz
```
Les fichiers sont bind-mountes — les changements a server.py et index.html sont instantanes. Le restart est necessaire pour le server.py seulement (le HTML est servi a chaque requete).

### Port pitfall
Le code source dit `PORT = 7778` mais le Docker CMD override avec `--port 7777 --host 0.0.0.0`. Toujours tester sur 7777 d'abord. Le fichier `references/port-migration-7777-7778.md` documente la migration historique.

## Scripts

### `scripts/reembed-entries.py`
Re-embedding apres batch > 5 modifs. Indispensable — changer le contenu ne met pas a jour les vecteurs.

```
~/.hermes/hermes-agent/venv/bin/python3 scripts/reembed-entries.py [--dry-run]
```

**TODO:** Ajouter parsing des blocs `::relations::` dans ce script. Actuellement il ne fait que re-embed les vecteurs et ré-indexer les entités. Il doit aussi :
1. Scanner chaque entrée pour le marqueur `::relations::`
2. Parser les paires `type=cible`
3. Upsert dans la table `memory_edges` (à créer)

### `scripts/migrate-entries.py`
Migration one-shot v1 -> format semi-structure. Plus utile (deja fait). Garde pour archive.

## Relations Table (`memory_edges`) — IMPLEMENTED

Store dans `~/.hermes/lancedb/memory_edges` — table LanceDB séparée de `memories`. Écrite automatiquement par `_write_relations()` dans store.py à chaque `add()`.

Schema :

```
memory_edges
├── source_id: str          <- UUID de la mémoire source
├── relation_type: str      <- "requires", "depends", "part_of", "runs_on", "connects_to", "uses", "extends"
├── target_label: str       <- Nom du label de la mémoire cible (ou label abstrait)
├── created_at: float64     <- Timestamp
```

**Workflow write (store.py `add()`) :**
1. Écrire l'entrée mémoire (table `memories`)
2. `_write_relations(mem_id, content)` → parse `::relations::`, `_ensure_edges_table()`, batch add

**Frontend (viz) :**
- `get_typed_edges()` → liste + attachee a chaque reponse `/api/graph` sous `result["typed_edges"]`
- `index.html` : resolve par matching target_label vs labels de nodes. Arrows directionnelles colorees par type (orange=requires, bleu=depends, vert=runs_on, violet=connects_to)
- Si target_label ne match aucun node → edge invisible (pas de crash, pas de node cree)

**Backfill :** Utiliser `/tmp/batch-relations.py` pattern — script qui scanne toutes les entrees, detecte les co-occurrences (keyword + label matching), ecrit `::relations::` dans le content + `memory_edges` table. Re-embedder apres.

**Format d'ecriture :**
```
Domaine:Sujet Contexte. clé=valeur. [Tier=N]
::relations:: type=cible | type=cible2 | ...
```

## Planned — FTS (BM25) + Hybrid Search

*LanceDB supporte nativement le full-text search via Tantivy et la fusion RRF. Pas encore activé dans le store.*

**Activation (une ligne) :**
```python
table.create_fts_index("content")    # Crée l'index BM25 sur la colonne content
table.search(query).hybrid_search()  # Vector + BM25 fusion RRF
```

**Impact :**
- BM25 seul : recall ~66.5% (bench GBrain)
- Vector seul : recall ~17.7% P@5
- Hybrid + RRF : recalls ~97.9% (vector + BM25 fusionnés)
- 0 coût API, 0 latence additionnelle (index Tantivy pré-calculé)

**Par rapport aux benchmarks GBrain :** leur gain vient de l'hybrid search + reranker, pas du graph. Le FTS index est ce qui donne le + gros delta qualité/perf.

**TODO store.py :**
1. Ajouter `table.create_fts_index("content")` dans `__init__()` après `table.create()`
2. Modifier `search()` pour appeler `.hybrid_search()` si query non-vide
3. Option: garder un flag `use_hybrid` dans la config pour fallback vector-only (très petits datasets)

## Auto-Tags (2026-06-11)

Les tags sont **extraits automatiquement des entités** lors de `lancedb_add()` :

```python
auto_tags = [e for e in entities if len(e) >= 3 and e not in _STOP_ENTITIES][:5]
```

Pas besoin de les gérer manuellement. Les 224 entrées existantes ont été backfillées via script le 2026-06-11.

## Auto-Quality Score (2026-06-11)

La qualité est recalculée à chaque `get_by_id()` via `_compute_quality(memory)` :

| Facteur | Bonus | Condition |
|---------|-------|-----------|
| Accès fréquent | +0.1 à +0.2 | ≥2 / ≥5 / ≥10 accès |
| Liens entités | +0.05/lien | Max +0.15 |
| Fraîcheur | +0.1 / +0.05 | < 7 jours / < 30 jours |

Base = 0.5, clampé [0.1, 1.0]. Recalculé dynamiquement à chaque fetch.

## API Response Pitfall

Les handlers `api_get_tags()`, `api_get_timeline()`, `api_get_duplicates()`, `api_get_projection()`, `api_get_clusters()`, `api_get_stale()` renvoient **directement** le résultat (liste ou dict) — PAS dans `{"tags": ...}`. L'ancien wrapping cassait le frontend.

**Ne JAMAIS wrapper ces réponses** dans un objet `{"tags": ...}`, `{"timeline": ...}`, etc. Le frontend attend un format plat.

## Graph Page: Ne PAS modifier la structure HTML/CSS (règle stricte)

La page Graph dans le SPA utilise son propre layout totalement indépendant :
- CSS standalone (backgrounds #0a0a16, borders #1e1e3a, pas de glassmorphism)
- HTML avec `<div id="main">` qui encapsule `#graph-container` + `#sidebar` en absolute
- Police Fira Code (pas Inter/JetBrains Mono)
- Boutons btn-focus/btn-save/btn-delete (pas btn-glass)
- Édges curvedCW, physique forceAtlas2Base avec ses propres paramètres

**Règle stricte :** Quand tu modifies `index.html`, ne JAMAIS patcher individuellement le HTML/CSS/JS de `#page-graph`. Si une correction est nécessaire :
1. **Restaurer le bloc complet** du HTML/CSS/JS graph depuis la dernière version connue bonne (backup ou session_search)
2. NE PAS faire 5 petits patchs — ça désynchronise le HTML, le CSS, et les event handlers
3. Vérifier que toutes les `document.getElementById()` référencées existent dans le HTML (pas de TypeError silencieux)
4. Vérifier que les classes CSS des boutons/badges existent (pas de `btn-neon` mort)

## Server Performance Optimization (cache TTL + Arrow col extraction)

Les endpoints `/api/stats` et `/api/dashboard` sont **optimisés** avec :

1. **`_compute_stats_fast()`** : lit les stats directement depuis les colonnes Arrow (pas de dict Python par ligne), droppe la colonne `vector` (768 floats, ~3KB/ligne) avant processing. Un seul scan pour tous les métriques.

2. **`_get_cached_stats()`** : cache TTL 30s avec `threading.Lock`. Les mutations (update, delete, access, tag ops) invalident le cache via `_invalidate_cache()`.

3. **Performance batterie:** (warm cache)
   - `/api/stats`: ~120ms → **~0.5ms**
   - `/api/dashboard`: ~130ms → **~0.5ms**
   - Cache miss (après mutation): ~120ms → **~57ms** (le Arrow path économise le double scan + vector col)

### Pitfall: Domain hubs mal groupés (catégorie DB ≠ label prefix)

La fonction `_build_category_hub_graph()` dans `server.py` groupait les nodes par leur **préfixe de label** (Hermes, Bodycam, CrowdWhisper, etc.) au lieu de leur **catégorie DB** (tech, correction, fact, project, pattern). Conséquence : 19+ hubs avec des couleurs néon qui ne correspondaient pas aux couleurs des nœuds, rendant le graph visuellement incohérent.

**Symptôme :** Les hubs montrent `◆ Hermes`, `◆ Bodycam` au lieu de `◆ Tech`, `◆ Correction`. Les couleurs des hubs ne matchent pas les couleurs des nœuds filles.

**Fix :** Grouper par `n.get("category", "fact")`, pas par `label.split(":")[0]`. Les hubs deviennent `◆ Tech` (orange), `◆ Correction` (rouge), `◆ Fact` (cyan), `◆ Project` (vert), `◆ Pattern` (orange foncé). La couleur du hub correspond à la palette de catégorie. Voir `_build_category_hub_graph()` dans server.py.

### Pitfall: LanceDB version bloat après batch-writes

LanceDB garde **toutes les versions** de la table à chaque écriture. 127 reclassifications + 38 corrections de format + les updates précédents = **14,823 versions** → 596 MB.

**Symptôme :** `du -sh ~/.hermes/lancedb/memories.lance/` montre 500+ MB pour seulement 224 entrées. Le dossier `_versions/` fait 400 MB.

**Fix :**
```python
import lancedb
from datetime import timedelta
db = lancedb.connect('/home/hermes/.hermes/lancedb')
tbl = db.open_table('memories')
tbl.cleanup_old_versions(timedelta(seconds=0))
```
→ 596 MB → **4.3 MB** en une commande. Les données (224 entrées, 130 MB) sont intactes.

**Prévention :** Ne pas lancer `cleanup_old_versions` à chaque update (coûteux), mais le faire après un batch > 50 updates/reclassifications. Le `timedelta(seconds=0)` garde uniquement la dernière version.

### Pitfall: `tags: null` crashe `get_tags()`

La colonne `tags` peut contenir `NULL` SQL si les entrées ont été ajoutées avant la migration des champs `tags`/`quality`, ou si un `update()` n'a pas touché `tags`. `m.get("tags", [])` retourne `None` (pas `[]`).

**Symptôme :** `GET /api/tags` retourne `{"error": "'NoneType' object is not iterable"}`.

**Fix partout où `m.get("tags", [])` est utilisé :**
```python
tags = m.get("tags", [])
if tags is None:
    tags = []
if isinstance(tags, str):
    ...
```

**Backfill :** Scanner les 25+ entrées avec `tags: null` et les mettre à jour avec `tags: "[]"`.
### Pitfall: Pagination `→` saute à la dernière page

Le bouton next page avait `onclick="memOffset=' + Math.max(0,(pages-1)*MEM_LIMIT) + ';loadMemories()'` — calculait l'offset de la **dernière page** au lieu de `offset + limit`.

**Symptôme :** `↩` fonctionne, `→` saute direct à la dernière page.

**Corrigé en :** `onclick="memOffset=Math.min(memOffset+MEM_LIMIT,((pages-1)*MEM_LIMIT));loadMemories()"` — page suivante plafonnée au max.

**Fichier:** `static/app.js` ligne 176.

### Pitfall: `async` manquant sur delete — tout JS mort

Les fonctions `deleteSingleMemory()` et `bulkDeleteMemories()` utilisaient `await` mais étaient déclarées comme `function` (pas `async function`). Ce TypeError silencieux tuait **tout** `app.js` — dashboard bloqué sur "Loading...".

**Symptôme :** Aucune erreur visible dans l'UI, console navigateur montre `SyntaxError: await is only valid in async function`. Toutes les pages restent en "Loading...".

**Fix :** `function deleteSingleMemory(id)` → `async function deleteSingleMemory(id)`.

**Règle :** Toute fonction contenant `await` DOIT être `async function`. Les fonctions fléchées aussi : `const fn = async () => { ... }`.


### Comportement actuel du graph :
- **Hover → rien.** Pas de tooltip. Pas de hoverNode/blurNode handlers. L'utilisateur clique sur un noeud → sidebar s'ouvre.
- Le tooltip vis-network natif est aussi désactivé (tooltipDelay supprimé). Zéro popup au survol.
- **Click → sidebar détail enrichie** avec :
  1. **Stats bar** (grid 2×2) — Freshness (dot glowé + label), Quality (%, couleur verte ≥80% / jaune ≥50% / rouge sinon), Views (access_count), Links (typed + vector edges)
  2. **Content** — texte complet + cat badge
  3. **Date** — timestamp formaté
  4. **Entities** — tags cliquables (clic → filtre search)
  5. **Similar by embedding** (EN PREMIER — section prioritaire) — vector edges (cosine similarity) triés par score décroissant, pourcentage couleur. Limité à 10. Filtrés par le threshold slider.
  6. **Related** — fusionne typed_edges + linked_memories en une seule section. Chaque entrée affiche son badge catégorie + son type de relation (depends, part_of, shared, linked...) + contexte supplémentaire. Pas de doublons. Les typed edges ont priorité sur les linked (si une mémoire apparaît dans les deux, le type explicite prime).
  - Actions : Center, Edit, Copy JSON, Delete
- **Mode Raw** par défaut (pas Domain hubs)
- **Tier checkboxes T1/T2/T3** filtrage local via `applyFilters()`
- **Threshold slider** impacte DEUX choses : (a) le graphe global (reload edges), (b) les sections Relations + Similar dans la sidebar (filtrées par le seuil actuel)

### Pitfall: DOM refs orphelines aprés merge graph→SPA

Le JS original de `renderGraph()` (lignes ~1356-1522) référence des éléments DOM **par ID** qui peuvent ne plus exister dans le nouveau layout SPA. Ces TypeError non-catchés tuent **tout** `renderGraph()` silencieusement — zéro erreur visible, le graph ne s'affiche pas.

**Victimes connues:**
- `document.getElementById('edge-count')` — supprimé lors de la restructuration SPA. Le TypeError non-catché bloque tout le rendu du graph.
- `btn-neon` / `btn-neon-export` — classes CSS de l'ancien thème, remplacées par `btn-glass` dans le nouveau thème SPA.

**Fix pattern:** Quand tu restores du HTML graph original dans une nouvelle SPA :
1. Scanner TOUTES les refs `document.getElementById('...')` dans `renderGraph()` et les `loadGraph()` event handlers
2. Vérifier que chaque ID existe dans le nouveau HTML de `#page-graph`
3. Null-guarder (`if (el) el.textContent = ...`) ou supprimer les refs aux IDs supprimés
4. Vérifier que toutes les classes CSS du HTML graph (boutons, badges) existent dans le thème courant

**Test:** `grep -n "getElementById\|btn-neon" index.html` pour détecter les refs orphelines et les classes mortes avant de restart le conteneur.

## Pitfalls techniques (store.py)

- **Viz server doit utiliser venv** : le conteneur Docker a deja les bonnes dependances. Si lance manuellement depuis l'hote, utiliser `~/.hermes/hermes-agent/venv/bin/python3`.#
- **`tbl.update()` avec liste** -> AttributeError. Toujours boucle for + where.
- **Race condition stats** -> setter compteurs locaux avant fetch API. Voir `references/stats-race-condition-fix.md`.
- **Schema migration via _migrate_table() peut perdre les donnees** : la methode `_migrate_table()` dans `store.py` cree une table `memories_v2`, lit l'ancienne, cree la nouvelle, DROP l'ancienne, puis RENAME v2 → memories. **Probleme:** `rename_table()` n'est pas supporte dans LanceDB OSS (lance des `NotImplementedError`). Apres le drop de l'ancienne table, les donnees restent dans `memories_v2` mais le store ouvre une table `memories` vide. **Fix:** ne pas utiliser `rename_table()` — a la place, lire `memories_v2`, creer une nouvelle `memories` avec le schema final, copier les donnees, puis drop `memories_v2`. Voir le pattern dans `references/schema-migration-recovery.md`.
- **Ajout de colonnes sur table existante** : LanceDB ne supporte pas `ALTER TABLE ADD COLUMN`. La seule facon de migrer est de creer une nouvelle table avec le schema complet et copier les anciennes donnees. Le store.py implemente cette migration auto dans `_init_table()` — il detecte les colonnes manquantes et declenche `_migrate_table()`.
- **Pas de pandas** -> `tbl.to_arrow().column('content')`.
- **Ollama keep_alive** : injecter `keep_alive: 30s` dans le body JSON des appels `/api/embed`. Per-request, pas global. Ni OLLAMA_KEEP_ALIVE dans la config systemd.
- **Warning `lance is not fork-safe`** : harmless, supprime via `warnings.filterwarnings(ignore)` dans store.py avant import lancedb. Voir `references/lance-fork-warning-suppression.md`.
- **Schema mismatch on reconstruction** : quand on reconstruit store.py contre une table existante, le schema doit matcher exactement — noms, types, ordre. `_get_schema()` sert seulement a la creation. Pas de champ `metadata`. `access_count` est `int64` pas `int32`. Symptome: `ValueError: Invalid input, field X does not exist in table schema` sur `tbl.add()`.
- **Ollama unreachable from Docker** : le conteneur viz (lancedb-viz:local) ne peut pas joindre Ollama sur `localhost:11434` du host. `_embed()` retourne zero vector silencieusement. Inoffensif — l'embedding est fait cote host par le plugin Hermes. Pour du debug depuis le conteneur, utiliser `OLLAMA_HOST=http://host.docker.internal:11434`.
- **`_parse_relations()` premier match vs dernier** : le marqueur `::relations::` apparait parfois dans le texte (ex: "store.py parse ::relations:: au write"). La regex `re.search()` trouve le premier match = faux positif dans le contenu. **Fix:** `list(re.finditer(...))[-1]` pour prendre le dernier occurrence — celui apres `[Tier=N]` qui est le vrai marqueur. Symptome: relations parasites avec des labels absurdes parsees depuis le corps du texte.\n- **Deps manquantes dans le venv** : si `lancedb` + `pyarrow` ne sont pas installes dans `~/.hermes/hermes-agent/venv/`, le plugin `LanceDBMemoryProvider` ne crash PAS — il s'initialise silencieusement avec `self._store = None`. Tous les tools (`lancedb_search`, `lancedb_add`, `lancedb_graph`) renvoient `'NoneType' object has no attribute 'search/add/graph'`. Pas de trace dans les logs car le constructeur ne lance pas d'exception. **Symptome :** tools disponibles (schemas charges), mais erreur `NoneType` sur chaque appel. **Diagnostic:** `cd ~/.hermes/hermes-agent && venv/bin/python -c "import lancedb; import pyarrow; print('OK')"` — si le module manque, le pip list montre leur absence. **Fix :** `cd ~/.hermes/hermes-agent && uv pip install lancedb pyarrow`, puis restart du gateway. Les 212+ memoires existantes ne sont pas perdues — le store les retrouve automatiquement.\n- **Import manquant dans `__init__.py`** : `from .store import LanceDBStore` peut disparaitre (rebased, cherry-pick, etc.). Meme symptome que les deps manquantes — provider s'initialise avec `self._store = None`, tools renvoient `NoneType`. **Diagnostic:** log agent `WARNING agent.memory_manager: provider 'lancedb' initialize failed: name 'LanceDBStore' is not defined`. **Fix:** re-ajouter l'import et restart gateway. Verification avec `cd ~/.hermes/hermes-agent && venv/bin/python -c "from plugins.memory.lancedb import LanceDBMemoryProvider; print('OK')"`.\n- **Category invalide pas filtree** : le schema enum a 5 categories (`project|tech|fact|correction|user_pref`) mais seul le LLM le respecte — aucune validation backend dans `_handle_add()`. Une categorie orpheline comme `"architecture"` passe, donne 0 entites, 0 liens, jamais matchée par les searches filtrées. **Fix:** fallback a `"fact"` dans `_handle_add()` si category pas dans `VALID_CATEGORIES`.\n- **Entity noise — keywords ≤2 chars** : `c`, `ts`, `js`, `rg`, `fd`, `sh`, `go` dans `_TECH_KEYWORDS` matchent en substring (`if kw in lower`). Dec 2026-06-07: `c` matche dans tout texte contenant la lettre c (100% des textes francais), `ts` matche dans "statuts", "contents", etc. 30 memoires polluees, 935 entites noise, ~329 faux liens. **Fix:** `len(kw) < 3: continue` dans `store.py:extract_entities()`. Apres fix: re-extract entities sur toutes les memoires + rebuild links + re-embed complet avec nomic.\n- **Stale browser cache faussant les couleurs** : si l'utilisateur signale "cette memoire est pas de la bonne couleur" alors que la DB a deja la bonne categorie, faire Ctrl+F5 (hard refresh). Le serveur sert `Cache-Control: no-cache` mais les navigateurs peuvent render des donnees stale du cache memoire.

## Recovery — Plugin files wiped, graph shows empty/no data

**Symptom:** Docker container up (healthy), DB present in `~/.hermes/lancedb/memories.lance/`, but `/api/stats` and `/api/graph` return `{"error": "LanceDB plugin not found"}` with 0 nodes/edges.

**Root cause:** The 3 plugin files under `plugins/memory/lancedb/` (store.py, __init__.py, plugin.yaml) were deleted or emptied. The viz server imports `from plugins.memory.lancedb.store import LanceDBStore` and fails silently, returning the fallback error dict.

**Diagnosis:**
- Check docker is really up: `docker ps --filter name=lancedb-viz`
- Check the API returns an error (not empty data): `curl -s http://localhost:7778/api/graph | python3 -c "import sys,json; d=json.load(sys.stdin); print('error:', d.get('error','none'))"`
- List plugin files: `ls -la ~/.hermes/hermes-agent/plugins/memory/lancedb/`
- Check viz logs: `docker logs lancedb-viz --tail 20`
- Check other memory plugins for collateral damage: `for d in ~/.hermes/hermes-agent/plugins/memory/*/; do name=$(basename "$d"); count=$(ls -A "$d" 2>/dev/null | wc -l); [ "$count" = "0" ] && echo "EMPTY: $name"; done`

**Recovery:** Reconstruct the 3 files. The DB data is persistent (Rust columnar format) — zero data loss.

### Files to recreate

**store.py** — LanceDBStore class.
Must export: `class LanceDBStore`, `extract_entities()`, `_STOP_ENTITIES`, `_TECH_KEYWORDS`, `EMBED_URL`, `EMBED_MODEL`.
Key methods called by server.py:
- `__init__(db_path)` — connect LanceDB, init/create table
- `_get_all_raw()` — return ALL rows WITH vectors (server uses this for graph/comparisons)
- `get_all()` — return ALL rows WITHOUT vectors
- `get_by_id(memory_id)` — single row (no vector), increments access_count
- `_get_by_id_raw(memory_id)` — single row WITH vector (similarity computation in server.py)
- `search(query, top_k, category)` — vector search via LanceDB
- `add(content, category)` — embed, extract entities, store, rebuild links
- `delete(memory_id)` — delete + rebuild all links
- `update_entities(memory_id, entities)` — update tags + rebuild links
- `graph()` — return {nodes, edges} for agent tool
- `_embed(text)` — Ollama `/api/embed` POST returning np.ndarray

Embedding via httpx POST to OLLAMA_HOST/api/embed (default http://localhost:11434) with model nomic-embed-text and keep_alive=30s.

**__init__.py** — MemoryProvider plugin.
Implements MemoryProvider ABC from agent.memory_provider.
Exposes 4 tool schemas: lancedb_search, lancedb_add, lancedb_graph, lancedb_delete.
Register function: `def register(ctx)` — loads config from config.yaml memory.lancedb, creates LanceDBMemoryProvider, calls ctx.register_memory_provider(provider).
Auto-sync intentionally disabled (sync_turn is pass).

**plugin.yaml**
```
name: lancedb
version: 1.2.0
description: "LanceDB vector memory — zero-footprint local memory with entity extraction, linking, Ollama embeddings, and interactive graph visualization."
```

### Verification after restore
```
docker exec lancedb-viz python3 -c "
import sys
sys.path.insert(0, '/home/hermes/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
from plugins.memory.lancedb import LanceDBMemoryProvider
print('OK')
"
docker restart lancedb-viz
curl -s http://localhost:7778/api/stats | python3 -m json.tool
curl -s http://localhost:7778/api/graph | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'nodes={len(d.get("nodes",[]))} edges={len(d.get("edges",[]))}')"
```

## Format des entrées

**→ Voir `memory-writing` skill.** Les règles de format (Domaine:Sujet, clé=valeur, [Tier=N], linking implicite, thresholds) sont là-bas. Pas de duplication ici.