# Local Graph Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make typed relations usable by recall, add conservative local contradiction detection, and expose both through Hermes and the visualizer without new runtime services or mandatory LLM calls.

**Architecture:** Extend the existing LanceDB store with referential edge management and a small conflict table. Keep hybrid retrieval as the default, add deterministic routing and bounded one-hop expansion, then deploy the same plugin source to the canonical user plugin and runtime copy.

**Tech Stack:** Python 3.11+, LanceDB, PyArrow, NumPy, unittest, vanilla JavaScript, stdlib HTTP server.

---

### Task 1: Test harness and edge integrity

**Files:**
- Create: `tests/test_store_retention.py`
- Modify: `plugin/store.py`

- [ ] Write real LanceDB tests proving target IDs resolve uniquely, updates replace edges, deletes clean both directions, and legacy schemas migrate idempotently.
- [ ] Run the focused tests and verify they fail for the missing behavior.
- [ ] Add target_id storage, deterministic canonical-key resolution, edge replacement, and edge cleanup.
- [ ] Run the focused tests and verify they pass.

### Task 2: Routed recall and graph expansion

**Files:**
- Modify: `tests/test_store_retention.py`
- Modify: `plugin/store.py`
- Modify: `plugin/__init__.py`

- [ ] Add failing tests for auto routing, lexical routing, graph routing, one-hop expansion, deduplication, and result limits.
- [ ] Implement route_query(), lexical search fallback, and bounded one-hop expansion.
- [ ] Extend the lancedb_search schema and handler with mode and relation_depth.
- [ ] Run focused tests until green.

### Task 3: Conservative contradiction ledger

**Files:**
- Modify: `tests/test_store_retention.py`
- Modify: `plugin/store.py`
- Modify: `plugin/__init__.py`

- [ ] Add failing tests showing same-subject conflicting key=value claims create one conflict, repeated checks deduplicate, and unrelated subjects do not conflict.
- [ ] Implement the memory_conflicts table and non-destructive detector.
- [ ] Add lancedb_conflicts with status filtering.
- [ ] Run focused tests until green.

### Task 4: Visualizer integration

**Files:**
- Modify: `server/server.py`
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `static/graph.js`
- Modify: `tests/test_server_retention.py`

- [ ] Add failing API tests for resolved typed edges and conflicts.
- [ ] Return resolved to IDs, expose `/api/conflicts`, and add a compact Conflicts page.
- [ ] Verify browser JavaScript syntax and HTTP behavior.

### Task 5: Migration, docs, and deployment

**Files:**
- Create: `scripts/migrate-graph-retention.py`
- Modify: `README.md`
- Modify: `docs/setup.md`
- Modify: `scripts/verify-setup.sh`
- Modify: relevant Hermes skill files after code verification

- [ ] Add dry-run/apply migration with backup guidance and integrity reporting.
- [ ] Document the local-only architecture, routes, relations, conflicts, and LLM extraction non-goal.
- [ ] Run all tests, syntax checks, smoke tests, and secret scan.
- [ ] Back up the live LanceDB directory and run migration.
- [ ] Sync repo plugin to `~/.hermes/plugins/lancedb/` and the runtime plugin copy; sync visualizer files and restart the user service.
- [ ] Exercise live search, relation traversal, conflict listing, graph API, and conflict API.
- [ ] Commit, push to main, and verify the remote commit.
