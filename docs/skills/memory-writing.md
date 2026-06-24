---
name: memory-writing
description: "How to write memories: format, tiers, categories, relations."
version: 3.0.0
triggers:
  - "memory add"
  - "lancedb_add"
  - "memory format"
  - "write memory"
---

# Memory Writing

## Before writing

1. `lancedb_search()` — check for duplicates and entries to link
2. Format with `[Tier=N]` + optional `::relations::`
3. `lancedb_add()`

## Format

```
Domain:Subject Self-contained context. key=value. [Tier=N]
::relations:: type=target | type=target2
```

| Rule | Why |
|------|-----|
| `Domain:Subject` prefix | Clusters by domain |
| Dense enough to stand alone | No dependency on other entries |
| `[Tier=N]` always | Missing = invalid |
| Max ~200 chars | Readable in 2s |
| No file extensions (`.py`, `.yaml`) | Breaks entity extraction |

## Tiers

| Tier | When |
|------|------|
| 1 | Critical — bugs, corrections, commands |
| 2 | Useful — URLs, stack, architecture |
| 3 | Contextual — background, references |

Pick the lowest that fits.

## Relations

`::relations:: type=target | type=target` after `[Tier=N]`.

| Relation | When |
|----------|------|
| `part_of` | Belongs to existing project |
| `depends` | Uses a documented tool/tech |
| `requires` | Tech required to function |
| `runs_on` | OS/arch |
| `connects_to` | Cross-link |
| `uses` | Consumes without hard dependency |
| `extends` | Extension/specialization |

Verify the target exists in the DB before writing.

## Categories

| Category | Use for |
|----------|---------|
| `correction` | Bugs, errors, fixes (priority #1 if bug) |
| `pattern` | Recurring workflows, procedures |
| `decision` | Architecture decisions |
| `user_pref` | Style, preferences |
| `reference` | Links, external docs |
| `insight` | Discoveries |
| `project` | Active project context |
| `tech` | Pure technical (ports, commands) |
| `fact` | Stable facts, architecture |
| `question` | Open questions, todos |

Priority if unsure: `correction` > `pattern` > `decision` > `user_pref` > `reference` > `insight` > `project` > `tech` > `fact` > `question`.

## Updating memories

Use `lancedb_update` to edit an existing memory in-place when its details change but its identity hasn't. Prefer this over delete + re-create — it preserves the UUID, links, and access history.

```
lancedb_update(memory_id, content="Domain:Subject new details. [Tier=N]")
lancedb_update(memory_id, category="correction")
lancedb_update(memory_id, tags=["python", "docker"])
```

Updating `content` triggers a re-embed automatically. Other fields (category, tags, quality, type) don't.

When to update vs create new:
- **Update**: same fact, details changed (URL moved, version bumped, correction applied)
- **Create new**: different fact, new subject, new domain

## Pitfalls

- Never omit `[Tier=N]`
- Never write paragraphs — max 200 chars
- Always `lancedb_search()` before add — check for duplicates and entries to link
- Prefer `lancedb_update` over delete + re-create when the memory's identity hasn't changed
- Entities with `.`, `<`, `>`, `()`, `/` don't extract. Rephrase: `config.yaml` → `config yaml`
- After batch > 5 edits → re-embed via `reembed-entries.py`