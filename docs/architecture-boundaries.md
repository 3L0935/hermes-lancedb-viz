# Architecture boundaries (F20)

Status: **documented, not refactored.** Recorded here rather than changed, because
the audit itself warns that an arbitrary split would only move the complexity.

## What was reported

`plugin/store.py` (3081 lines) carries four concerns at once:

- the memory contract (validation, fingerprints, patching),
- mutations (add, update, delete, conflicts, relations, links),
- retrieval (lexical, vector, hybrid, diagnostics, abstention),
- visualization analytics (clusters, projections, graph neighbourhoods, health).

`server/server.py` (2066 lines) calls 12 private store methods and redoes some of
the same calculations:

```
284   store._get_by_id_raw(memory_id)
326   store._fresh()
330   store._table.search().select([...])
619   store._get_by_id_raw(memory_id)
643   store._table.search(vector)
665   store._get_all_raw()
1168  store._rebuild_all_links()
1188  store._get_by_id_raw(memory_id)
1195  store._get_all_raw()
1314  store._db
1455  store._table.search()
1493  store._table.search()
```

## Why it was not refactored here

The audit is explicit: splitting `store.py` into small helpers would relocate the
complexity without lowering it, while touching 3000 lines of contract and
mutation code on a live database. That is a rewrite with a blast radius, not a
hardening task.

`server/maintenance.py` (added by D4/D5) is the first real boundary: health
diagnostics and manual compaction now live outside `server.py` and are reusable.
That is the shape any further extraction should follow.

## The boundary a future change should aim for

Extract by behaviour, never by size, and only with behaviour frozen first:

- **mutations** (add / update / delete / conflicts / relations / links),
- **retrieval** (lexical / vector / hybrid / diagnostics / abstention),
- **analytics** (clusters / projections / graph / health).

Prerequisite before moving any of it: the 182-test suite must stay green at every
step, and the 12 private accesses above must be replaced by public methods on the
extracted boundary rather than re-exposed as private ones.
