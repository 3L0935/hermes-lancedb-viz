# Local Graph Retention Design

## Goal

Improve long-term retention without adding a daemon, remote API, graph database, or mandatory LLM call. LanceDB remains the only persistent engine and Ollama remains the local embedding provider.

## Decisions

1. Typed relations become referentially sound. Every resolvable edge stores a target memory ID. Labels remain display metadata and unresolved legacy labels remain visible but are not traversed.
2. Relation writes replace the source memory's old edges. Deleting a memory removes incoming and outgoing edges.
3. Search keeps hybrid BM25/vector retrieval as its default. A local regex router may select lexical retrieval or hybrid retrieval followed by one-hop graph traversal. No routing LLM is used.
4. Relation traversal is bounded to one hop and a fixed result budget. Direct matches always rank before graph-expanded context.
5. Contradiction detection is deterministic and conservative. It compares explicit key=value claims only between memories sharing the same canonical Domain:Subject key. It records conflicts non-destructively and never auto-merges or deletes.
6. Conflicts are inspectable through a dedicated tool and visualizer API/page. Resolution is explicit and auditable.
7. LLM relation extraction is not implemented in this release. A future implementation must be opt-in and disabled by default.

## Storage

memory_edges adds target_id while retaining target_label. Existing rows are migrated in place where possible; ambiguous or missing targets retain an empty target_id.

memory_conflicts stores conflict ID, both memory IDs, subject key, claim key, both values, status, confidence, timestamps. Rows are deduplicated by the unordered memory pair plus claim key.

## Public API

lancedb_add accepts an optional relations array containing type and either target_id or target label. lancedb_search accepts mode=auto|hybrid|lexical|graph and relation_depth=0|1. lancedb_conflicts lists conflicts and can filter by status.

## Failure policy

Embedding failure keeps existing behavior for compatibility. Relation resolution never guesses between multiple matches. Conflict detection failure never blocks a memory write. Schema migration is idempotent and tested against a temporary real LanceDB database.

## Verification

Automated tests cover relation resolution, replacement, deletion cleanup, graph response shape, bounded traversal, routing, conflict creation and deduplication. Deployment is verified against a backup of the live database, then the canonical plugin, runtime copy, visualizer service, tool calls, and HTTP APIs are exercised before push.
