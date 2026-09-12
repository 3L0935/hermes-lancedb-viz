# LanceDB Write Amplification Design

## Scope

Fix the production defect where constructing a `LanceDBStore` refreshes a stale
FTS index and therefore turns a read into a database write. Keep BM25 enabled,
keep the calibrated thresholds and LanceDB pin unchanged, and bound Lance
versions, fragments, and replaced FTS directories without touching production
data during development.

## Reader/writer boundary

`LanceDBStore._init_table()` only opens or creates the table. It never creates
or refreshes an FTS index for an existing table. Searches, list operations,
graph reads, health diagnostics, and store reopenings therefore do not create a
new Lance version, fragment, or index directory.

The existing mutation decorator becomes a batch boundary. It records the
`memories` table version at the outermost mutation, permits nested mutation
methods to complete, and refreshes the content FTS index once only when that
table version changed. A public batch context gives raw batch scripts the same
boundary. A failed refresh is reported as a post-commit maintenance failure; it
must not be silently presented as a fully successful write.

LanceDB 0.34.0 searches unindexed fragments as part of a normal FTS query. A
synthetic test must nevertheless prove that a newly added row is returned both
before and after the writer refresh, because the BM25 threshold is calibrated
and this behavior cannot be assumed from API shape alone.

## Bounded maintenance

Read-only diagnostics expose exact counts for table versions, fragments, FTS
directories, and active FTS UUIDs. Unknown values remain `None`. Maintenance is
recommended when a fixed count threshold is crossed; an hourly systemd user
timer calls a small local helper which reads the plan and invokes the existing
confirmed compaction route only when recommended. This bounds growth by both a
resource threshold and the timer interval instead of relying on an operator to
notice it.

All repository writers and compaction use the same advisory file lock inside
the mounted database root, so the host gateway and container see the same
inode. The existing in-server lock remains, while the shared lock prevents a
cooperating gateway, viz request, CLI batch, or maintenance run from mutating
during compaction.

The compaction contract remains backup-first. After the atomic backup is
published and all tables are optimized with explicit version cleanup, the
maintenance code obtains the UUIDs referenced by the current Lance metadata.
Only `_indices/<uuid>` directories absent from that active set are removed.
If active UUID discovery is unavailable on a supported engine, orphan cleanup
is skipped and reported with `None`; no directory is guessed or deleted.
Post-maintenance verification still checks row counts, readable versions, and
FTS freshness, and now also verifies that every active UUID directory exists.
Any failure returns `failed_step` and the backup path when already available;
there is no automatic restore.

## Operational evidence

Tests and a synthetic `/tmp` measurement record size, versions, fragments, and
FTS directory counts before and after reads and compaction. The real retention
harness and benchmark run unchanged and retain engine, table-version, and row
provenance. The production database and its backups remain read-only.

The observed 15,311-version producer is not attributed without process-level
evidence. Backup/live row differences are compared read-only and classified as
a separate unresolved integrity question unless repository or process evidence
proves the removals were intentional.
