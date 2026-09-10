---
name: memory-writing
description: "Use when creating, updating, auditing, or migrating durable LanceDB memories through the strict structured write contract."
version: 9.0.0
triggers:
  - lancedb_add
  - lancedb_update
  - write memory
  - memory audit
---

# Memory writing

Use structured fields and let the plugin contract normalize, validate, and
render the persisted content. Do not reconstruct the canonical string, copy
contract regexes, or repair rejected payloads in background automation.

## Choose the destination

- Use the host application's always-loaded memory for identity and global
  interaction conventions, following that application's permission policy.
- Use LanceDB for durable facts, configuration, corrections, decisions,
  patterns, and references that should be retrieved on demand.
- Do not store secrets, transient task status, build identifiers, or facts with
  a short expected lifetime.

## Choose the operation

Search first with `lancedb_search` when the subject may already exist.

- Use `lancedb_add` with `write_mode="create"` (the default) for a new subject.
  An existing subject returns `update_suggested`; it is never overwritten.
- Use `lancedb_update` with the existing memory ID and its complete structured
  form when intentionally correcting or extending that memory.
- Use `write_mode="upsert_subject"` only when the subject is known to identify
  exactly one memory, the new claims do not conflict, and replacement is
  explicitly intended. The response includes `replaced_content` for review.
- Treat `conflicting_claims` as a blocked write requiring resolution, not as a
  reason to merge automatically.

Both write tools require `domain`, `subject`, `facts`, `tier`, and `category`.
`facts` accepts dense `key=value` strings and concise single-sentence facts,
with up to 12 facts, 1,000 characters per fact, and 2,000 characters in total.
Pass relations in the structured `relations` field. Never pass a preformatted
content wrapper or `::relations::` block to an agent tool.

Example:

```python
lancedb_add(
    domain="Project",
    subject="ApiGateway",
    facts=["transport=SSE", "port=7777", "reconnect uses exponential backoff"],
    tier=2,
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

## Interpret write results

- `created`: a new row was embedded and stored.
- `idempotent`: the exact canonical memory already exists; reuse its ID.
- `update_suggested`: `create` found the same subject; inspect the returned
  existing ID and content, then update by ID if replacement is appropriate.
- `conflicting_claims`: an explicit same-subject `key=value` claim differs;
  resolve the conflict before writing.
- `ambiguous_subject`: subject-level upsert matched multiple rows; search and
  choose an exact memory ID.

Successful and preflight responses echo `canonical_content`. Updates and
successful subject upserts also echo `replaced_content`. Errors are structured
as `code`, `field`, `message`, `received`, and `expected`; retry only when the
top-level `retryable` value is true.

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
