#!/usr/bin/env python3
"""Auto-merge duplicate memories by vector similarity.

Detects entries with cosine similarity >= threshold (default 0.92),
merges them into one (keeping the best entry, deleting the rest).
Runs as a dry-run by default — pass --apply to actually merge.

Usage:
    python3 scripts/auto-merge-duplicates.py [--threshold 0.92] [--apply] [--db-path ~/.hermes/lancedb]
"""
import sys
import os
import json
import time
import argparse
import numpy as np

# Path setup — works from repo root or scripts/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
HERMES_AGENT = os.path.expanduser("~/.hermes/hermes-agent")

sys.path.insert(0, REPO_ROOT)
try:
    from plugin.store import LanceDBStore, MemoryPatch
except ImportError:  # Installed script fallback.
    sys.path.insert(0, HERMES_AGENT)
    from plugins.memory.lancedb.store import LanceDBStore, MemoryPatch


def find_duplicate_groups(store: LanceDBStore, threshold: float = 0.92) -> list[list[dict]]:
    """Find groups of duplicate memories by vector similarity.

    Returns list of groups, each group is a list of raw memory dicts.
    Groups are transitive connected components: if A~B and B~C, they belong to
    the same group even when A!~C. Walking only the neighbours of the first
    element split such chains into separate groups and hid duplicates.
    """
    raw = store._get_all_raw()
    if len(raw) < 2:
        return []

    vectors = []
    mems = []
    for r in raw:
        vec = r.get("vector")
        if vec is None or not isinstance(vec, (list, np.ndarray)):
            continue
        v = np.array(vec, dtype=np.float32)
        norm = np.linalg.norm(v)
        if norm < 0.001:
            continue
        vectors.append(v / norm)
        mems.append(r)

    if len(vectors) < 2:
        return []

    vec_matrix = np.array(vectors, dtype=np.float32)
    sim_matrix = np.dot(vec_matrix, vec_matrix.T)
    n = len(mems)

    # Transitive connected components (breadth-first over the full adjacency).
    visited = set()
    groups = []
    for start in range(n):
        if start in visited:
            continue
        visited.add(start)
        component = [start]
        frontier = [start]
        while frontier:
            node = frontier.pop()
            neighbours = np.flatnonzero(sim_matrix[node] >= threshold)
            for candidate in neighbours:
                index = int(candidate)
                if index in visited:
                    continue
                visited.add(index)
                component.append(index)
                frontier.append(index)
        if len(component) >= 2:
            groups.append([mems[idx] for idx in component])

    return groups


def pick_best_entry(group: list[dict]) -> dict:
    """Pick the best entry from a duplicate group.
    Criteria: highest quality, then highest access_count, then longest content, then oldest (keep original).
    """
    def score_key(m):
        quality = m.get("quality", 0.5) or 0.5
        access = m.get("access_count", 0) or 0
        content_len = len(m.get("content", ""))
        created = m.get("created_at", time.time())
        # Score: quality * 100 + access * 10 + content_len * 0.1 - created * 0.001
        # Higher quality + access + content wins; older wins ties (keep original)
        return (quality * 100 + access * 10 + content_len * 0.1, -created)

    return max(group, key=score_key)


def merge_content(keeper: dict, duplicates: list[dict]) -> str:
    """Merge content from duplicates into the keeper.
    If the keeper's content is a superset of duplicate content, keep keeper.
    Otherwise, append unique info from duplicates.
    """
    keeper_content = keeper.get("content", "").strip()
    # Extract the [Tier=N] marker from keeper
    tier_marker = ""
    for t in ["1", "2", "3"]:
        if f"[Tier={t}]" in keeper_content:
            tier_marker = f" [Tier={t}]"
            break

    # Check if any duplicate has unique content not in keeper
    unique_parts = []
    for dup in duplicates:
        dup_content = dup.get("content", "").strip()
        # Remove tier marker from dup for comparison
        for t in ["1", "2", "3"]:
            dup_content = dup_content.replace(f" [Tier={t}]", "").replace(f"[Tier={t}]", "")
        keeper_no_tier = keeper_content
        for t in ["1", "2", "3"]:
            keeper_no_tier = keeper_no_tier.replace(f" [Tier={t}]", "").replace(f"[Tier={t}]", "")

        # If dup content is not a substring of keeper, there's unique info
        if dup_content and dup_content not in keeper_no_tier:
            # Check if keeper is a substring of dup (dup is more complete)
            if keeper_no_tier in dup_content:
                # Dup has more info — use dup content instead
                keeper_content = dup_content + tier_marker
            else:
                # Both have unique parts — note the difference but don't auto-merge text
                # (manual review needed for content merge)
                unique_parts.append(dup_content)

    if unique_parts:
        # Don't auto-merge different content — just flag it
        # The keeper stays, duplicates get deleted, but we note the unique parts
        pass

    return keeper_content


def merge_tags(keeper: dict, duplicates: list[dict]) -> list[str]:
    """Merge tags from all duplicates into keeper."""
    all_tags = set()
    for m in [keeper] + duplicates:
        tags = m.get("tags", [])
        if tags is None:
            continue
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except:
                continue
        if isinstance(tags, list):
            all_tags.update(tags)
    return sorted(all_tags)


def resolve_memory_patch(store: LanceDBStore):
    """Return the MemoryPatch class belonging to THIS store's module.

    `update_memory` checks `isinstance(patch, MemoryPatch)` against the class
    bound inside the store's own module. The plugin ships twice
    (plugin/store.py and plugins/memory/lancedb/store.py), each with its own
    memory_contract copy, so importing MemoryPatch from one side while the store
    comes from the other fails with "update_memory requires MemoryPatch".

    Resolve through the method that will actually run, so proxies, subclasses
    and delegating wrappers are followed instead of guessed from their type.
    """
    import importlib

    def _from_module(module_name):
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except Exception:
                return None
        candidate = getattr(module, "MemoryPatch", None)
        if candidate is not None:
            return candidate
        # flat module that only imported the symbol
        for attribute in vars(module).values():
            if getattr(attribute, "__name__", "") == "MemoryPatch":
                return attribute
        parent = module_name.rsplit(".", 1)[0]
        if parent != module_name:
            return _from_module(parent + ".memory_contract")
        return None

    method = getattr(store, "update_memory", None)
    function = getattr(method, "__func__", method)
    module_name = getattr(function, "__module__", None) or type(store).__module__
    resolved = _from_module(module_name)
    return resolved if resolved is not None else MemoryPatch


def incoming_relation_sources(store: LanceDBStore, target_ids: list[str]) -> dict[str, list[str]]:
    """Map each target id to the ids of memories whose relations point at it.

    `store.delete` only clears edges whose source_id or target_id IS the deleted
    memory, so a relation held in ANOTHER memory's `relations` list dangles after
    the target disappears. Read the live relations before deleting anything.
    """
    wanted = {str(item) for item in target_ids if item}
    if not wanted:
        return {}
    sources: dict[str, list[str]] = {}
    try:
        memories = store.get_all()
    except Exception:
        # If we cannot read the relations, refuse to guess: report every target
        # as referenced so the caller refuses the delete.
        return {target: ["<unreadable>"] for target in wanted}
    for memory in memories:
        source_id = str(memory.get("id") or "")
        relations = memory.get("relations") or []
        if isinstance(relations, str):
            try:
                relations = json.loads(relations)
            except Exception:
                relations = []
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            target_id = str(relation.get("target_id") or "").strip()
            if target_id and target_id in wanted:
                sources.setdefault(target_id, []).append(source_id)
    return sources


def remap_incoming_relations(
    store: LanceDBStore,
    source_ids: list[str],
    old_target_id: str,
    new_target_id: str,
) -> tuple[list[str], list[str]]:
    """Point every relation at the keeper instead of the duplicate.

    Returns (remapped_source_ids, refused_source_ids). A source is refused when
    its relations cannot be read or rewritten, so the caller can skip the delete
    instead of leaving a dangling target behind.
    """
    remapped: list[str] = []
    refused: list[str] = []
    for source_id in source_ids:
        try:
            raw = store._get_by_id_raw(source_id)
        except Exception as error:
            print(f"    ! cannot read {source_id[:12]}...: {type(error).__name__}: {error}")
            raw = None
        if not raw:
            refused.append(source_id)
            continue
        relations = raw.get("relations") or []
        if isinstance(relations, str):
            try:
                relations = json.loads(relations)
            except Exception as error:
                print(f"    ! cannot parse relations of {source_id[:12]}...: {error}")
                refused.append(source_id)
                continue
        rewritten = []
        touched = False
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            relation_type = relation.get("type")
            target_id = str(relation.get("target_id") or "").strip()
            target_label = relation.get("target")
            if target_id == old_target_id:
                # The contract accepts exactly one of target_id / target. Keep the
                # authoritative pointer and drop the label: the store re-derives
                # target_label from the new target's content.
                rewritten.append({"type": relation_type, "target_id": new_target_id})
                touched = True
                continue
            # Untouched relations must be normalised too: the stored rows carry
            # BOTH target_id and target, and the contract rejects that shape.
            if target_id:
                rewritten.append({"type": relation_type, "target_id": target_id})
            elif target_label:
                rewritten.append({"type": relation_type, "target": target_label})
        if not touched:
            # The relation vanished between the scan and now; treat as refused so
            # the caller re-evaluates rather than assuming a clean remap.
            refused.append(source_id)
            continue
        try:
            patch_class = resolve_memory_patch(store)
            store.update_memory(patch_class.from_mapping({
                "memory_id": source_id,
                "relations": rewritten,
            }))
        except Exception as error:
            print(f"    ! cannot rewrite {source_id[:12]}...: {type(error).__name__}: {error}")
            refused.append(source_id)
            continue
        remapped.append(source_id)
    return remapped, refused


def run_merge(store: LanceDBStore, threshold: float, apply: bool = False) -> dict:
    """Run the auto-merge. Returns summary stats."""
    groups = find_duplicate_groups(store, threshold)

    stats = {
        "threshold": threshold,
        "duplicate_groups": len(groups),
        "total_duplicates": sum(len(g) - 1 for g in groups),
        "merged": 0,
        "deleted": 0,
        "skipped": 0,
        "relations_blocked": 0,
        "details": [],
    }

    if not groups:
        print(f"No duplicates found at threshold {threshold}")
        return stats

    print(f"Found {len(groups)} duplicate group(s) at threshold {threshold}")
    print()

    for i, group in enumerate(groups):
        keeper = pick_best_entry(group)
        duplicates = [m for m in group if m["id"] != keeper["id"]]

        print(f"Group {i+1}/{len(groups)} ({len(group)} entries):")
        print(f"  KEEPER: {keeper['id'][:12]}... q={keeper.get('quality',0.5)} "
              f"access={keeper.get('access_count',0)} "
              f"content={keeper.get('content','')[:80]}...")

        for dup in duplicates:
            sim = None
            # Compute similarity to keeper
            keeper_vec = keeper.get("vector")
            dup_vec = dup.get("vector")
            if keeper_vec is not None and dup_vec is not None:
                kv = np.array(keeper_vec, dtype=np.float32)
                dv = np.array(dup_vec, dtype=np.float32)
                kn = np.linalg.norm(kv)
                dn = np.linalg.norm(dv)
                if kn > 0 and dn > 0:
                    sim = float(np.dot(kv / kn, dv / dn))

            print(f"  DUP:    {dup['id'][:12]}... q={dup.get('quality',0.5)} "
                  f"access={dup.get('access_count',0)} "
                  f"sim={sim:.4f} "
                  f"content={dup.get('content','')[:80]}...")

        if apply:
            # Merge tags into keeper
            merged_tags = merge_tags(keeper, duplicates)
            if merged_tags != (keeper.get("tags") or []):
                store.update_memory(MemoryPatch.from_mapping({
                    "memory_id": keeper["id"],
                    "tags": merged_tags,
                }))
                print(f"  -> Merged tags: {merged_tags}")

            # Delete duplicates, but never silently drop a relation.
            incoming = incoming_relation_sources(store, [d["id"] for d in duplicates])
            for dup in duplicates:
                dup_content = dup.get("content", "")
                keeper_content = keeper.get("content", "")

                # Anyone pointing at this duplicate must be remapped or the delete
                # must be refused; deleting first would leave a dangling target.
                referencing = incoming.get(dup["id"], [])
                if referencing:
                    remapped, refused = remap_incoming_relations(
                        store, referencing, dup["id"], keeper["id"]
                    )
                    if refused:
                        stats["skipped"] += 1
                        stats["relations_blocked"] += 1
                        print(
                            f"  -> SKIPPED {dup['id'][:12]}... "
                            f"({len(refused)} relation(s) could not be remapped: "
                            f"{', '.join(sorted(refused)[:3])})"
                        )
                        continue
                    print(
                        f"  -> REMAPPED {len(remapped)} relation(s) "
                        f"-> keeper {keeper['id'][:12]}..."
                    )

                # Check if content is identical (modulo tier marker)
                dup_clean = dup_content
                keeper_clean = keeper_content
                for t in ["1", "2", "3"]:
                    dup_clean = dup_clean.replace(f" [Tier={t}]", "").replace(f"[Tier={t}]", "")
                    keeper_clean = keeper_clean.replace(f" [Tier={t}]", "").replace(f"[Tier={t}]", "")

                if dup_clean.strip() == keeper_clean.strip():
                    # Exact content match — safe to delete
                    store.delete(dup["id"])
                    stats["deleted"] += 1
                    print(f"  -> DELETED {dup['id'][:12]}... (exact content match)")
                else:
                    # Different content — skip (needs manual review)
                    stats["skipped"] += 1
                    print(f"  -> SKIPPED {dup['id'][:12]}... (different content, needs manual review)")
            stats["merged"] += 1
        else:
            print(f"  -> DRY RUN (use --apply to merge)")

        stats["details"].append({
            "keeper_id": keeper["id"],
            "keeper_content": keeper.get("content", "")[:200],
            "duplicates": [{"id": d["id"], "content": d.get("content", "")[:200]} for d in duplicates],
        })
        print()

    return stats


def main():
    parser = argparse.ArgumentParser(description="Auto-merge duplicate LanceDB memories")
    parser.add_argument("--threshold", type=float, default=0.92,
                        help="Cosine similarity threshold for duplicates (default: 0.92)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually merge and delete (default: dry-run)")
    parser.add_argument("--db-path", type=str, default="~/.hermes/lancedb",
                        help="Path to LanceDB database")
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db_path)
    print(f"LanceDB Auto-Merge Duplicates")
    print(f"DB: {db_path}")
    print(f"Threshold: {args.threshold}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print()

    store = LanceDBStore(db_path)
    print(f"Total memories: {store.count()}")
    print()

    if args.apply:
        with store.write_batch():
            stats = run_merge(store, args.threshold, apply=True)
    else:
        stats = run_merge(store, args.threshold, apply=False)

    print("=" * 60)
    print(f"Summary:")
    print(f"  Duplicate groups:  {stats['duplicate_groups']}")
    print(f"  Total duplicates:   {stats['total_duplicates']}")
    if args.apply:
        print(f"  Merged (keepers):   {stats['merged']}")
        print(f"  Deleted:            {stats['deleted']}")
        print(f"  Skipped (review):   {stats['skipped']}")
    else:
        print(f"  (dry run — no changes made)")
    print("=" * 60)

    if args.apply and stats["deleted"] > 0:
        print()
        print("IMPORTANT: Run docker restart lancedb-viz to refresh the viz")
        print("IMPORTANT: Re-embed if content changed (scripts/reembed-entries.py)")


if __name__ == "__main__":
    main()
