---
name: lancedb-memory-system
description: "Architecture LanceDB locale — store, viz, scripts. Pas de format ici (voir memory-writing)."
version: 5.2.0
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
~/.hermes/plugins/lancedb/                      <- store.py + provider + plugin.yaml (CANONIQUE, survit aux updates)
~/.hermes/hermes-agent/plugins/memory/lancedb/  <- copie runtime (compat bundled-first)
~/.hermes/lancedb/                              <- DB files (donnees persistantes)
~/.hermes/lancedb-viz/                          <- viz server.py + static/ (deploy par deploy-local.sh)
~/.config/systemd/user/lancedb-viz.service      <- unit systemd (versionnee dans le repo)
~/.config/systemd/user/lancedb-viz-maintenance.timer <- seuils de compaction, toutes les heures
```

**Viz** : le conteneur Docker `lancedb-viz` sur `127.0.0.1:7777` est canonique (décision du 10/09/2026). Il est piloté par `~/github/hermes-hub/services/lancedb-viz/docker-compose.yaml`, utilise l'image `lancedb-viz:local` et le réseau externe `hub-services`. L'unité systemd sur 7778 est un secours désactivé.
**Redémarrage viz** : les bind mounts chargent `server.py`, `maintenance.py` et `static/` depuis `~/.hermes/lancedb-viz/`, mais Python ne recharge pas le serveur tout seul. Redémarrer le conteneur après une modification Python ; les fichiers statiques sont relus à chaque requête.
**Déploiement** : `docker compose up -d` depuis le service Hub gère le conteneur. `./scripts/deploy-local.sh` synchronise repo, plugin canonique, copie runtime et viz, puis redémarre le conteneur existant. Il ne redémarre pas `hermes-gateway` : après un changement plugin, redémarrer le gateway séparément pour recharger les modules importés.
**Store** : plugin memory du repo Hermes — `memory.provider: lancedb` dans config.yaml.
**Frontend** : SPA 10 pages en vanilla JS dans `static/`. Liste paginee (20/page), timeline, tags, duplicates, conflicts, embedding scatter plot, clusters, stale, graph (vis-network).

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

## Viz canonique (localhost:7777)

Le déploiement normal est le conteneur Docker du Hub :

```bash
cd ~/github/hermes-hub/services/lancedb-viz
docker compose up -d
cd ~/github/hermes-lancedb-viz
./scripts/deploy-local.sh --dry-run
./scripts/deploy-local.sh
```

Le service monte `~/.hermes/lancedb-viz/server.py`, `maintenance.py` et
`static/`, ainsi que la base et le dossier des backups de compaction. L'unité
versionnée `systemd/lancedb-viz.service` écoute sur 7778 mais reste désactivée ;
`./scripts/deploy-local.sh --systemd-fallback` ne sert qu'à un recovery explicite.

### Frontend : architecture multi-fichiers (juin 2026)
Le viz est un **SPA vanilla JS** reparti sur **4 fichiers séparés** sous `~/.hermes/lancedb-viz/static/` :

```
static/
├── style.css     (22 KB) — Thème néon OLED, glow system, btn-neon, Fira Code, tout le CSS
├── index.html    (16 KB) — Squelette HTML : nav, topbar, pages (dashboard, graph, conflicts, etc.)
├── graph.js      (27 KB) — Moteur graph : vis-network, physique, typed_edges, sidebar, filtres
└── app.js        (27 KB) — Navigation + pages : dashboard, memories, timeline, tags, duplicates,
                            conflicts, embeddings, clusters, stale, modals, import/export
```

**Pourquoi cette archi :** le fichier monolithique `index.html` (2000+ lignes) etait devenu inmaintenable — 4 couches de patchs par-dessus patchs. Separer CSS/HTML/JS graph/JS pages empeche qu'un changement sur le graph casse le dashboard ou vice-versa.

**10 pages** : Dashboard, Memories, Timeline, Tags, Duplicates, Conflicts, Embedding, Clusters, Stale, Graph (vis-network).

**Page Conflicts (09/2026)** : liste du registre des contradictions déterministes (open/resolved/all), cartes côte à côte valeur_a vs valeur_b, clic → modal mémoire. Endpoint `GET /api/conflicts`. CSS dédié `.conflict-*` dans style.css (couleur amber, pas de neon conflictuel).

**Navigation** : Sidebar nav a gauche (responsive — se reduit a icones sur mobile). Les pages sont des `<div class="page">` montrees/cachees via `switchPage()`.

**⚠️ Pitfall: monolithe → patchs desynchronises.** Ne JAMAIS faire 5+ petits patchs sur le HTML/CSS/JS graph quand il est dans un fichier monolithe. Les patchs desynchronisent IDs HTML, classes CSS, event handlers, et references DOM. Si le fichier est trop gros ou deja corrompu : reecrire les sections propres a zero (style.css, graph.js, app.js, index.html) plutot que de continuer a patcher. Un rewrite propre prend 10 minutes, 20 patchs prennent 2 heures et cassent tout.

**⚠️ Pitfall: doublons `const` entre graph.js et app.js → JS mort.** `graph.js` definit `const catColors = {...}`, `const catLabels = {...}`, `const catColorsG`, `const defaultColorG`. Si `app.js` redefinit ces memes `const` dans le scope global (Script mode), le navigateur jette une `SyntaxError: Identifier 'X' has already been declared` et **TOUT le JS meurt** — dashboard vide, rien cliquable, zero message d'erreur dans l'UI. Ce bug est invisible sans ouvrir la console navigateur. **Fix:** `app.js` ne doit PAS redefinir `catColors`/`catLabels`/etc. Mettre un commentaire `// catColors and catLabels are defined in graph.js (loaded first)` a la place. Verifier avec `browser_console` apres deploy — si `console.error()` ou `js_errors` sont vides mais que le dashboard reste en "Loading...", c'est probablement ce bug.

**⚠️ Pitfall: .bak = piège à régressions.** Les `.bak` sont un snapshot gelé qui ignore toutes les features et corrections ajoutées depuis sa création. S'en servir comme référence réintroduit des bugs déjà corrigés et supprime des features. Si l'utilisateur dit que le .bak est trop vieux, le supprimer immédiatement (ne pas insister). **Règle :** ne JAMAIS utiliser un .bak comme référence pour restaurer du code. Toujours utiliser `session_search` pour retrouver l'état exact du code à un moment donné.

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
- `GET /api/conflicts?status=&memory_id=&limit=` — registre des contradictions (plat, comme les autres handlers)
- `GET /api/search?q=&top_k=&diagnostics=1` — recherche routée et diagnostics d'abstention/engine
- `GET /api/health` — diagnostics read-only tables, FTS, Ollama et pipeline
- `GET /api/review` — inbox de review bornée et read-only
- `GET /api/typed-edges` — relations typées persistées
- `GET /api/maintenance/compact/plan` — plan read-only, backup et rétention
- `POST /api/memories/:id` — update content, category, tags, quality, type
- `POST /api/memories/:id/access` — increment access count
- `POST /api/memories/:id/preview` — validation/rendu sans écriture
- `GET /api/refresh` — reset le singleton store + invalide le cache stats. Appelé par le bouton ↻ dans l'UI. Réponse `{"status": "ok"}`.
- `POST /api/memories/bulk-delete` — `{memory_ids: [...]}`
- `POST /api/memories/bulk-tag` — `{memory_ids, add_tags, remove_tags}`
- `POST /api/memories/bulk-type` — `{memory_ids, type}`
- `POST /api/tags/rename`, `delete`, `merge`
- `POST /api/maintenance/compact` — compaction confirmée avec backup préalable et vérification

**Redemarrer le service / Refresh**
```bash
docker restart lancedb-viz
```
Le conteneur 7777 est le service canonique. Le fichier unit est toujours versionné dans le repo et installé par `scripts/deploy-local.sh`, mais uniquement comme fallback 7778 désactivé.

**Alternative sans restart :** bouton **↻** dans la topbar de l'UI (ou `curl http://localhost:7777/api/refresh`). Reset le store singleton et recharge la page active. Fonctionne pour toutes les pages sauf les changements au code serveur (server.py) qui nécessitent un restart.

**⚠️ Pitfall: Les updates directes `tbl.update()` sur la colonne `tags` DB ne sont pas visibles dans le viz tant que le container n'a pas été restart (ou le bouton Refresh cliqué).** Le store.py est importé au démarrage du container (via `from plugins.memory.lancedb.store import LanceDBStore`). Si tu modifies la DB en direct via un script (cleanup de tags, reclassification batch), le viz continue de servir l'ancien état depuis ses imports en mémoire. **Fix:** toujours `docker restart lancedb-viz` après un batch d'updates directs sur la DB.

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

Schema (09/2026 : `target_id` ajouté ; backfill via `scripts/migrate_graph_retention.py` dans le repo viz, dry-run par défaut, `--apply` après backup) :

```
memory_edges
├── source_id: str          <- UUID de la mémoire source
├── relation_type: str      <- part_of, depends, requires, runs_on, connects_to, uses, extends, supersedes, invalidates, contradicts
├── target_id: str          <- UUID cible résolu ("" si label ambigu/absent, jamais de guess)
├── target_label: str       <- Label affichage + fallback
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

## Conflicts (memory_conflicts) — 09/2026

Table LanceDB `memory_conflicts` : id, memory_a_id, memory_b_id, subject, claim_key, value_a, value_b, status (open/resolved), confidence, created_at, resolved_at.

Détection DÉTERMINISTE au add/update de content, zéro LLM : catégories decision/correction/project/user_pref/tech/fact + même Domain:Subject + même clé key=value + valeurs différentes → record. Jamais de suppression/fusion auto. Update content → ferme les open conflicts de la mémoire puis recheck. Conflit réintroduit → réouvert avec les nouvelles valeurs. Tool `lancedb_conflicts` (status/memory_id/limit), endpoint `GET /api/conflicts`, page Conflicts du viz. `lancedb_add` retourne `potential_conflicts` immédiatement.

## Recall routing + graph traversal (09/2026)

`search(mode="auto")` route localement sans LLM : lexical (guillemets, UUID, chemins, marqueurs exact), graph (related/depends/linked/... → hybrid seed ≤5 + traversal 1-hop des edges target_id, budget total = top_k, champ `relation_type` + `retrieval_source` sur les résultats), hybrid par défaut. `relation_depth=0` désactive le traversal. Modes explicites `lexical|hybrid|graph` disponibles côté tool.

**Deploy standard (09/2026) :** le conteneur 7777 est géré depuis `~/github/hermes-hub/services/lancedb-viz/` avec `docker compose up -d`. Depuis le repo viz, lancer `./scripts/deploy-local.sh --dry-run` puis sans flag pour synchroniser le plugin canonique, la copie runtime et les bind mounts. Le script redémarre le conteneur mais PAS `hermes-gateway` ; un changement plugin exige donc un restart gateway séparé, sinon l'ancien module Python reste chargé silencieusement.

## FTS (BM25) + Hybrid Search — ACTIVÉ (2026-06-12)

LanceDB supporte nativement le full-text search via Tantivy (BM25) et la fusion RRF. **Activé dans store.py.**

### Abstention et gate de rétention (mis à jour 12/09/2026)

Le RRF sert uniquement au classement. Un candidat hybride est retenu si son
score BM25 est `>= SEARCH_MIN_BM25_SCORE` (`12.80`) **ou** si sa distance cosine
est `<= SEARCH_MAX_COSINE_DISTANCE` (`0.30`). Si aucun candidat ne franchit un
seuil, la recherche s'abstient (`abstained=true`, `below_calibrated_evidence`).

Le score BM25 dépend de la version du moteur : mêmes lignes, même requête et
même code ont donné `12.774338` avec `lancedb==0.34.0` et `14.397717` avec
0.38.0. Le pin `lancedb==0.34.0` doit rester aligné dans le repo, le venv hôte
et l'image Hub. Avec `diagnostics=1`, vérifier `calibrated_engine`,
`running_engine` et `matches` avant d'interpréter un score.

Le harnais `audit/repro/benchmark-retrieval.py` copie la base read-only vers
`/tmp`. Le split final exige strictement zéro faux résultat sur les questions
sans réponse et la présence de chaque `old_critical_correction`. MRR et recall,
dépendants du corpus, ne servent que de garde-fous anti-effondrement contre le
contrôle lexical gelé ; le delta du baseline vivant reste informatif. Les
cibles, le bruit mesuré, leur coût et l'engine sont dans
`audit/repro/retrieval-baseline-reference.json`.

`GET /api/maintenance/compact/plan` est strictement read-only et expose les
seuils (`64` versions, `64` fragments, `4` dossiers d'index orphelins),
`recommended` et les raisons exactes. Le timer user
`lancedb-viz-maintenance.timer` consulte ce plan chaque heure et appelle le POST
confirmé uniquement lorsqu'un seuil est dépassé. L'apply publie atomiquement un
backup `lancedb-pre-compact-*` avant toute compaction ou suppression, retient les
deux derniers backups gérés, compacte les quatre tables, supprime seulement les
UUID d'index absents des métadonnées Lance courantes, puis vérifie chaque table.
Un échec expose `failed_step` et, si créé, `backup_created` pour le recovery ; il
n'y a jamais de restauration automatique.

## lancedb_update tool (2026-06-22)

Nouvel outil MCP exposé au plugin : `lancedb_update`. Permet d'éditer une mémoire **in-place** au lieu de delete+recreate.

**Schema :**
- `memory_id` (required) — ID de la mémoire à update
- `content` (optional) — nouveau content, déclenche re-embed + entity extraction + relation rewrite
- `category` (optional) — nouvelle catégorie
- `tags` (optional) — nouveaux tags (remplace entièrement)
- `quality` (optional) — override manuel de quality (0-1)
- `type` (optional) — nouveau sub-type

**Quand utiliser :** Corriger une info fausse, mettre à jour le content, recategoriser, re-tagguer.
**Quand NE PAS utiliser :** Pour une nouvelle info -> `lancedb_add`. Pour supprimer -> `lancedb_delete`.
**Comportement :** `store.update(memory_id, **kwargs)` fait le re-embed + re-extract entities + re-write relations. Retourne la mémoire mise à jour.

### API LanceDB 0.34.0

```python
store._ensure_fts_index(tbl)  # writer explicite, une fois en fin de batch

# Hybrid query (vector + BM25 fusion RRF)
tbl.search(query_type='hybrid').text(query).vector(vec).limit(top_k).where(...)
```

### Ce qui a été patché

**`_init_table()`** : ouvre/crée la table mais ne touche jamais l'index FTS. Un
reopen, un search ou une lecture du viz ne doit créer ni version, ni fragment,
ni dossier `_indices`.

**`write_batch()` / `_serialized_mutation`** : le writer prend le verrou
inter-processus, regroupe les mutations imbriquées et appelle
`_ensure_fts_index(tbl)` une seule fois à la fin si la version de `memories` a
changé. Les scripts qui écrivent directement dans la table doivent entourer le
batch avec `store.write_batch()` ; ne jamais reconstruire dans un reader.

LanceDB 0.34.0 inclut les fragments non encore indexés dans une recherche FTS,
ce qui préserve le rappel pendant le batch. Mesure synthétique : une ligne
nouvelle est retrouvée avant et après le refresh ; le test de régression garde
ce contrat explicite au lieu de le supposer.

**`search()`** : utilise `query_type='hybrid'` avec `.text(query).vector(vector)`. Fusion RRF entre BM25 et cosine similarity. Score exposé via `_relevance_score` (champ LanceDB hybride) avec fallback `_distance`.

**Score mapping :**
- `mem["score"] = r.get("_relevance_score", 1.0 - mem.get("_distance", 0.0))`
- Les scores hybrides sont typiquement 0.015-0.035 (RRF normalisé) — plus serrés que les scores vectoriels purs (0.0-1.0) mais plus discriminants.

### Impact
- BM25 seul : recall ~66.5% (bench GBrain)
- Vector seul : recall ~17.7% P@5
- Hybrid + RRF : recalls ~97.9% (vector + BM25 fusionnés)
- 0 coût API, 0 latence additionnelle (index Tantivy pré-calculé)

### Pitfall : `query_type='hybrid'` crée une `LanceHybridQueryBuilder`, pas `LanceVectorQueryBuilder`
- `.where()`, `.limit()`, `.to_list()` marchent pareil
- `.text(q)` pour le FTS, `.vector(v)` pour le vector
- Pas de `.hybrid_search()` — cette méthode n'existe pas. C'est le `query_type='hybrid'` qui active le mode hybride.

## Auto-Tags (2026-06-13 — curated `_select_tags()`)

Les tags sont **extraits automatiquement des entités** lors de `lancedb_add()` via `_select_tags()` :

```python
safe_entities = [e for e in entities if _is_valid_tag(e)]
auto_tags = tags if tags is not None else _select_tags(safe_entities)
```

Le système de curation score chaque entité sur 3 niveaux :

| Score | Type | Exemples |
|-------|------|----------|
| 3 | Tech keywords connus (matching `_TECH_KEYWORDS`) | `hermes`, `docker`, `python`, `godot`, `steam` |
| 2 | Noms de projet CamelCase (≥5 chars) ou acronymes ALL CAPS | `BloodReaver`, `SpawnDirector`, `VIGIL` |
| 1 | Autres mots non-bloqués | gardés si place disponible |

Un bloc `_TAG_NOISE_WORDS` (~140+ mots) filtre les mots génériques français/anglais : verbes, adverbes, noms communs (`toujours`, `setup`, `format`, `gateway`, `sans`, `clean`...).

Pas besoin de gérer les tags manuellement. Les filtres `_is_valid_tag()` et `_select_tags()` s'appliquent automatiquement à chaque `lancedb_add()`.

**2026-06-13 cleanup complet** :
1. Nettoyage initial `_is_valid_tag()` : 87 entries, 144 bad tags, 230 entités pourries
2. Script de retag massif avec `_select_tags()` : 225 entries retaggées, 0 noise tags restants

**Script de retag existant** : `/tmp/retag_all_entries.py` — lit tout le content, re-extract les entities, re-`_select_tags()`, update la colonne `tags`. À rerun si `_TAG_NOISE_WORDS` ou `_select_tags()` changent.

**⚠️ Pitfall : les updates batch de tags nécessitent un `docker restart lancedb-viz`** pour que le viz reflète les changements (voir plus haut).

**Auto-Quality Score + Decay Curve (2026-06-22)**

La qualité est recalculée à chaque `get_by_id()` via `_compute_quality(memory)` :

| Facteur | Bonus | Condition |
|---------|-------|-----------|
| Accès fréquent | +0.1 à +0.2 | ≥2 / ≥5 / ≥10 accès |
| Liens entités | +0.05/lien | Max +0.15 |
| Fraîcheur création | +0.1 / +0.05 | < 7 jours / < 30 jours |
| **Decay (accessed_at)** | **factor 0.985^jours** | **Perte lente depuis dernier accès** |

Base = 0.5, clampé [0.1, 1.0]. Recalculé dynamiquement à chaque fetch.

**Decay curve (inspiré de PMB) :** Le decay utilise `accessed_at` (dernier accès), pas `created_at`. Une entree accedee hier reste fresh meme si creee il y a 6 mois. Factor 0.985/jour -> ~50% importance apres 46 jours sans accès, ~25% apres 93 jours. Le quality score final = (quality * decay_factor) + residual. Une entree T1 non-accedee depuis 100 jours tombe a 0.17. La remonte en quality se fait en re-accedant (get_by_id/search).

Exemples (access_count=3, 0 links) :
- Accessed now : 0.70
- Accessed 100d ago : 0.17
- Accessed 365d ago : 0.10 (floor)
- 15 access + 3 links + accessed now : 0.85
- 15 access + 3 links + accessed 90d ago : 0.26

## Search Context Enrichment — quality + relations au fetch

Quand tu fais un `lancedb_search()`, ne t'arrête pas aux résultats bruts. Deux étapes pour enrichir :

### 1. Quality-aware ranking

Le score de `lancedb_search()` est cosine similarity pure. Les entrées avec qualité faible peuvent être du bruit, même avec un bon score.

**Workflow :**

```
1. lancedb_search(query)
2. Filtrer : garder les résultats avec quality >= 0.3 (en dessous c'est du bruit)
3. Re-ranking simple : score_final = cosine_score * 0.7 + quality * 0.3
4. Prendre le top 5 du re-ranking comme "best guesses"
```

Implémentation rapide (post-processing à la main, pas dans le moteur LanceDB) :

```python
results = lancedb_search("mon sujet")
scored = []
for r in results.get("results", []):
    cos = r.get("score", 0)
    qual = r.get("quality", 0.5) or 0.5
    if qual < 0.3:
        continue  # skip le bruit
    scored.append((cos * 0.7 + qual * 0.3, r))
ordered = [r for _, r in sorted(scored, key=lambda x: -x[0])]
```

**Pourquoi pas juste quality ?** La cosine similarity est plus discriminante sur le sujet exact. La qualité est un boost correctif — pas un remplacement.

### 2. Relation-following — context traversal

Les résultats du search peuvent avoir des `relations` et `links` vers d'autres entrées. Suis-les comme on le fait à l'écriture.

**Workflow :**

```
1. lancedb_search(query) → top 3-5
2. Pour chaque résultat, lire relations[] et links[]
3. Si relations présent → lancedb_search() sur les cibles (label matching)
4. Si links présent → IDs direct, fetch avec get_by_id()
5. Enrichir la réponse avec les contextes secondaires
```

**Priorité relations > links** : les relations typées (depends, part_of) sont directionnelles et portent du sens. Un simple link cosine est bidirectionnel et moins informatif.

### 3. Exemple complet

```python
# 1. Search + quality rerank
results = lancedb_search("Ollama embedding")
scored = []
for r in results.get("results", []):
    cos = r.get("score", 0)
    qual = r.get("quality", 0.5) or 0.5
    if qual < 0.3: continue
    scored.append((cos * 0.7 + qual * 0.3, r))
ordered = [r for _, r in sorted(scored, key=lambda x: -x[0])][:3]

# 2. Follow relations
context = []
for r in ordered:
    context.append(r)
    for rel in (r.get("relations") or []):
        # rel = {"type": "depends", "target": "Ollama"}
        follow = lancedb_search(rel["target"])
        context.extend(follow.get("results", [])[:2])

# 3. Réponse enrichie avec tout le contexte
# → plus de liens entre les infos, meilleure réponse
```

**Règle :** Toujours enrichir au moins 1 niveau de relations. Si le résultat principal parle de `depends=Ollama`, fetch la cible pour avoir le setup complet. Le silence sur un sujet = échec du proactif.

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

### Pitfall: `store.delete()` is extremely slow (110-200s per call at 460+ rows)

Each `store.delete(memory_id)` triggers a full link rebuild across ALL entries — it re-scans every memory to recompute entity links. The cost scales with the row count: 30-90s at 230 rows, **108-195s measured at 463 rows (12/09/2026)**.

**Symptôme :** Batch deletes in a loop timeout. `terminal` calls with `timeout=120` hang. For 10+ deletes, even a 420s foreground call is not enough — a 14-delete batch needs ~35 minutes total.

**Fix :** Never delete in a session loop. Write the delete list to a file and run the whole batch as a **background** process (`nohup ... &`, one delete per iteration, `flush=True` on each print), then poll the log. A per-delete `terminal` call is only viable for 1-3 deletes with `timeout=300` minimum:
```python
# ONE delete per Python invocation:
cd ~/.hermes/hermes-agent && timeout 90 venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore('/home/elo/.hermes/lancedb')
store.delete('UUID-HERE')
print('deleted')
"
```
**Do NOT attempt to batch them in one foreground process.** Foreground batches hit the tool timeout mid-loop; the work continues only if the process is detached (`nohup`, background=true), and results must be read back from a log file.

**Dedupe triage (measured 12/09/2026, 463 rows):** vector similarity alone is NOT proof of a duplicate. At threshold 0.92 there were 8 groups and at 0.88 there were 24, but most groups were the same *template* with different subjects (same `Domaine:Sujet` shape, different content). Read every pair before deleting. Safe procedure: enrich the keeper first (`update_memory` with the union of the facts, or `add_memory` + delete when the keeper's content is legacy and the contract refuses the patch), then delete the loser, then re-embed both sides. `update_memory` REJECTS a patch on a row whose stored content has no `Domain:Subject` prefix (`legacy content must start with Domain:Subject`) — for those rows, create a fresh entry via `add_memory` and delete the legacy one instead of trying to repair it in place.

**Prévention :** Design cron jobs and cleanup scripts to avoid frequent deletes.
Pour un update raw indispensable, utiliser `with store.write_batch():` afin de
prendre le verrou partagé et de rafraîchir le FTS une seule fois. Only use
`delete` for actual removal of obsolete/duplicate entries.

### Pitfall: Version bloat et index FTS remplacés

**Mise à jour 2026-07-05 :** `cleanup_old_versions()` est déprécié depuis LanceDB 0.21.0. Remplacé par `Table.optimize()` mais **attention : `tbl.optimize()` sans argument est un no-op pour le cleanup de versions** — il ne fait que réécrire les fichiers de données. Pour purger les vieilles versions, il faut explicitement `cleanup_older_than=timedelta(seconds=0)`.

**Cause supplémentaire vérifiée 12/09/2026 :** chaque
`create_index(..., replace=True)` crée un nouvel UUID sous `_indices` sans
effacer le précédent. `optimize(cleanup_older_than=0)` compacte versions et
fragments mais crée lui-même un nouvel index et ne retire pas ces UUID abandonnés.

**Signature complète :** `tbl.optimize(*, cleanup_older_than: Optional[timedelta] = None, delete_unverified: bool = False, retrain: bool = False)`. Sans `cleanup_older_than`, les versions s'accumulent indéfiniment.

**Legacy (LanceDB <0.21) :** `tbl.cleanup_old_versions(timedelta(seconds=0))` — nécessite pylance. `tbl.optimize()` n'existait pas encore.

### Maintenance sûre après batch-writes

LanceDB garde une version de table à chaque écriture. Mesure du 12/09/2026 avant
compaction : **15 311 versions**, 15 308 fragments et 131 dossiers FTS pour 463
lignes. Le process responsable du volume exact n'est pas identifié ; ne pas
l'attribuer au cron observé sans nouvelle preuve processus.

**Symptôme :** `du -sh ~/.hermes/lancedb/memories.lance/` montre 400-600 MB pour seulement ~300 entrées. Le dossier `_versions/` fait la majorité.

**Fix actuel :** ne jamais lancer un `optimize()` ou supprimer `_indices` à la
main. Utiliser le plan et la route backup-first :

```bash
curl -fsS http://127.0.0.1:7777/api/maintenance/compact/plan | python3 -m json.tool
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data '{"confirmed":true}' http://127.0.0.1:7777/api/maintenance/compact \
  | python3 -m json.tool
```

Le timer versionné automatise exactement cette décision. Le store et la route
partagent `~/.hermes/lancedb/.write.lock`, donc les writers coopérants attendent
pendant la compaction. Tout writer raw qui n'utilise pas `store.write_batch()`
doit être arrêté manuellement. La suppression d'orphelins est sautée et
rapportée `None` si l'API ne permet pas d'obtenir les UUID actifs : aucun chiffre
ni dossier n'est deviné.

### Pitfall: Entree sans [Tier=N] — jamais filtrable, jamais visible correctement

Quand une memoire est ajoutee sans marqueur `[Tier=N]`, elle est:
- Invisible dans le filtre Tier (T1/T2/T3 checkboxes du Graph ne l'affichent pas)
- Classee `"none"` dans le dashboard (compteur `Tier none`)
- Invisible dans `_compute_vector_data()` qui parse le tier du content pour l'attribut `n.tier`

**Symptôme:** Le dashboard montre `Tier none: 1` (ou N), et le graph ignore ces entrees dans les filtres tier.

**Detection:**
```bash
python3 -c "
import sys
sys.path.insert(0, '/home/elo/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore('/home/elo/.hermes/lancedb')
raw = store._get_all_raw()
no_tier = [r for r in raw if '[Tier=' not in r.get('content','')]
print(f'{len(no_tier)} without [Tier=]')
for r in no_tier:
    print(f'  {r[\"content\"][:70]}')
"
```

**Fix:** Ajouter `[Tier=N]` a la fin du content + re-embed :
```python
import sys, numpy as np, httpx
sys.path.insert(0, '/home/elo/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore('/home/elo/.hermes/lancedb')
raw = store._get_all_raw()
for r in raw:
    content = r.get('content', '')
    if '[Tier=' not in content:
        content_fixed = content.rstrip() + ' [Tier=1]'
        store._table.update(where=f"id = '{r['id']}'", values={'content': content_fixed})
        resp = httpx.post('http://localhost:11434/api/embed',
            json={'model': 'nomic-embed-text', 'input': [content_fixed[:4096]], 'keep_alive': '30s'}, timeout=30)
        vec = np.array(resp.json()['embeddings'][0], dtype=np.float32)
        vec = vec / np.linalg.norm(vec)
        store._table.update(where=f"id = '{r['id']}'", values={'vector': vec.tolist()})
        print(f'  Fixed: {r[\"id\"][:12]}')
print('Done. Cliquer sur ↻ dans l\\'UI pour voir le changement.')
```

### Pitfall: Zero-vector entries (Ollama down au write) — graph silencieux

Quand `_embed()` dans `store.py` echoue (Ollama unreachable, timeout, model pas charge), le fallback renvoie `np.zeros(768, dtype=np.float32)`. L'entree est **ecrite dans la DB normalement** (content, category, tier OK) mais le vecteur est nul.

Le graph (`server.py` → `_compute_vector_data()`) filtre ces entrees : `norm = np.linalg.norm(vec_arr)` et `if norm < 0.001: continue`. **Silencieusement** — zéro erreur, zéro log, juste un nœud qui n'apparaît pas.

**Symptôme :** Le compteur total du dashboard dit 228, le graph montre 209. La différence = entrées à vecteur nul.

**Symptôme bis :** `docker restart lancedb-viz` ne change rien (les données sont déjà bonnes sur disque — c'est le vecteur qui est nul, pas un cache).

**Détection rapide :**
```bash
python3 -c "
import sys, numpy as np
sys.path.insert(0, '/home/elo/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore('/home/elo/.hermes/lancedb')
raw = store._get_all_raw()
z = [r for r in raw if r.get('vector') is not None and isinstance(r['vector'], (list, np.ndarray)) and np.linalg.norm(np.array(r['vector'], dtype=np.float32)) < 0.001]
print(f'{len(z)} zero-vector entries out of {len(raw)}')
for r in z[:5]:
    print(f'  {r[\"content\"][:70]}')
"

# Meme chose depuis le Docker:
docker exec lancedb-viz python3 -c "
import sys, numpy as np
sys.path.insert(0, '/home/hermes/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore('/home/hermes/.hermes/lancedb')
raw = store._get_all_raw()
z = [r for r in raw if r.get('vector') is not None and isinstance(r['vector'], (list, np.ndarray)) and np.linalg.norm(np.array(r['vector'], dtype=np.float32)) < 0.001]
print(f'{len(z)} zero-vectors')
"
```

**Fix :** Re-embed via Ollama direct + restart Docker :
```bash
python3 << 'PYEOF'
import sys, numpy as np, httpx
sys.path.insert(0, '/home/elo/.hermes/hermes-agent')
from plugins.memory.lancedb.store import LanceDBStore
store = LanceDBStore('/home/elo/.hermes/lancedb')
raw = store._get_all_raw()
to_fix = []
for r in raw:
    vec = r.get('vector')
    if vec is not None and isinstance(vec, (list, np.ndarray)):
        norm = np.linalg.norm(np.array(vec, dtype=np.float32))
        if norm < 0.001:
            to_fix.append((r['id'], r['content']))
print(f"Fixing {len(to_fix)}...")
for mem_id, content in to_fix:
    resp = httpx.post("http://localhost:11434/api/embed",
        json={"model": "nomic-embed-text", "input": [content[:4096]], "keep_alive": "30s"}, timeout=30)
    emb = resp.json()["embeddings"][0]
    vec = np.array(emb, dtype=np.float32)
    vec = vec / np.linalg.norm(vec)
    store._table.update(where=f"id = '{mem_id}'", values={"vector": vec.tolist()})
    print(f"  {mem_id[:12]} — norm=1.0")
print("Done. Restart Docker:")
print("docker restart lancedb-viz")
PYEOF
```

**Prévention :** Le script `scripts/reembed-entries.py` fait le même travail en batch — l'utiliser après toute migration de contenu > 5 entrées. Pour un quick-fix < 20 entrées, le one-liner ci-dessus est plus rapide.

## Pitfall: Update Hermes (desktop app) wipe le runtime plugin — FIX DURABLE (06/09/2026)

**Symptôme :** après un update hermes-agent (merge origin/main, ex: 05/09 14:59 via l'app desktop), `plugins/memory/lancedb/` est VIDE. Symptômes en cascade : viz `/api/stats` → `"No module named 'plugins.memory.lancedb.store'"`, tools `lancedb_*` absents de l'agent (provider init avec store=None au démarrage du gateway), `lancedb_list` → `'NoneType' object has no attribute 'get_all'`.

**Cause :** lancedb n'a JAMAIS été tracked upstream (`git log --all -- plugins/memory/lancedb/` = vide). Le merge fast-forward de l'update nettoie les fichiers untracked du tree runtime. L'app desktop déclenche ces updates (git pull + restart gateway).

**Architecture durable (06/09/2026) :**
1. **Canonique = `~/.hermes/plugins/lancedb/`** (dir user, survit aux updates, scan par la discovery memory-provider comme fallback bundled). Sync depuis `~/github/hermes-lancedb-viz/plugin/`.
2. **Copie runtime `plugins/memory/lancedb/`** = bundled-first (precedence), mais fragile. Restaurer après wipe : `cp ~/github/hermes-lancedb-viz/plugin/{store.py,__init__.py,plugin.yaml} ~/.hermes/hermes-agent/plugins/memory/lancedb/`.
3. **server.py viz : `_import_store_module()`** importe le store canonique-first (spec_from_file_location, sys.modules cache), runtime en fallback. 3 call sites patchés (`_get_store`, graph guard, `_STOP_ENTITIES`).
4. **Viz = Docker via le Hub** : conteneur `lancedb-viz`, port **7777**, bind mounts depuis `~/.hermes/lancedb-viz/`. Restart : `docker restart lancedb-viz`. L'unité systemd 7778 est uniquement un secours désactivé.

**Validation fallback (testée) :** runtime renommé → `load_memory_provider('lancedb')` charge depuis le dir user → `initialize(session_id=...)` → store OK → `lancedb_list` 426 résultats, search OK. Le provider survit donc aux wipes.

**Recovery complet après wipe :**
```bash
# 1. Restaurer runtime (precedence bundled)
cp ~/github/hermes-lancedb-viz/plugin/{store.py,__init__.py,plugin.yaml} ~/.hermes/hermes-agent/plugins/memory/lancedb/
# 2. Viz OK direct (server.py pointe canonique) mais restart propre:
systemctl --user restart lancedb-viz
# 3. Gateway restart OBLIGATOIRE (provider a caché store=None au boot):
#    bloqué depuis l'intérieur du gateway (guard anti-self-restart SIGTERM).
#    Contourner via systemd-run (cgroup séparé, survit au kill):
systemd-run --user --collect --unit=xana-gw-restart bash -lc 'sleep 2 && systemctl --user restart hermes-gateway.service'
#    Si le guard bloque le contenu → demander à elo de taper /restart sur Discord.
```

**Après gateway restart :** les tools `lancedb_*` réapparaissent à la prochaine session (toolset chargé au boot). Vérif: `grep -i lancedb ~/.hermes/logs/agent.log | tail`, puis curl `/api/stats`.

## Pitfall: Handles MVCC desynchronises — reads vides + updates perdus (FIXED 02/09/2026)

**Symptôme :** en pleine session, `lancedb_search/get/list` (tools MCP) retournent 0 résultats alors que la table contient 400+ rows (vérifiable via `store._get_all_raw()` en Python direct). Un `lancedb_update` via plugin "réussit" mais l'entrée disparaît (update commité depuis un vieux snapshot puis écrasée). `lancedb_list` retourne même 0 sur un store avec des données.

**Cause :** `LanceDBStore` cache `self._table` à l'init. LanceDB est MVCC : le handle est pinné sur la version ouverte à l'init. Les writes concurrents (cron, workers kanban, viz) avancent la table, mais le handle long-lived lit l'ancien snapshot et ses commits (update/delete) **écrasent silencieusement les writes récents**. Côté viz c'était déjà connu (pitfall singleton) — côté plugin/provider c'est le même bug.

**Fix (commit 7028365, repo hermes-lancedb-viz, poussé sur GitHub) :** helper `_fresh()` dans store.py qui appelle `checkout_latest()` (lancedb >= 0.21, fallback reopen) avant CHAQUE opération sur la table : add, update, delete, bulk_delete, get_by_id, _get_by_id_raw, _get_all_raw, count, search, graph (10 call sites). Runtime sync + `docker restart lancedb-viz` faits. Validé empiriquement : 2 handles concurrents, write par l'un, lecture par l'autre immédiate (avant : invisible + update perdu).

**Leçon :** avant de conclure à une perte de données LanceDB, toujours vérifier le row count direct (`store._get_all_raw()` en Python). Le symptôme "0 résultats" des tools ≠ table vide.

### Pitfall: Singleton store cache — restart Docker obligatoire après updates directs (mitigé juin 2026)

Le serveur viz (`server.py`) utilise un singleton `_get_store()` qui cree et garde une instance `LanceDBStore` en memoire (`_store_instance`). Les mutations cote serveur (POST /api/delete, POST /api/update, etc.) appellent `_reset_store()` correctement. Mais les **updates directs** sur la DB (re-embed via script, cleanup de tags, reclassifications batch) ne declenchent **jamais** `_reset_store()`.

**Symptôme :** Tu modifies les données sur disque, le compteur du dashboard dit toujours l'ancien nombre, les nouveaux nodes n'apparaissent pas dans le graph, les tags sont obsolètes.

**Fix #1 — Auto-refresh graph (patché juin 2026) :** `get_graph_data()` appelle maintenant `_reset_store()` en tête de fonction. Le graph est toujours frais sans restart. Les autres pages (dashboard, memories, stats) utilisent l'API cache avec TTL 30s, donc voient les changements dans la demi-minute.

**Fix #2 — Bouton Refresh ↻ (patché juin 2026) :** Un bouton **↻** a été ajouté dans la topbar à côté de Export/Import. Il appelle `GET /api/refresh` qui déclenche `_reset_store()`, puis recharge la page active (la même que la navigation). Fonctionne sur Dashboard, Memories, Timeline, Tags, Duplicates, Embeddings, Clusters, Stale, Graph. Le bouton affiche ⟳ pendant le chargement puis revient à ↻ après 800ms.

**Fix #3 — L'API `/api/refresh` :**
```
GET /api/refresh → {"status": "ok"}
```
Appelle `_reset_store()` côté serveur : drop le singleton `_store_instance`, invalide le cache stats. Le prochain appel à tout endpoint API recréera un store frais depuis la DB. Utilisable aussi en curl :
```bash
curl -s http://localhost:7777/api/refresh
```

**Règle :** Pour un refresh rapide après script externe → cliquer ↻ dans l'UI. Pour un batch lourd de modifs → `docker restart lancedb-viz` reste la méthode la plus propre (reset complet des imports, threads, connexions DB).

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
- **`_parse_relations()` premier match vs dernier** : le marqueur `::relations::` apparait parfois dans le texte (ex: "store.py parse ::relations:: au write"). La regex `re.search()` trouve le premier match = faux positif dans le contenu. **Fix:** `list(re.finditer(...))[-1]` pour prendre le dernier occurrence — celui apres `[Tier=N]` qui est le vrai marqueur. Symptome: relations parasites avec des labels absurdes parsees depuis le corps du texte.\n- **Deps manquantes dans le venv** : si `lancedb` + `pyarrow` ne sont pas installes dans `~/.hermes/hermes-agent/venv/`, le plugin `LanceDBMemoryProvider` ne crash PAS — il s'initialise silencieusement avec `self._store = None`. Tous les tools (`lancedb_search`, `lancedb_add`, `lancedb_graph`) renvoient `'NoneType' object has no attribute 'search/add/graph'`. Pas de trace dans les logs car le constructeur ne lance pas d'exception. **Symptome :** tools disponibles (schemas charges), mais erreur `NoneType` sur chaque appel. **Diagnostic:** `cd ~/.hermes/hermes-agent && venv/bin/python -c "import lancedb; import pyarrow; print('OK')"` — si le module manque, le pip list montre leur absence. **Fix :** `cd ~/.hermes/hermes-agent && venv/bin/pip install -r ~/github/hermes-lancedb-viz/requirements.txt`, puis restart du gateway. Les 212+ memoires existantes ne sont pas perdues — le store les retrouve automatiquement.\n- **Import manquant dans `__init__.py`** : `from .store import LanceDBStore` peut disparaitre (rebased, cherry-pick, etc.). Meme symptome que les deps manquantes — provider s'initialise avec `self._store = None`, tools renvoient `NoneType`. **Diagnostic:** log agent `WARNING agent.memory_manager: provider 'lancedb' initialize failed: name 'LanceDBStore' is not defined`. **Fix:** re-ajouter l'import et restart gateway. Verification avec `cd ~/.hermes/hermes-agent && venv/bin/python -c "from plugins.memory.lancedb import LanceDBMemoryProvider; print('OK')"`.\n- **Category invalide pas filtree** : le schema enum a 5 categories (`project|tech|fact|correction|user_pref`) mais seul le LLM le respecte — aucune validation backend dans `_handle_add()`. Une categorie orpheline comme `"architecture"` passe, donne 0 entites, 0 liens, jamais matchée par les searches filtrées. **Fix:** fallback a `"fact"` dans `_handle_add()` si category pas dans `VALID_CATEGORIES`.\n- **Entity noise — keywords ≤2 chars** : `c`, `ts`, `js`, `rg`, `fd`, `sh`, `go` dans `_TECH_KEYWORDS` matchent en substring (`if kw in lower`). Dec 2026-06-07: `c` matche dans tout texte contenant la lettre c (100% des textes francais), `ts` matche dans "statuts", "contents", etc. 30 memoires polluees, 935 entites noise, ~329 faux liens. **Fix:** `len(kw) < 3: continue` dans `store.py:extract_entities()`. Apres fix: re-extract entities sur toutes les memoires + rebuild links + re-embed complet avec nomic.\n- **Stale browser cache faussant les couleurs** : si l'utilisateur signale "cette memoire est pas de la bonne couleur" alors que la DB a deja la bonne categorie, faire Ctrl+F5 (hard refresh). Le serveur sert `Cache-Control: no-cache` mais les navigateurs peuvent render des donnees stale du cache memoire.

### Pitfall: Empty manifests (0 bytes) — `Invalid range 0..0 for object of size 0 bytes`

Quand un write LanceDB echoue au moment d'ecrire le manifest (crash, disk full, cleanup_old_versions qui mal tourne), le fichier `.manifest` dans `_versions/` reste a **0 bytes**. Le `latest_version_hint.json` pointe vers cette version vide. Au prochain `open_table()`, LanceDB lit le manifest vide et plante: `LanceError(IO): Generic memory error: Invalid range 0..0 for object of size 0 bytes, .../lance-*/src/dataset.rs`.

**Symptome:** `open_table('memories')` plante cote host ET cote Docker viz. Les outils MCP `lancedb_search`/`lancedb_add`/`lancedb_graph` retournent `'NoneType' object has no attribute 'search/add/graph'` (le provider s'init avec store=None silencieusement). Les data files (.lance) sont intacts sur disque.

**Diagnostic:**
```bash
# Trouver les manifests vides
find ~/.hermes/lancedb/memories.lance/_versions/ -name "*.manifest" -size 0
# Si > 0, c'est la cause
```

**Mapping version LanceDB -> nom de manifest:**
Les manifests sont nommes en u64 decroissant: `18446744073709551615 - version_number + 1`. Exemple: version 22416 -> manifest `18446744073709529199.manifest`. Pour trouver la version valide la plus recente: prendre le manifest non-vide avec le numero le plus eleve.

**Fix (voir `references/empty-manifest-recovery.md` pour le script complet):**
1. Backup: `cp -r ~/.hermes/lancedb ~/.hermes/lancedb-backup-$(date +%Y%m%d-%H%M%S)`
2. Supprimer les manifests vides: `find ... -name "*.manifest" -size 0 -delete`
3. Mettre `latest_version_hint.json` vers la derniere version valide: `echo '{"version":N}' > .../latest_version_hint.json`
4. `docker restart lancedb-viz`
5. Restart le gateway Hermes (le provider a cache store=None et ne retry pas)

## Recovery — Plugin files wiped, graph shows empty/no data (Docker 7777)

**Symptom:** Docker container up (healthy), DB present in `~/.hermes/lancedb/memories.lance/`, but `/api/stats` and `/api/graph` return `{"error": "LanceDB plugin not found"}` with 0 nodes/edges.

**Root cause:** The 3 plugin files under `plugins/memory/lancedb/` (store.py, __init__.py, plugin.yaml) were deleted or emptied. The viz server imports `from plugins.memory.lancedb.store import LanceDBStore` and fails silently, returning the fallback error dict.

**Diagnosis:**
- Check docker is really up: `docker ps --filter name=lancedb-viz`
- Check the API returns an error (not empty data): `curl -s http://localhost:7777/api/stats | python3 -c "import sys,json; d=json.load(sys.stdin); print('error:', d.get('error','none'))"`
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
curl -s http://localhost:7777/api/stats | python3 -m json.tool
curl -s http://localhost:7777/api/health | python3 -m json.tool
```

## Format des entrees

**-> Voir `memory-writing` skill.** Les regles de format (Domaine:Sujet, cle=valeur, [Tier=N], linking implicite, thresholds) sont la-bas. Pas de duplication ici.

## GitHub Repo

Le code source est versionné sur `3L0935/hermes-lancedb-viz` (**public**, MIT license) dans `~/github/hermes-lancedb-viz/`. Les fichiers Docker, server, static et scripts y sont maintenus. **Les changements dans les bind mounts Docker ne sont pas automatiquement synchronisés avec le repo** — voir `references/repo-sync-workflow.md` pour le workflow de sync.

### Public-readiness checklist (2026-06-24)

Le repo a ete audite et prepare pour public:
- docs/plugin/ supprime (duplicate stale sans lancedb_update tool)
- docs/skills/ reecrit en anglais (memory-writing + lancedb-memory-system)
- docs/setup.md reecrit en anglais
- README.md public-ready avec quick start, tool reference, MIT license
- LICENSE (MIT) ajoute
- Toutes refs perso supprimees (Bodycam, CrowdWhisper, session_search, chemins absolus)
- Net: -2249 lines

**Pitfall: docs/plugin/ vs plugin/**. Ne JAMAIS recreer docs/plugin/ — c'etait une copie stale qui manquait le tool lancedb_update (7 tools au lieu de 7). Le code canonique est dans `plugin/` a la racine du repo.

## References (gardees)

- `references/repo-sync-workflow.md` — structure du repo GitHub, sync workflow, pitfalls
- `references/freshness-model.md`
- `references/typed-edges-viz.md`
- `references/lance-fork-warning-suppression.md`
- `references/stats-race-condition-fix.md`
- `references/reconstruction-pattern.md`
- `references/entity-extraction-limits.md`

Les autres references (entity-extraction, hub-lessons, vector-vs-entity-links, viz-physics, viz-ui) -> supprimees. Stale.
