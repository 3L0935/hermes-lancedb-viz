# Memory write contract v2

This document is the versioned source of truth for supported memory writes.
`plugin/memory_contract.py` enforces it for tools, the visualizer, scripts, and
direct Python callers. Stored `content` is derived by the contract and is not a
supported input format except through explicitly fenced legacy import helpers.

## Required request

A create request contains `domain`, `subject`, `facts`, `tier`, and `category`.
`relations` is optional. `write_mode` defaults to `create`; the only opt-in
alternative is `upsert_subject`.

- `domain`: 1–32 characters after NFKC, trim, and whitespace-to-underscore
  normalization; no colon, brackets, controls, or newline.
- `subject`: 1–80 characters under the same label rules.
- `facts`: 1–12 single-line dense strings. Both prose and `key=value` claims are
  valid. Each fact is at most 1,000 characters and all facts total at most
  2,000 characters.
- `tier`: integer `1`, `2`, or `3`.
- `category`: one value from `VALID_CATEGORIES`; unknown values are rejected.
- `relations`: at most 20 values with a known type and exactly one of
  `target_id` or `target`.

The proposed 240-character density cap was measured rather than assumed. A
read-only projected scan on 2026-09-11 found 465 rows and 446 bodies with valid
outer wrappers. A 240-character cap retained 252/446 (56.5%), while 1,000
retained 425/446 (95.3%). The table version remained `110000` before and after
the scan. The conservative first cut therefore keeps 1,000 per fact and 2,000
combined; migration never guesses how to split an oversized legacy body.

## Casing and canonical content

NFKC, outer whitespace, repeated inline whitespace, wrapper spelling, claim-key
casing, and punctuation between facts are canonicalized. Domain, subject,
prose, claim values, paths, and code identifiers preserve their case. In
particular, `path=/tmp/Alpha` and `path=/tmp/alpha` have different fingerprints.

Canonical rendering is:

```text
Domain:Subject fact one. fact two [Tier=N]
```

Canonicalization is idempotent. Facts containing a nested subject prefix,
`[Tier=N]`, `::relations::`, or a newline are rejected rather than repaired.

## Create and update semantics

- Exact canonical fingerprint: successful `idempotent` response with the
  existing ID; no embedding and no row creation.
- Same subject without explicit claim conflict under `create`:
  `update_suggested`, existing ID/content echoed, no write.
- Same subject with conflicting claim values: `conflicting_claims`, no write.
- `upsert_subject`: allowed only with exactly one subject match; the response
  echoes `replaced_content`. Multiple matches are `ambiguous_subject`.
- Structured update: validates the complete candidate using the same rules,
  preserves the ID, and echoes old and canonical content.
- Overly broad subject: accepted with `overly_broad_subject` warning and a
  concrete more-specific subject suggestion.

Every rejection uses `{code, field, message, received, expected}`. Embedding
failure is retryable and never persists a zero vector.

## Legacy policy and frozen baseline

Raw `add`/`update` calls require an explicit internal `legacy=True` fence and
still parse and validate canonical wrappers. Ordinary tools and UI routes do
not expose this flag. Format migration is classification-only; applying changes
to any database requires a separate human-approved operation outside this
contract.

The pre-migration fixture freezes the observed 450-row audit baseline: 18 rows
without a valid leading wrapper, 5 with a missing/invalid tier, 7 with multiple
tier markers, 2 with a duplicated leading wrapper, and 88 over 500 characters.
The machine-readable fixture is `tests/fixtures/memory_contract_v2.json`.
