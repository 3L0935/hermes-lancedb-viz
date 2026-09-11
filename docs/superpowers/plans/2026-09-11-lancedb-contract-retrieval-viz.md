# LanceDB Contract, Retrieval, and Viz Implementation Plan

> **For agentic workers:** Execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce one structured memory write contract, close the remaining consistency defects, promote only measured retrieval improvements, and add bounded diagnostic visualization features.

**Architecture:** `plugin/memory_contract.py` owns pure normalization and validation; `plugin/store.py` owns persistence and mutation serialization; retrieval diagnostics remain a bounded store/server concern; the vanilla frontend consumes explicit JSON without trusting database text. The live Hermes database is read-only and all mutation/evaluation work uses temporary synthetic copies.

**Tech Stack:** Python 3.11, LanceDB 0.34, unittest/pytest, vanilla JavaScript, Node syntax checks.

---

## Definition of done

- Every supported writer reaches `MemoryWrite` or `MemoryPatch`; only the contract renders stored `content`.
- Exact retries are idempotent before embedding; values remain case-sensitive; broad subjects warn; `create` is the default and subject replacement is explicit and echoed.
- Migration is dry-run-only and classifies safe normalization separately from manual review.
- Database-originated text cannot reach an HTML parser in either frontend script.
- Process-local mutations serialize preflight and commit; cross-process atomicity is explicitly reported as unsupported.
- Retrieval returns exact IDs/trivial lexical matches first, degrades explicitly when embedding is unavailable, abstains below calibrated evidence, and explains bounded one-hop neighbors.
- A frozen FR/EN evaluation set reports Recall@5, MRR, and false-result rate for lexical, current hybrid, corrected hybrid, and one-hop variants; routing changes are promoted only when the final split supports them.
- Viz features are on-demand, bounded, and consume the contract/audit/diagnostic APIs without background cleanup.
- Every coherent unit has a red reproduction, green focused tests, the full test suite, and its own conventional commit.

## Safety and verification harness

- [x] Confirm `HEAD=4f04106332b1a8ce889c593095a3637ec893a4b1` and a clean tree.
- [x] Run baseline with `PYTHONPATH=/home/elo/github/hermes-lancedb-viz:/home/elo/.hermes/hermes-agent /home/elo/.hermes/hermes-agent/venv/bin/python -m pytest -q tests`; expect `120 passed`.
- [x] Read the required architecture, hardening plan, and reproduction artifacts in order.
- [x] Measure density by direct projected Arrow read with `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1`; verify the live table version is unchanged.
- [ ] Keep reproducible measurements and decisions under ignored `audit/repro/`; never instantiate `LanceDBStore` against the live path.

## Task A1: Freeze and test contract v2

**Files:**
- Create: `docs/memory-contract-v2.md`
- Create: `tests/fixtures/memory_contract_v2.json`
- Modify: `plugin/memory_contract.py`
- Modify: `tests/test_memory_contract.py`

- [ ] Add fixture assertions for the six baseline counts and representative valid/invalid inputs.
- [ ] Add a red test proving `/tmp/Alpha` and `/tmp/alpha` have different fingerprints while normalized claim keys remain equivalent.
- [ ] Preserve claim values in `memory_fingerprint`; normalize only wrappers, claim keys, whitespace, and relation ordering.
- [ ] Record the 2026-09-11 read-only density distribution and retain the evidence-backed 1,000-character per-fact / 2,000-character aggregate caps; accept both prose and `key=value`.
- [ ] Run `python -m pytest -q tests/test_memory_contract.py` and the full suite, then commit `fix: freeze case-safe memory contract`.

## Task A2: Enforce the single write gate

**Files:**
- Modify: `plugin/store.py`
- Modify: `plugin/__init__.py`
- Modify: `server/server.py`
- Modify: `tests/test_store_retention.py`
- Modify: `tests/test_provider_formatting.py`
- Modify: `tests/test_viz_retention.py`

- [ ] Enumerate every mutation call site and add a red bypass test for any supported raw path.
- [ ] Validate exact fingerprint, subject conflicts, relation targets, and write mode before embedding.
- [ ] Make tool schemas closed and fully structured; responses include normalized fields, canonical content, warnings, conflicts, idempotency status, and replaced content for upsert/update.
- [ ] Run focused tests and the full suite, then commit only if behavior changes are required.

## Task A3: Align audit, cron, and migration

**Files:**
- Modify: `scripts/audit-memory-format.py`
- Modify: `scripts/migrate-memory-format.py`
- Modify: `tests/test_audit_memory_format.py`
- Modify: `tests/test_migrate_memory_format.py`

- [ ] Add a red CLI test proving `--apply` is unavailable and no mutation helper is reachable.
- [ ] Remove apply/backup/write code; output only `auto_fix` versus `manual_review` classifications from a projected read.
- [ ] Verify scripts contain no formatting policy beyond calls into `memory_contract.py`.
- [ ] Run focused tests and the full suite, then commit `fix: make memory format migration dry-run only`.

## Task B1: Render database text safely

**Files:**
- Modify: `static/app.js`
- Modify: `static/graph.js`
- Modify: `tests/test_viz_retention.py`

- [ ] Add a red static regression covering every database-originated field used by `innerHTML` or `insertAdjacentHTML`.
- [ ] Replace those sinks with DOM construction and `textContent`, or centralized escaping when markup structure is static.
- [ ] Run the focused test, `node --check static/app.js`, `node --check static/graph.js`, and the full suite; commit `fix: render memory fields as untrusted text`.

## Task B2: Serialize local mutations and unify quality

**Files:**
- Modify: `plugin/store.py`
- Modify: `tests/test_store_retention.py`
- Modify: `README.md`

- [ ] Add a deterministic red interleaving test using two stores in one process.
- [ ] Hold one process-local lock keyed by resolved database path around mutation preflight and commit.
- [ ] Document that `_fresh()` prevents stale snapshots but does not provide inter-process atomicity.
- [ ] Add a red stale-quality reproduction; select one read-time computation path and ensure every recalculating view uses it without contradictory persisted values.
- [ ] Run focused and full tests; commit `fix: serialize local writes and unify quality reads`.

## Task B3: Repair maintenance docs and dry-run

**Files:**
- Modify: `docs/skills/memory-writing.md`
- Modify: `docs/skills/lancedb-memory-system.md`
- Modify: `scripts/reembed-entries.py`
- Modify: `CONTRIBUTING.md`
- Create or modify: focused script/documentation tests under `tests/`

- [ ] Replace stale raw-write/update examples with structured policy and tool-choice examples.
- [ ] Add a red subprocess test for `reembed-entries.py --dry-run` with Ollama unavailable; fix imports and order so it exits before any model connection.
- [ ] Document the exact clean-checkout `PYTHONPATH` and pytest invocation.
- [ ] Run focused and full tests; commit docs and script fixes as separate coherent units.

## Task C1: Build the frozen retrieval evaluation

**Files:**
- Create: `audit/repro/retrieval-questions-calibration.json`
- Create: `audit/repro/retrieval-questions-final.json`
- Create: `audit/repro/benchmark-retrieval.py`

- [ ] Read only projected metadata from the live table and copy selected rows into a dedicated `/tmp` LanceDB fixture; record no private row text in committed files.
- [ ] Author 30–50 FR/EN questions spanning project name, ID, exact path, config, paraphrase, dependency, old critical correction, contradiction, and no-answer.
- [ ] Freeze the final split before tuning and implement deterministic Recall@5, MRR, no-answer false-result rate, context size, embedding-call count, and timing collection.

## Task C2: Implement observable retrieval candidates

**Files:**
- Modify: `plugin/store.py`
- Modify: `plugin/__init__.py`
- Modify: `server/server.py`
- Modify: retrieval/store/provider/server tests

- [ ] Add red tests for exact generated IDs, trivial BM25 matches, invalid/zero embeddings, explicit degraded lexical fallback, calibrated abstention, typed/directed one-hop provenance, and strict neighbor budgets.
- [ ] Return route, match type, metric/distance, embedding/search timings, degraded state, and abstention reason without query text by default.
- [ ] Keep bounded local history only when diagnostics are requested.
- [ ] Run focused and full tests.

## Task C3: Benchmark and promotion gate

- [ ] Run lexical, current hybrid, corrected hybrid, and corrected one-hop under the one-thread protocol.
- [ ] Write `audit/repro/retrieval-benchmark-results.json` with per-split metrics and environment metadata.
- [ ] Promote route changes only if the frozen final metrics support them; otherwise revert routing behavior while retaining the report/harness.
- [ ] Commit promoted code and tests, then rerun the full suite.

## Task D: Add bounded vanilla-JS visualization features

**Files:**
- Modify: `server/server.py`
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `static/graph.js`
- Modify: `static/style.css`
- Modify: server/viz tests

- [ ] Add a structured editor with canonical preview, old/new diff, relation fields, and visible version conflict.
- [ ] Add an on-demand review inbox fed by projected audit output; explain format, contradiction, broken-reference, and near-duplicate reasons without treating age as falsity.
- [ ] Replace the global graph default with a selected-memory neighborhood, typed/proximity edge distinction, hidden-neighbor count, and paused off-screen physics.
- [ ] Add on-demand health/cost diagnostics with versions, fragments, FTS lag, useful/history bytes, Ollama errors, pipeline identity, and pre-maintenance estimates.
- [ ] Connect the “Why this result?” panel to C diagnostics.
- [ ] State and enforce response/node/edge/history budgets in code and tests.
- [ ] Run browser-independent tests, both Node syntax checks, and the full suite; commit each independent feature.

## Final verification

- [ ] Run the exact full pytest command and preserve complete output.
- [ ] Run both Node syntax checks.
- [ ] Inspect `git diff`/commits for secrets and forbidden runtime paths.
- [ ] Confirm `git status --short` is empty and the live table version was not changed by this work.
- [ ] Report delivered/stopped chantier, file:line evidence, test names, benchmark table/JSON, and every commit hash/message.
