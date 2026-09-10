---
name: memory-writing
description: "Règles pour écrire dans LanceDB via lancedb_add. Format, quoi mettre, quoi éviter."
version: 7.0.0
triggers:
  - lancedb_add
  - write memory
  - memory add
  - memory cleanup
  - clean memory
  - outdated memory
  - memory full
---

# Memory Writing — Utilisation de lancedb_add

Le tool a ete etendu (v5) — il accepte maintenant les champs structures `domain`, `subject`, `tier` en plus du `content` libre. **PREFERER la syntaxe structuree** : le handler assemble automatiquement le format correct.

## Strict write contract (10/09/2026)
Le plugin enforce maintenant un write contract: domain/subject/facts/tier/category requis, category invalide = REJET (plus de fallback fact), sujet trop large = warning, fingerprint exact = idempotent (re-add du meme contenu retourne l'ID existant), meme sujet avec claims conflictuelles = blocage, embedding echoue = erreur retryable (jamais de zero-vector). Legacy store.add(str) = interdit hors migration. Content avec prefixe [Tier=] ou Domain: duplique = rejet. Toute la normalisation est automatique cote store (NFKC, trim, casing).

## Structure recommandee (v6.1 — strict contract)

```python
lancedb_add(
    domain="Hermes",        # namespace : Hermes, Projet, Tech, User, Correction, Config
    subject="Kanban",        # sujet specifique (PAS trop large: Correction:Hermes-Dashboard, pas Correction:Hermes)
    content="key=value autre=info",  # ou facts=[...] — le corps, sans prefixe ni suffixe
    tier="1",                # obligatoire : 1, 2 ou 3
    category="tech"          # Categorie pour le graphe
)
```

Le handler produit automatiquement : `Hermes:Kanban key=value autre=info [Tier=1]`

## Syntaxe legacy (toujours supportee)

```python
lancedb_add(content="Hermes:Kanban key=value autre=info [Tier=1]", category="tech")
```

Mais **les nouveaux champs sont preferes** — pas de risque de mauvais format.

## Quand utiliser LanceDB

Tout ce qui est technique, factuel, durable. Projets, bugs, corrections, stack, préférences, décisions, patterns, références.

## Quand utiliser memory() à la place

Ce qui définit qui est l'utilisateur : nom, OS, GPU, personnalité, conventions durables. Le built-in (~2200 chars) est injecté à chaque session — pas de technique dedans.

Test : "c'est qui l'utilisateur ?" → memory(). "Comment fixer X ?" → lancedb_add().

## Built-in Memory Maintenance (periodic cleanup)

Les built-in MEMORY.md et USER.md se remplissent vite. La règle "tech → LanceDB, user → built-in" dérive avec le temps. Prévoir un cleanup quand l'usage dépasse 85%.

**Emplacement actuel (Hermes ≥v0.18) :** `~/.hermes/memories/MEMORY.md` et `~/.hermes/memories/USER.md`. Plus à `~/.hermes/memory.md` / `~/.hermes/user.md` (anciens chemins, obsolètes).

**Vérifier l'occupation :**
```bash
wc -c ~/.hermes/memories/MEMORY.md ~/.hermes/memories/USER.md
```
Capacité max ~2500 chars pour MEMORY.md, ~2500 pour USER.md. Au-delà de 85% (~2125 chars), déclencher un cleanup.

**Vérifier le provider actif :**
```bash
hermes memory status
```
Affiche le provider (lancedb, builtin, etc.) et les plugins installés.

### Workflow de cleanup (MEMORY.md)

1. **Scanner** chaque entrée : est-ce que c'est TECHNIQUE (chemins, versions, configs, URLs, projets) ou DURABLE (style élo, meta-règles, préférences fondamentales) ?
2. **Sauver dans LanceDB** tout ce qui est technique avec le format structuré :
   ```
   lancedb_add(domain="Tech", subject="ComfyUI", content="path=/ssd/comfyui/ venv=rocm6.3", tier="2", category="tech")
   ```
3. **Supprimer de MEMORY.md** en éditant le fichier `~/.hermes/memories/MEMORY.md` — retirer les lignes techniques, une par une. Chaque entrée est séparée par `§` (ligne vide avec `§` seul). Utiliser `patch` ou `write_file` pour réécrire le fichier nettoyé.
4. **Garder une mini-ligne** pour les trucs utiles en session sans encombrer : ex "Atomic Mail MCP (npx @atomicmail/mcp). Inbox: xana_hermes@atomicmail.ai."

### Workflow de cleanup (USER.md)

1. **Chercher les redites** : si deux entrées disent la même chose (ex "UI préf mobileAgent" qui répète "UI general"), fusionner en une.
2. **Migrer les borderlines** : les préférences techniques sur des projets spécifiques (ex "Veut workflows ComfyUI V2 avec ControlNet") peuvent aller dans LanceDB en `user_pref` :
   ```
   lancedb_add(domain="Pref", subject="ComfyUI", content="workflows V2 ControlNet required deep research avant chaque création", tier="2", category="user_pref")
   ```
3. **Règle USER.md** : si c'est pas intrinsèquement "qui est elo", ça part.

### Ce qui reste dans MEMORY.md (built-in)

- Style de travail ("fais le taf", pas 3x confirmation, debug racine)
- Méta-règles (built-in vs LanceDB, skills backup)
- Préférences durables (skills minimales, tasks scoped)
- Liens critiques compacts (Atomic Mail inbox, etc.)
- **Rien** qui soit un chemin, une version, une config, une URL longue, ou une procédure

### Ce qui reste dans USER.md (built-in)

- UI preferences (OLED, glass bubbles, drawer+burger)
- Workflow ("fais le taf", no wrapper, tasks scoped)
- Voix préférée (Suno/YuE chant, pas TTS)
- **Rien** qui soit projet-spécifique ou technique

### Quand déclencher un cleanup

- MEMORY.md > 85% full (~2125 chars)
- USER.md > 80% full (~2000 chars)
- À la demande du user ("nettoie la mémoire", "enlève les trucs tech")
- Après une session qui a ajouté 3+ entry techniques dans le built-in

### ⚠️ Pitfall: Built-in delete requires user confirmation

**La suppression d'entrées du built-in est interdite sans confirmation explicite de l'utilisateur** (règle SOUL.md). En cron ou en session normale, tu peux migrer les entrées techniques vers LanceDB, mais tu dois **signaler dans ton rapport** que les entrées built-in correspondantes méritent d'être supprimées — ne les supprime pas toi-même.

**Workflow correct quand MEMORY.md > 85% :**
1. Migrer les entrées techniques vers LanceDB (carte blanche)
2. **Ne PAS supprimer** les entrées correspondantes de MEMORY.md
3. Signaler dans le rapport : "⚠️ Built-in: [sujet] mériterait d'être supprimé de MEMORY.md — confirmation nécessaire"
4. Attendre la confirmation de l'utilisateur pour agir

**Symptôme si tu ignores cette règle :** tu supprimes une entrée built-in que l'utilisateur considère importante, et l'info est perdue (pas de undo sur memory tool).

### ⚠️ Pitfall: MEMORY.md path change (Hermes ≥v0.18)

Depuis Hermes v0.18, les fichiers built-in sont dans `~/.hermes/memories/MEMORY.md` et `~/.hermes/memories/USER.md`. Les anciens chemins `~/.hermes/memory.md` et `~/.hermes/user.md` n'existent plus. Ne pas utiliser `memory(action="remove", ...)` — l'outil memory tool a été remplacé par l'édition directe des fichiers.

**Symptôme si tu utilises les anciens chemins :** `ls: cannot access '/home/elo/.hermes/memory.md'` — le fichier n'existe pas à cet emplacement.

**Fix :** Toujours utiliser `~/.hermes/memories/MEMORY.md` et `~/.hermes/memories/USER.md`. Vérifier avec `hermes memory status` pour confirmer le provider actif.

## Workflow avant chaque write

0. skill_view('memory-writing') — charger ce skill
1. Vérifier les duplicats + entités existantes à lier
   - **En session normale** : utiliser `lancedb_search()` (tool plugin)
   - **En cron/terminal-only** : `lancedb_search` et `lancedb_add` ne sont PAS disponibles (toolsets restreints). Fallback obligatoire via Python direct avec le CONTRACT STRICT (pas de raw add/update) :
     ```python
     cd ~/.hermes/hermes-agent && venv/bin/python3 -c "
     import sys; sys.path.insert(0, '.')
     sys.path.insert(0, '/home/elo/.hermes/hermes-agent/plugins/memory/lancedb')
     from plugins.memory.lancedb.store import LanceDBStore, MemoryWrite, MemoryPatch
     store = LanceDBStore('/home/elo/.hermes/lancedb')
     # READ: store.search('ma query', top_k=5) retourne une LIST de dicts
     # WRITE strict (obligatoire depuis le contract v2 — raw add(str) = fenced):
     result = store.add_memory(MemoryWrite.from_mapping({
         'domain': 'Hermes', 'subject': 'Sujet-Specifique',
         'facts': ['cle=valeur', 'phrase dense courte'],
         'tier': 2, 'category': 'tech', 'write_mode': 'create',
     }))
     # result = {'success': True, 'status': 'created|idempotent|update_suggested', 'memory_id': ...}
     # UPDATE strict:
     store.update_memory(MemoryPatch.from_mapping({'memory_id': '<uuid>', 'facts': [...], 'tier': 2}))
     "
     ```
2. Choisir les champs structures (obligatoire depuis le contract v2)
3. Écrire dans LanceDB
   - **En session normale** : `lancedb_add(domain=..., subject=..., facts=[...], tier=..., category=...)`
   - **En cron/terminal-only** : `store.add_memory(MemoryWrite...)` / `store.update_memory(MemoryPatch...)` — JAMAIS store.add(str) (fenced, raise legacy_api_disabled)
   - `write_mode` default = create ; `upsert_subject` = opt-in explicite, jamais de destruction silencieuse

**Toujours preferer les champs structures** : `domain`, `subject`, `tier` sont valides par le handler, le format est garanti.

## Format interne (assemble par le handler)

Le handler produit automatiquement :

    Domaine:Sujet corps_de_l_info [Tier=N]

Ne pas ecrire le prefixe `Domaine:` ni `[Tier=N]` dans le champ `content` — passe-les via `domain`, `subject` et `tier`.

## Catégories par priorité

correction > pattern > decision > user_pref > reference > insight > project > tech > fact > question

Prends la plus haute applicable.

## Tiers

Tier 1 = friction immédiate (bug, correction, commande critique)
Tier 2 = utile récurrent (stack, URLs, archi)
Tier 3 = contextuel (notes de fond)

## Relations (v7 : champ structuré recommandé)

Passe une liste `relations` à lancedb_add / lancedb_update (préférable au bloc legacy) :

```
relations=[{"type": "depends", "target_id": "<uuid>"}]
```

- `target_id` = UUID exact d'une mémoire existante (lancedb_search d'abord pour l'obtenir).
- `target` (label) résolu seulement si le Domain:Subject ou sujet court matche UNE seule mémoire ; sinon conservé sans résolution (pas de guess).
- Update avec `relations` = remplacement complet des edges sortants (pas d'empilement).
- Delete nettoie automatiquement les edges entrants et sortants.
- Types valides : part_of, depends, requires, runs_on, connects_to, uses, extends, supersedes, invalidates, contradicts.
- Legacy `::relations:: type=cible` après [Tier=N] reste parsé au add.

## Conflits (détection locale, 09/2026)

Au add/update de content, détection DÉTERMINISTE sans LLM : mêmes Domain:Subject + même clé key=value avec valeurs différentes (catégories decision/correction/project/user_pref/tech/fact) → enregistrement dans `memory_conflicts`. Les deux mémoires restent intactes. `lancedb_add` retourne `potential_conflicts` ; tool `lancedb_conflicts` liste le registre (status/memory_id/limit) ; page Conflicts du viz. Corriger un contenu ferme les conflits ouverts et recheck. Un conflit réintroduit se rouvre automatiquement.

## Mettre a jour une memoire (lancedb_update)

Utiliser `lancedb_update` pour editer une memoire **in-place** quand les details changent mais que l'identite reste la meme. Preferer ça a delete+recreate — ça preserve l'UUID, les links, et l'historique d'acces.

```
lancedb_update(memory_id, content="Hermes:Kanban new details [Tier=1]")
lancedb_update(memory_id, category="correction")
lancedb_update(memory_id, tags=["python", "docker"])
```

Updater `content` declenche un re-embed automatique. Les autres champs (category, tags, quality, type) ne re-embed pas.

**Quand update vs create new :**
- **Update** : meme fait, details changes (URL deplacee, version bump, correction appliquee)
- **Create new** : fait different, nouveau sujet, nouveau domaine

## Interdits dans le contenu

Task progress (PR #42, "Phase 3 faite"), commit SHAs, données éphémères < 7 jours, paragraphes ou blocs de texte.

## Pitfall: Doublon de préfixe Domaine: + double [Tier=] — OBSOLÈTE depuis le strict contract

Le fallback cron avec template `store.add(f"{domain}:{subject} ... [Tier={tier}]")` est FERMÉ depuis le strict contract (raw add fenced). La normalisation (NFKC, trim, casing, détection de wrappers dupliqués) est faite par le contract module — ne pas dupliquer de regex de strip dans les scripts callers. Toute écriture hors tool = `store.add_memory(MemoryWrite)` / `store.update_memory(MemoryPatch)`.

## Pitfalls — ce qui génère des tags pourris

Les tags sont auto-extraits du contenu. Ces patterns créent des tags garbage (même si _is_valid_tag() en bloque la plupart, mieux vaut ne pas les générer) :

- **Chemins** : `~/github/`, `/home/`, `%h/`
- **Key=value avec path** : `Path=~/project/`, `Config=~/.config/x.yaml`
- **File extensions** : `setup.py`, `config.yaml`
- **Special chars** : `(speaker_id)`, `[config]`, `foo/bar`
- **Pure numbers** : seuls dans un token

**Solution** : paraphraser. `Path=~/github/monprojet/` → `Dossier=monprojet`. `config.yaml` → `config yaml`. `get_enabled()` → `get_enabled`.