# LanceDB Write Amplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make reads physically read-only while keeping BM25 current on writer batches and automatically bounding LanceDB history and orphan FTS directories.

**Architecture:** Store construction never refreshes FTS. The outer writer batch owns one refresh and shares an advisory lock with backup-first maintenance; an hourly local timer invokes maintenance only when fixed resource thresholds are crossed.

**Tech Stack:** Python 3.11/3.12, LanceDB 0.34.0 with compatibility helpers for 0.30.2, unittest/pytest, systemd user units, Bash deployment.

---

### Task 1: Prove the read-path regression

**Files:**
- Modify: `tests/test_store_retention.py`

- [ ] Add a filesystem snapshot helper counting bytes, manifests, fragments, and FTS UUID directories under a temporary `memories.lance` table.
- [ ] Add a test which creates a current index, makes it stale with a raw write, snapshots the table, reopens `LanceDBStore`, performs `search`, and asserts the snapshot is unchanged.
- [ ] Run only that test against unmodified `plugin/store.py` and record the expected failure caused by one new version and one new FTS directory.

### Task 2: Move FTS refresh to writer batches

**Files:**
- Modify: `plugin/store.py`
- Modify: `scripts/reembed-entries.py`
- Modify: `scripts/auto-merge-duplicates.py`
- Test: `tests/test_store_retention.py`
- Test: `tests/test_reembed_entries.py`
- Test: `tests/test_auto_merge_duplicates.py`

- [ ] Remove `_ensure_fts_index()` calls from `_init_table()`.
- [ ] Add an outermost mutation/batch context which compares `memories` table versions and refreshes FTS once after a changed batch.
- [ ] Keep nested mutations inside the same batch and expose the boundary to raw batch scripts.
- [ ] Prove the read-path test is green and one batch creates no more than one replacement index.
- [ ] Prove a distinctive newly added row is returned by BM25 before and after refresh, recording both scores without changing calibrated thresholds.

### Task 3: Make compaction clean only abandoned index UUIDs

**Files:**
- Modify: `server/maintenance.py`
- Test: `tests/test_maintenance.py`

- [ ] Add dict-or-attribute helpers that discover active Lance index UUIDs and return `None` when unavailable.
- [ ] Add read-only counts for physical FTS UUID directories and orphans.
- [ ] After atomic backup and version cleanup, delete only UUID directories not referenced by current metadata.
- [ ] Verify active directories, row counts, readable versions, and current FTS; preserve `failed_step`, `backup_created`, and no automatic restore on every failure.
- [ ] Add failure tests proving no orphan deletion occurs before backup or when active UUID discovery is unavailable.

### Task 4: Add bounded automatic triggering and writer exclusion

**Files:**
- Modify: `plugin/store.py`
- Modify: `server/maintenance.py`
- Modify: `server/server.py`
- Create: `scripts/compact-if-needed.py`
- Create: `systemd/lancedb-viz-maintenance.service`
- Create: `systemd/lancedb-viz-maintenance.timer`
- Modify: `scripts/deploy-local.sh`
- Test: `tests/test_maintenance.py`
- Test: `tests/test_deploy_contract.py`

- [ ] Use the same advisory lock-file convention for every outer store mutation and compaction.
- [ ] Extend the read-only compaction plan with fixed version, fragment, and orphan-index thresholds plus exact trigger reasons.
- [ ] Implement a local helper that exits without writes when the plan is below threshold and POSTs `{"confirmed": true}` only when recommended.
- [ ] Install and enable an hourly persistent systemd user timer for the primary Docker endpoint; leave the fallback viz service disabled.
- [ ] Test no-trigger, trigger, lock-contention, and deploy-contract behavior.

### Task 5: Documentation, measurements, and provenance

**Files:**
- Modify: `README.md`
- Modify: `docs/setup.md`
- Modify: `docs/skills/lancedb-memory-system.md`
- Modify: canonical `lancedb-memory-system/SKILL.md`, then propagate it byte-identically to `docs/skills/`
- Create: `audit/repro/measure-write-amplification.py`

- [ ] Document reader/writer ownership, refresh batching, thresholds, timer behavior, shared locking, backup-first orphan cleanup, and recovery semantics.
- [ ] Run the synthetic measurement under `/tmp` and record before/after bytes, versions, fragments, and index directories.
- [ ] Compare the real database and pre-compaction backup read-only by IDs and available provenance; do not infer intent from differences alone.
- [ ] Run the unchanged retention negative-control test, real retention harness, and benchmark with engine/table-version/row provenance.

### Task 6: Final verification and commit

**Files:**
- Verify all changed files.

- [ ] Run `PYTHONPATH=$PWD:/home/elo/.hermes/hermes-agent /home/elo/.hermes/hermes-agent/venv/bin/python -m pytest -q tests` after all edits.
- [ ] Run `node --check static/app.js` and `node --check static/graph.js`.
- [ ] Verify skill/doc byte identity, inspect `git diff --check`, and inspect the staged diff for secrets or forbidden threshold/engine changes.
- [ ] Commit once with a message explaining that read-time FTS rebuilds caused unbounded Lance history; do not push.
