---
name: memory-writing
description: "Format d'écriture LanceDB — semi-structuré, tiers, built-in vs LanceDB, workflow."
version: 2.0.0
triggers:
  - "memory add"
  - "lancedb_add"
  - "memory format"
  - "write memory"
---

# Memory Writing — Format & Workflow

## RÈGLE — LOAD AVANT CHAQUE WRITE

`skill_view('memory-writing')` **avant chaque** `lancedb_add()`. Puis :

1. `lancedb_search()` — vérifier duplicats ET entrées existantes à lier
2. Formater avec `[Tier=N]` + `::relations::` si pertinent
3. `lancedb_add(category=...)`

---

## Format

```
Domaine:Sujet Contexte autosuffisant. clé=valeur. [Tier=N]
::relations:: type=cible | type=cible
```

| Règle | Pourquoi |
|-------|----------|
| `Domaine:Sujet` en tête | Clustering par domaine |
| Assez dense pour être comprise seule | Pas dépendre d'une autre entrée |
| Pas besoin d'être une phrase correcte | Fragments OK |
| `[Tier=N]` **toujours** | Oubli = invalide |
| Max ~200 chars | Lisible en 2s |
| Pas d'extension fichier (`.py`, `.yaml`) | Tue l'extraction d'entités |

**Valide :**
`Tech:Ollama Embeddings nomic-embed-text=768d keep_alive=30s. [Tier=1]`

**Invalide :**
`port=8846` (pas de Domaine:Sujet) · 3 lignes sans Tier (trop long)

## Tiers

| Tier | Quand |
|------|-------|
| **1** | Friction immédiate — bugs, corrections, commandes |
| **2** | Utile récurrent — URLs, stack, architecture |
| **3** | Contextuel — pourquoi, références |

Prendre le plus bas.

## Linking implicite

Les entrées se lient via cosine similarity. Pas besoin de `Related=ID`. Chaque entrée doit être autosuffisante + assez de shared terms pour matcher les voisines.

**Règle :** Jamais "Voir entrée X" — ça casse le graphe.

## ::relations:: block

`::relations:: type=cible | type=cible` **après** `[Tier=N]`.

| Relation | Quand |
|----------|-------|
| `part_of` | Appartient à un projet existant |
| `depends` | Utilise un outil/techno documenté |
| `requires` | Techno nécessaire |
| `runs_on` | OS/arch |
| `connects_to` | Lien transverse |
| `uses` | Consomme sans dépendance forte |
| `extends` | Extension/spécialisation |

Stocké dans `memory_edges` pour le graphe. Vérifier que la cible existe dans la DB avant d'écrire.

## Catégories

| Catégorie | Usage |
|-----------|-------|
| `correction` | Bugs, erreurs, fixes (priorité #1 si bug) |
| `pattern` | Workflows récurrents, procédures |
| `decision` | Décisions architecturales |
| `user_pref` | Style, préférences |
| `reference` | Liens, docs externes |
| `insight` | Découvertes |
| `project` | Contexte projet actif |
| `tech` | Technique pure (ports, commandes) |
| `fact` | Faits stables, architecture |
| `question` | Questions ouvertes, todo |

Priorité si hésitation : `correction` > `pattern` > `decision` > `user_pref` > `reference` > `insight` > `project` > `tech` > `fact` > `question`.

## memory() vs lancedb_add()

| Destination | Contenu |
|-------------|---------|
| **`memory()`** | Identité utilisateur, stack critique, conventions durables (~2200 chars) |
| **`lancedb_add()`** | Tout le reste : technique, bugs, fixes, chemins, commandes |

Test : "c'est qui l'utilisateur ?" → memory(). "Comment fixer X ?" → lancedb_add().

## Search Context Enrichment — quality + relations au fetch

Quand tu cherches, ne t'arrête pas aux résultats bruts du `lancedb_search()`.

### 1. Quality-aware re-rank

Le score retourné par `lancedb_search()` est cosine similarity pure. Les entrées avec qualité < 0.3 sont du bruit même avec un bon score.

Filtrage post-search :
1. Garder les résultats avec `quality >= 0.3`
2. Re-rank : `score_final = cosine_score * 0.7 + quality * 0.3`
3. Prendre le top 3-5

### 2. Follow relations

Chaque résultat peut avoir des `relations[]` et des `links[]`. Suis-les :

1. Lire `relations[]` → `lancedb_search()` sur chaque cible (label matching)
2. Lire `links[]` → si ce sont des IDs, ils pointent vers d'autres entrées directes
3. Priorité : relations typées (depends, part_of) > links cosine (vague)
4. Ajouter les contextes secondaires à ta réponse

### 3. Workflow complet

```
1. lancedb_search("sujet") → top 5
2. Quality re-rank : cos*0.7 + quality*0.3, garder top 3
3. Pour chaque résultat, suivre les relations[] 1 niveau
4. Synthétiser : résultat principal + contexte lié
```

## Pitfalls

- **Jamais de task progress** (PR #42, "Phase 3 done")
- **Jamais omettre `[Tier=N]`**
- **Jamais de paragraphes** — max 200 chars
- **Toujours** `lancedb_search()` avant add
- **Toujours** `lancedb_add()` pas `memory()` pour du technique
- **Entités vides** : tokens avec `.`, `<`, `>`, `()`, `/` ne passent pas. Reformuler : `config.yaml` → `config yaml`, `get_enabled()` → `get_enabled`
- **Batch > 5 modifs** → ré-embedding via `reembed-entries.py`