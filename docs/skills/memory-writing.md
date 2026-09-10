---
name: memory-writing
description: "Policy and tool choice for durable LanceDB memories."
version: 8.0.0
triggers:
  - lancedb_add
  - lancedb_update
  - write memory
  - memory audit
---

# Memory writing policy

The plugin contract is the source of truth for formatting and validation. Do
not reconstruct its canonical string, copy its regexes, or repair rejected
payloads in a cron job.

## Choose the destination

- Use built-in `MEMORY.md` / `USER.md` only for durable identity, interaction
  preferences, and session-wide working conventions.
- Use LanceDB for project facts, technical configuration, corrections,
  decisions, patterns, and references that should be retrieved on demand.
- Do not store task progress, transient status, commit SHAs, secrets, or facts
  expected to expire within a week.
- Removing built-in memory still requires explicit user confirmation.

## Choose the operation

Search first with `lancedb_search` when the subject may already exist.

- Use `lancedb_add` with `write_mode="create"` (the default) for a new subject.
  An existing subject returns `update_suggested`; it is never overwritten.
- Use `lancedb_update` with the existing memory ID and its complete structured
  form when intentionally correcting or extending that memory.
- Use `write_mode="upsert_subject"` only when the subject is known to identify
  exactly one memory and replacement is explicitly intended. The response
  includes `replaced_content` for review.
- Treat `conflicting_claims` as a blocked write requiring resolution, not as a
  reason to merge automatically.

Both write tools require `domain`, `subject`, `facts`, `tier`, and `category`.
`facts` accepts dense `key=value` strings and concise single-sentence facts.
Pass relations in the structured `relations` field. Never pass a preformatted
content wrapper to an agent tool.

Example:

```python
lancedb_add(
    domain="Hermes",
    subject="KanbanEventStream",
    facts=["transport=SSE", "port=7777", "reconnect uses exponential backoff"],
    tier=1,
    category="tech",
    write_mode="create",
)
```

## Classification

Category priority when several apply:

`correction > pattern > decision > user_pref > reference > insight > project > tech > fact > question`

Tier policy:

- Tier 1: a correction, safety rule, critical command, or recurring failure.
- Tier 2: reusable configuration, architecture, endpoint, or workflow fact.
- Tier 3: durable background context.

Use a component-, bug-, or decision-specific subject. A generic subject or a
subject equal to its domain is accepted with `overly_broad_subject`; refine it
to avoid unrelated conflict matches.

## Relations

Prefer `target_id` obtained from search. A `target` label resolves only when it
matches one memory unambiguously. Supported relation types are `part_of`,
`depends`, `requires`, `runs_on`, `connects_to`, `uses`, `extends`,
`supersedes`, `invalidates`, and `contradicts`.

## Automation and audits

Terminal-only automation must construct `MemoryWrite` / `MemoryPatch` and call
`add_memory` / `update_memory`. Raw `store.add` and `store.update` are fenced
legacy adapters reserved for controlled import and migration code.

Run `scripts/audit-memory-format.py` for cron monitoring. It is read-only and
emits JSON. Use `--fail-on-drift` in CI. Automation reports drift; it does not
rewrite live rows. Remediation belongs in the reviewed migration workflow.
