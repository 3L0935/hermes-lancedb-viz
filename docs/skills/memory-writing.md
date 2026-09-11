---
name: memory-writing
description: Write durable structured facts through the LanceDB contract.
version: 8.0.0
triggers:
  - lancedb_add
  - lancedb_update
  - write memory
---

# Memory writing

Use LanceDB for durable project facts, technical configuration, corrections,
decisions, patterns, references, and project-specific preferences. Do not store
temporary task progress, secrets, credentials, or unreviewed speculation.

The write contract is enforced by the plugin. Callers provide structured data;
they never construct `Domain:Subject ... [Tier=N]` themselves. The authoritative
field limits and error codes are in `docs/memory-contract-v2.md`.

## Choose the tool

1. Search first with `lancedb_search` when the subject may already exist.
2. Use `lancedb_add` for a new subject or a distinct durable fact.
3. Use `lancedb_update` with the existing ID when replacing or correcting a
   known memory. It preserves the ID and echoes the old content.
4. Use `write_mode="upsert_subject"` only when exactly one subject match is the
   intended replacement. The default is non-destructive `create`.
5. Read the returned `status`, `canonical_content`, `normalized_fields`,
   `warnings`, and `conflicts` before reporting success.

Exact retries return `status=idempotent` and the existing ID without another
embedding. A same-subject create can return `update_suggested`. Conflicting
`key=value` claims are blocked until the caller chooses an authoritative row.

## Create example

```python
lancedb_add(
    domain="Project",
    subject="Search_Config",
    facts=[
        "port=7777",
        "Use the local database as the source of truth",
    ],
    tier=2,
    category="project",
    relations=[
        {"type": "depends", "target_id": "12345678-abcd"},
    ],
)
```

Both dense prose and `key=value` facts are valid. Keep paths, identifiers, and
technical names exact; casing in values is significant. Put the wrapper fields
only in `domain`, `subject`, and `tier`.

## Update example

```python
lancedb_update(
    memory_id="12345678-abcd",
    domain="Project",
    subject="Search_Config",
    facts=[
        "port=7778",
        "Use the local database as the source of truth",
    ],
    tier=2,
    category="project",
)
```

The update tool requires the complete structured form so the preview and
result are unambiguous. Omitting `relations` preserves existing relations;
passing a relation list replaces the outgoing set.

## Tiers and categories

- Tier 1: critical correction or rule whose omission causes immediate failure.
- Tier 2: recurring project/configuration knowledge.
- Tier 3: useful background context.

Categories are `project`, `tech`, `fact`, `correction`, `user_pref`, `decision`,
`insight`, `reference`, `pattern`, and `question`. Unknown values are rejected,
not coerced.

Subjects should identify one component, failure, or decision. Broad subjects
are accepted with an `overly_broad_subject` warning and a more specific
suggestion; they are never rejected solely for breadth.

## Relations and conflicts

Relations require a known type and exactly one target form. Prefer an exact
`target_id` obtained from search. A text `target` is resolved only when it
matches one memory unambiguously. Traversal is bounded to one hop.

Valid types are `part_of`, `depends`, `requires`, `runs_on`, `connects_to`,
`uses`, `extends`, `supersedes`, `invalidates`, and `contradicts`.

Conflict detection is deterministic: same canonical subject, same explicit
claim key, different case-sensitive value. Similarity alone is only a review
warning and never deletes or merges a memory.

## Cron and scripts

Repository callers use `store.add_memory(MemoryWrite.from_mapping(...))` or
`store.update_memory(MemoryPatch.from_mapping(...))`. Raw `store.add(str)` and
`store.update(..., content=...)` are fenced legacy adapters and are unavailable
to ordinary writes. Cron jobs may audit and report drift but own no formatting
or repair regex.

`scripts/migrate-memory-format.py` is dry-run-only. `auto_fix` means a proposed
change is mechanically unambiguous; it does not apply the change. `quarantine`
always requires human review. No script in this workflow writes the live
database without a separate explicit operation and approval.

## Rejections

Do not put subject wrappers, tier markers, relation blocks, newlines, or empty
facts inside `facts`. Never paraphrase an exact path or identifier to work
around tagging or retrieval. Rejections use the stable shape:

```json
{"code":"invalid_tier","field":"tier","message":"...","received":9,"expected":[1,2,3]}
```
