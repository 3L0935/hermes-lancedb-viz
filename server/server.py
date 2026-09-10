#!/usr/bin/env python3
"""LanceDB Memory Graph Visualization Server.

Serves an interactive knowledge graph of stored memories.
Reads directly from the LanceDB store — no daemon, no API.

Usage:
    python server.py            # defaults to port 7778
    python server.py --port 8888
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATIC_DIR = Path(__file__).parent / "static"
LANCEDB_PATH = HERMES_HOME / "lancedb"
HOST = "127.0.0.1"
PORT = 7778


def _parse_entities(val):
    """Parse entities field — handles both JSON array and CSV string formats."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: comma-separated string
    if isinstance(val, str) and val.strip():
        return [e.strip() for e in val.split(",") if e.strip()]
    return []


# ---------------------------------------------------------------------------
# Store singleton
# ---------------------------------------------------------------------------

_store_instance = None

def _import_store_module():
    """Import the LanceDB store MODULE: canonical user copy first, runtime fallback.

    ~/.hermes/plugins/lancedb/store.py (canonical, survives hermes-agent updates)
    wins over the runtime copy (HERMES_HOME/hermes-agent/plugins/memory/lancedb),
    which gets wiped by updates when untracked. Module cached in sys.modules.
    """
    import importlib.util
    canonical = HERMES_HOME / "plugins" / "lancedb" / "store.py"
    if canonical.exists():
        name = "lancedb_store_canonical"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, canonical)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    sys.path.insert(0, str(HERMES_HOME / "hermes-agent"))
    try:
        import plugins.memory.lancedb.store as mod  # type: ignore
        return mod
    except ImportError:
        raise ImportError("LanceDB store module introuvable (canonical + runtime)")

def _get_store():
    """Lazy helper to get a LanceDBStore instance (cached)."""
    global _store_instance
    if _store_instance is not None:
        return _store_instance
    _store_instance = _import_store_module().LanceDBStore(LANCEDB_PATH)
    return _store_instance


def _reset_store():
    """Reset cached store instance (after mutations that change row count)."""
    global _store_instance
    _store_instance = None
    _invalidate_cache()


# ---------------------------------------------------------------------------
# Stats / dashboard cache
# ---------------------------------------------------------------------------

import threading

_cache_lock = threading.Lock()
_stats_cache = None       # (result_dict, timestamp)
_stats_cache_ttl = 30.0   # seconds — refresh at most every 30s

def _invalidate_cache():
    """Drop cached stats (after mutations)."""
    global _stats_cache
    _stats_cache = None

def _compute_stats_fast() -> dict:
    """Compute stats directly from Arrow — avoids get_all() dict overhead."""
    import json as _json
    import lancedb

    store = _get_store()
    store._fresh()
    now = time.time()

    # Read only the columns we need (no vector column)
    try:
        arrow = store._table.to_arrow().drop(["vector"])
    except Exception:
        arrow = store._table.to_arrow()

    n = arrow.num_rows
    categories = {}
    types = {}
    tiers = {"1": 0, "2": 0, "3": 0, "none": 0}
    age_buckets = {"<1h": 0, "1-24h": 0, "1-7d": 0, "7-30d": 0, ">30d": 0}
    access_buckets = {"0": 0, "1-2": 0, "3-5": 0, "6-10": 0, ">10": 0}
    total_entities = set()

    cat_col = arrow.column("category")
    type_col = arrow.column("type")
    content_col = arrow.column("content")
    created_col = arrow.column("created_at")
    access_col = arrow.column("access_count")
    entities_col = arrow.column("entities")
    tags_col = arrow.column("tags")
    quality_col = arrow.column("quality")

    tag_counts = {}
    total_quality = 0.0
    access_counts_list = []
    top_accessed_heap = []  # (access_count, id, content[:80], category)

    for i in range(n):
        cat = cat_col[i].as_py() or "unknown"
        categories[cat] = categories.get(cat, 0) + 1

        mt = type_col[i].as_py() or cat
        types[mt] = types.get(mt, 0) + 1

        content = content_col[i].as_py() or ""
        if "[Tier=1]" in content:
            tiers["1"] += 1
        elif "[Tier=2]" in content:
            tiers["2"] += 1
        elif "[Tier=3]" in content:
            tiers["3"] += 1
        else:
            tiers["none"] += 1

        created = created_col[i].as_py() or now
        age_h = (now - created) / 3600
        if age_h < 1: age_buckets["<1h"] += 1
        elif age_h < 24: age_buckets["1-24h"] += 1
        elif age_h < 168: age_buckets["1-7d"] += 1
        elif age_h < 720: age_buckets["7-30d"] += 1
        else: age_buckets[">30d"] += 1

        ac = access_col[i].as_py() or 0
        if ac == 0: access_buckets["0"] += 1
        elif ac <= 2: access_buckets["1-2"] += 1
        elif ac <= 5: access_buckets["3-5"] += 1
        elif ac <= 10: access_buckets["6-10"] += 1
        else: access_buckets[">10"] += 1
        access_counts_list.append(ac)

        # Entities
        raw_ent = entities_col[i].as_py()
        if isinstance(raw_ent, str):
            try:
                raw_ent = _json.loads(raw_ent)
            except (ValueError, TypeError):
                raw_ent = []
        for e in (raw_ent or []):
            total_entities.add(e)

        # Tags (for dashboard)
        raw_tags = tags_col[i].as_py()
        if isinstance(raw_tags, str):
            try:
                raw_tags = _json.loads(raw_tags)
            except (ValueError, TypeError):
                raw_tags = []
        for t in (raw_tags or []):
            tag_counts[t] = tag_counts.get(t, 0) + 1

        # Quality (for dashboard)
        q = quality_col[i].as_py() or 0.5
        total_quality += q if q else 0.5

        # Top accessed (keep top 10)
        if len(top_accessed_heap) < 10:
            import heapq
            heapq.heappush(top_accessed_heap, (ac, cat, content[:80], str(i)))
        elif ac > top_accessed_heap[0][0]:
            import heapq
            heapq.heapreplace(top_accessed_heap, (ac, cat, content[:80], str(i)))

    # Build top accessed from heap
    top_accessed_clean = []
    for ac, cat, content80, _ in sorted(top_accessed_heap, key=lambda x: -x[0]):
        top_accessed_clean.append({
            "content": content80,
            "category": cat,
            "access_count": ac,
        })

    avg_quality = total_quality / n if n else 0.0
    total_access = sum(access_counts_list)
    never_accessed = sum(1 for a in access_counts_list if a == 0)
    top_tags = dict(sorted(tag_counts.items(), key=lambda x: -x[1])[:20])

    db_size = store.db_size

    # Shared result — both get_stats and api_get_dashboard use this
    return {
        "_shared": True,
        "total_memories": n,
        "total_entities": len(total_entities),
        "categories": categories,
        "types": types,
        "tiers": tiers,
        "ages": age_buckets,
        "access": access_buckets,
        "db_size_bytes": db_size,
        "db_size_mb": round(db_size / (1024 * 1024), 2),
        # Dashboard extras
        "total": n,
        "tags": top_tags,
        "avg_quality": round(avg_quality, 3),
        "total_access_count": total_access,
        "never_accessed": never_accessed,
        "top_accessed": top_accessed_clean,
    }

def _get_cached_stats() -> dict:
    """Return stats with a short TTL cache to avoid re-scanning on every request."""
    global _stats_cache
    now = time.time()
    with _cache_lock:
        if _stats_cache is not None and (now - _stats_cache[1]) < _stats_cache_ttl:
            return _stats_cache[0]
        result = _compute_stats_fast()
        _stats_cache = (result, now)
        return result


# ---------------------------------------------------------------------------
# API handlers — legacy (kept for backward compat)
# ---------------------------------------------------------------------------

def get_graph_data(cluster: str = "raw", threshold: float = 0.65) -> dict:
    """Read memories from LanceDB and format as graph.

    cluster='domain': vector links + community hubs (✦ prefix)
    cluster='entity': vector links + community hubs (◆ prefix)  
    cluster='raw' (default): pure vector links, no hubs
    threshold: cosine similarity threshold (0.0-1.0, default 0.65)

    Resets the store singleton so every graph load reflects latest DB writes
    without requiring a container restart.
    """
    _reset_store()
    try:
        _import_store_module()
    except ImportError:
        return {"error": "LanceDB plugin not found", "nodes": [], "edges": []}

    try:
        store = _get_store()

        if cluster == "entity":
            result = _build_entity_clustered_graph(store, threshold)
        elif cluster == "raw":
            result = _build_raw_graph(store, threshold)
        else:
            result = _build_category_hub_graph(store, threshold)

        # Attach typed edges (::relations::)
        result["typed_edges"] = store.get_typed_edges()

        return result
    except Exception as e:
        return {"error": str(e), "nodes": [], "edges": []}


def _compute_vector_data(store, threshold: float = 0.65) -> tuple:
    """Compute vector similarity data shared by all graph modes.
    
    Returns (nodes, edges, nodes_by_id, sim_matrix, memories_list, threshold)
    where edges are REAL cosine similarity links from the vector store.
    All 3 graph modes use this as their base.
    """
    import numpy as np
    raw = store._get_all_raw()
    if not raw:
        return [], [], {}, None, [], threshold

    nodes, edges = [], []
    seen_edges = set()
    nodes_by_id = {}

    cat_colors = {
        "user_pref": "#ec4899", "project": "#10b981",
        "tech": "#f59e0b", "correction": "#ef4444", "fact": "#06b6d4",
    }

    memories = []
    vectors = []
    for r in raw:
        vec = r.get("vector")
        if not vec or not isinstance(vec, (list, np.ndarray)) or len(vec) < 2:
            continue
        vec_arr = np.array(vec, dtype=np.float32)
        norm = np.linalg.norm(vec_arr)
        if norm < 0.001:
            continue
        vec_arr = vec_arr / norm

        content = r["content"]
        first_part = content.split()[0] if content else "?"
        name = first_part.rstrip(":,")
        color = cat_colors.get(r.get("category", "fact"), "#6b7280")

        node_id = r["id"]
        node = {
            "id": node_id,
            "label": name[:30],
            "title": content,
            "color": color, "size": 20,
            "category": r.get("category", "fact"),
            "node_type": "leaf",
            "created_at": r.get("created_at", 0),
            "access_count": r.get("access_count", 0),
            "accessed_at": r.get("accessed_at", None),
            "entities": _parse_entities(r.get("entities", "[]")),
            "relations": _parse_entities(r.get("relations", "[]")),
            "tier": "1" if "[Tier=1]" in content else "2" if "[Tier=2]" in content else "3" if "[Tier=3]" in content else "none",
        }
        nodes.append(node)
        nodes_by_id[node_id] = node
        memories.append(node_id)
        vectors.append(vec_arr)

    threshold = threshold
    sim_matrix = None
    
    if vectors:
        vec_matrix = np.array(vectors, dtype=np.float32)
        sim_matrix = np.dot(vec_matrix, vec_matrix.T)
        n = len(memories)
        for i in range(n):
            for j in range(i + 1, n):
                score = float(sim_matrix[i][j])
                if score >= threshold:
                    ek = tuple(sorted([memories[i], memories[j]]))
                    if ek not in seen_edges:
                        seen_edges.add(ek)
                        opacity = min(0.9, max(0.3, (score - threshold) / (1.0 - threshold)))
                        edges.append({
                            "from": memories[i],
                            "to": memories[j],
                            "label": f"{score:.2f}",
                            "color": {"color": "#6366f1", "opacity": opacity},
                        })

    return nodes, edges, nodes_by_id, sim_matrix, memories, threshold


def _find_vector_communities(sim_matrix, memories, threshold: float = 0.65):
    """Find communities in the similarity graph using label propagation.
    
    Returns dict of {community_label: [node_ids]} for communities with 3+ members.
    Communities are natural clusters of vector-similar memories.
    
    The threshold parameter is used to build the adjacency graph — two nodes
    are connected if their cosine similarity >= threshold. Higher thresholds
    produce smaller, tighter communities.
    """
    if sim_matrix is None or not memories:
        return {}
    
    n = len(memories)
    # Build adjacency: nodes are connected if similarity >= threshold
    adj = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if float(sim_matrix[i][j]) >= threshold:
                adj[i].add(j)
                adj[j].add(i)
    
    # Label propagation
    labels = list(range(n))
    changed = True
    max_iter = 20
    it = 0
    while changed and it < max_iter:
        changed = False
        it += 1
        # Random order for stability
        order = list(range(n))
        import random
        random.shuffle(order)
        for i in order:
            if not adj[i]:
                continue
            # Count neighbor labels
            neighbor_labels = {}
            for nb in adj[i]:
                lbl = labels[nb]
                neighbor_labels[lbl] = neighbor_labels.get(lbl, 0) + 1
            if not neighbor_labels:
                continue
            best_label = max(neighbor_labels, key=neighbor_labels.get)
            if labels[i] != best_label:
                labels[i] = best_label
                changed = True
    
    # Group by final label
    groups = {}
    for i, lbl in enumerate(labels):
        if lbl not in groups:
            groups[lbl] = []
        groups[lbl].append(memories[i])
    
    # Only return communities with 3+ members
    communities = {}
    for lbl, ids in groups.items():
        if len(ids) >= 3:
            communities[lbl] = ids
    
    return communities


def _build_raw_graph(store, threshold: float = 0.65) -> dict:
    """Pure vector links — no hubs, no artefacts. The real LanceDB."""
    nodes, edges, _, _, _, _ = _compute_vector_data(store, threshold)
    return {"nodes": nodes, "edges": edges}


def _build_category_hub_graph(store, threshold: float = 0.65) -> dict:
    """Vector links + category-based hubs (tech, correction, fact, project, etc.)
    
    Groups nodes by their DB category field, not by label prefix.
    Creates one hub per active category with the category's neon color.
    """
    nodes, edges, nodes_by_id, _, _, _ = _compute_vector_data(store, threshold)
    if not nodes:
        return {"nodes": [], "edges": []}

    seen_edges = set()
    for e in edges:
        seen_edges.add(tuple(sorted([e["from"], e["to"]])))

    # Category colors — match the neon palette from graph.js
    cat_colors = {
        "tech": "#f59e0b", "correction": "#ef4444", "fact": "#06b6d4",
        "project": "#10b981", "pattern": "#f97316", "user_pref": "#ec4899",
        "reference": "#3b82f6", "insight": "#22c55e", "decision": "#8b5cf6",
        "question": "#a855f7",
    }
    cat_labels = {
        "tech": "Tech", "correction": "Correction", "fact": "Fact",
        "project": "Project", "pattern": "Pattern", "user_pref": "Pref",
        "reference": "Reference", "insight": "Insight", "decision": "Decision",
        "question": "Question",
    }

    # Group nodes by DB category
    cat_groups = {}
    for n in nodes:
        cat = n.get("category", "fact")
        if cat not in cat_groups:
            cat_groups[cat] = []
        cat_groups[cat].append(n)

    used_labels = set()
    for cat, items in sorted(cat_groups.items(), key=lambda x: -len(x[1])):
        if len(items) < 2:
            continue

        label = cat_labels.get(cat, cat.capitalize())
        hub_label = f"◆ {label}"
        if hub_label in used_labels:
            continue
        used_labels.add(hub_label)

        summaries = [item.get("label", "?")[:40] for item in items[:5]]
        hub_id = f"hub:cat_{cat}"
        color = cat_colors.get(cat, "#6366f1")

        nodes.append({
            "id": hub_id,
            "label": hub_label,
            "title": f"{label} ({len(items)} entries): {' · '.join(summaries)}",
            "color": "#12122a", "size": 32,
            "category": "hub", "node_type": "hub",
            "created_at": 0,
            "entities": [cat],
            "fontColor": color,
        })

        for item in items:
            ek = tuple(sorted([hub_id, item["id"]]))
            if ek not in seen_edges:
                seen_edges.add(ek)
                edges.append({
                    "from": hub_id,
                    "to": item["id"],
                    "label": "domain",
                    "color": {"color": "#334155", "opacity": 0.35},
                })

    return {"nodes": nodes, "edges": edges}


def _build_entity_clustered_graph(store, threshold: float = 0.65) -> dict:
    """Vector links + entity-based hubs for readability.
    
    Groups nodes by their most representative entities (from extract_entities
    in the DB). Shows which real entities dominate the memory graph.
    Same vector edges as raw mode.
    """
    nodes, edges, nodes_by_id, _, _, _ = _compute_vector_data(store, threshold)
    if not nodes:
        return {"nodes": [], "edges": []}

    seen_edges = set()
    for e in edges:
        seen_edges.add(tuple(sorted([e["from"], e["to"]])))

    cat_colors = {
        "user_pref": "#ec4899", "project": "#10b981",
        "tech": "#f59e0b", "correction": "#ef4444", "fact": "#06b6d4",
    }

    # Count entity frequency across ALL nodes
    entity_counts = {}
    for n in nodes:
        for ent in n.get("entities", []):
            el = ent.lower()
            if el in ('go', 'code', 'path', 'api', 'cli', 'gui', 'ui', 'ux',
                       'git', 'github', 'config', 'false', 'true',
                       'liste', 'recents', 'tous', 'vite', 'pas'):
                continue
            entity_counts[el] = entity_counts.get(el, 0) + 1

    # Keep entities present in 3+ memories
    top_entities = {e: c for e, c in entity_counts.items() if c >= 3}

    if not top_entities:
        return {"nodes": nodes, "edges": edges}

    # Sort by frequency, keep top 8 to avoid too many hubs
    sorted_entities = sorted(top_entities, key=top_entities.get, reverse=True)[:8]

    used_labels = set()
    for ent in sorted_entities:
        # Find which nodes have this entity
        items = [n for n in nodes if ent in [e.lower() for e in n.get("entities", [])]]
        if len(items) < 2:
            continue

        hub_label = f"#{ent}"
        if hub_label in used_labels:
            continue
        used_labels.add(hub_label)

        summaries = [item.get("label", "?")[:30] for item in items[:6]]
        hub_id = f"hub:ent_{ent}"

        # Pick color from most common category in the group
        cats = [it["category"] for it in items]
        main_cat = max(set(cats), key=cats.count)
        color = cat_colors.get(main_cat, "#6b7280")

        nodes.append({
            "id": hub_id,
            "label": hub_label,
            "title": f"#{ent} ({len(items)} entries, x{top_entities[ent]}): {' · '.join(summaries)}",
            "color": "#1a1a2e", "size": 28,
            "category": "hub", "node_type": "hub",
            "created_at": 0,
            "entities": [ent],
            "fontColor": color,
        })

        for item in items:
            ek = tuple(sorted([hub_id, item["id"]]))
            if ek not in seen_edges:
                seen_edges.add(ek)
                edges.append({
                    "from": hub_id,
                    "to": item["id"],
                    "label": "tagged",
                    "color": {"color": "#334155", "opacity": 0.35},
                })

    return {"nodes": nodes, "edges": edges}


def search_memories(query: str, top_k: int = 20) -> dict:
    """Semantic search over memories using vector similarity."""
    if not query or not query.strip():
        return {"error": "Missing query", "results": []}

    try:
        store = _get_store()
        results = store.search(query, top_k=top_k)
        return {
            "query": query,
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        return {"error": str(e), "query": query, "results": []}


def get_stats() -> dict:
    """Return store statistics — enriched with tier/age/access distribution.
    Uses cached computation to avoid full DB scan on every request.
    """
    try:
        shared = _get_cached_stats()
        return {
            "total_memories": shared["total_memories"],
            "total_entities": shared["total_entities"],
            "categories": shared["categories"],
            "types": shared["types"],
            "tiers": shared["tiers"],
            "ages": shared["ages"],
            "access": shared["access"],
            "db_size_bytes": shared["db_size_bytes"],
            "db_size_mb": shared["db_size_mb"],
        }
    except Exception as e:
        return {"error": str(e)}


def delete_memory(memory_id: str) -> dict:
    """Delete a memory by ID."""
    if not memory_id:
        return {"error": "Missing memory_id"}
    try:
        store = _get_store()
        ok = store.delete(memory_id)
        _reset_store()
        if ok:
            return {"success": True, "message": f"Memory {memory_id} deleted"}
        return {"error": "Memory not found"}
    except Exception as e:
        _reset_store()
        return {"error": str(e)}


def update_memory(data: dict) -> dict:
    """Update memory content or category."""
    memory_id = data.get("memory_id", "")
    content = data.get("content", "")
    category = data.get("category", "")

    if not memory_id:
        return {"error": "Missing memory_id"}

    try:
        store = _get_store()
        updates = {}
        if content:
            updates["content"] = content
        if category:
            updates["category"] = category
        if not updates:
            return {"error": "No fields to update"}
        if store.update(memory_id, **updates):
            _invalidate_cache()
            return {"success": True, "message": "Memory updated"}
        return {"error": "Memory not found or update failed"}
    except Exception as e:
        return {"error": str(e)}


def update_memory_entities(data: dict) -> dict:
    """Update entities (tags) for a memory. Rebuilds links."""
    memory_id = data.get("memory_id", "")
    entities = data.get("entities", [])

    if not memory_id:
        return {"error": "Missing memory_id"}
    if not isinstance(entities, list):
        return {"error": "entities must be a list"}

    try:
        store = _get_store()
        ok = store.update_entities(memory_id, entities)
        if ok:
            return {"success": True, "message": "Entities updated, links rebuilt"}
        return {"error": "Memory not found"}
    except Exception as e:
        return {"error": str(e)}


def export_memories() -> dict:
    """Export all memories as clean JSON array (no vectors)."""
    try:
        store = _get_store()
        memories = store.get_all()
        # Strip vectors, keep only useful fields
        clean = []
        for m in memories:
            clean.append({
                "id": m["id"],
                "content": m["content"],
                "category": m.get("category", "fact"),
                "entities": m.get("entities", []),
                "links": m.get("links", []),
                "relations": m.get("relations", []),
                "tags": m.get("tags", []),
                "quality": m.get("quality", 0.5),
                "type": m.get("type", m.get("category", "fact")),
                "created_at": m.get("created_at", 0),
                "updated_at": m.get("updated_at", 0),
                "access_count": m.get("access_count", 0),
            })
        return {"memories": clean, "count": len(clean)}
    except Exception as e:
        return {"error": str(e), "memories": []}


def import_memories(data: dict) -> dict:
    """Import memories from JSON. Skips duplicates by ID, rebuilds links."""
    items = data.get("memories", [])
    if not isinstance(items, list) or not items:
        return {"error": "Missing or empty 'memories' array"}

    try:
        store = _get_store()
        existing_ids = {m["id"] for m in store.get_all()}
        imported, skipped = 0, 0
        now = time.time()
        rows_to_add = []

        for item in items:
            mem_id = item.get("id", "")
            content = item.get("content", "")
            if not content or not content.strip():
                skipped += 1
                continue
            if mem_id and mem_id in existing_ids:
                skipped += 1
                continue

            # Generate ID if missing
            from uuid import uuid4
            if not mem_id:
                mem_id = str(uuid4())[:12]

            category = item.get("category", "fact")
            entities = item.get("entities", [])
            tags = item.get("tags", [])
            quality = item.get("quality", 0.5)
            mem_type = item.get("type", category)
            created_at = item.get("created_at", now)
            access_count = item.get("access_count", 0)

            # Embed the content
            try:
                vector = store._embed(content)
            except Exception:
                import numpy as np
                vector = np.zeros(768, dtype=np.float32)

            rows_to_add.append({
                "id": mem_id,
                "content": content,
                "category": category,
                "entities": json.dumps(entities),
                "links": json.dumps([]),
                "tags": json.dumps(tags),
                "quality": quality,
                "type": mem_type,
                "source": "import",
                "session_id": "",
                "user_id": "hermes-user",
                "created_at": created_at,
                "updated_at": now,
                "access_count": access_count,
                "vector": vector,
            })
            imported += 1

        if rows_to_add:
            store._table.add(rows_to_add)

        # Rebuild all links (entities may have changed due to new memories)
        _rebuild_all_links(store, existing_ids | {r["id"] for r in rows_to_add})

        _reset_store()
        return {"success": True, "imported": imported, "skipped": skipped}
    except Exception as e:
        _reset_store()
        return {"error": str(e), "imported": 0, "skipped": 0}


def _rebuild_all_links(store, memory_ids: set) -> None:
    """Rebuild links for all memories based on shared entities (2+ threshold)."""
    store._rebuild_all_links()


def get_memory_detail(memory_id: str, threshold: float = 0.65) -> dict:
    """Get full detail for a single memory."""
    if not memory_id:
        return {"error": "Missing memory_id"}
    try:
        store = _get_store()
        memory = store.get_by_id(memory_id)
        if not memory:
            return {"error": "Memory not found"}

        # Find linked memories via real vector similarity (cosine)
        # Must use raw to get vector — get_by_id() pops it
        links = []
        try:
            import numpy as np
            raw_memory = store._get_by_id_raw(memory_id)
            target_vec = raw_memory.get("vector") if raw_memory else None
            if target_vec is not None and len(np.array(target_vec, dtype=np.float32)) >= 2:
                target_arr = np.array(target_vec, dtype=np.float32)
                tnorm = np.linalg.norm(target_arr)
                if tnorm >= 0.001:
                    target_arr = target_arr / tnorm
                    all_raw = store._get_all_raw()
                    for r in all_raw:
                        if r["id"] == memory_id:
                            continue
                        vec = r.get("vector")
                        if not vec or not isinstance(vec, (list, np.ndarray)) or len(np.array(vec, dtype=np.float32)) < 2:
                            continue
                        v_arr = np.array(vec, dtype=np.float32)
                        v_norm = np.linalg.norm(v_arr)
                        if v_norm < 0.001:
                            continue
                        v_arr = v_arr / v_norm
                        score = float(np.dot(target_arr, v_arr))
                        if score >= threshold:
                            links.append({
                                "id": r["id"],
                                "content": r["content"][:100],
                                "category": r.get("category", "fact"),
                                "score": score,
                            })
                    links.sort(key=lambda x: x["score"], reverse=True)
        except Exception:
            pass

        memory["linked_memories"] = links
        memory.pop("vector", None)
        return memory
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# API handlers — new memviz endpoints
# ---------------------------------------------------------------------------

def api_get_memories(params: dict) -> dict:
    """GET /api/memories — filtered, paginated memory list."""
    try:
        store = _get_store()
        return store.get_by_filters(
            category=params.get("category"),
            type_=params.get("type"),
            tag=params.get("tag"),
            quality_min=float(params["quality_min"]) if params.get("quality_min") else None,
            quality_max=float(params["quality_max"]) if params.get("quality_max") else None,
            date_from=float(params["date_from"]) if params.get("date_from") else None,
            date_to=float(params["date_to"]) if params.get("date_to") else None,
            search_query=params.get("search"),
            offset=int(params.get("offset", "0")),
            limit=int(params.get("limit", "20")),
        )
    except Exception as e:
        return {"error": str(e), "memories": [], "total": 0}


def api_get_tags() -> dict:
    """GET /api/tags — tag cloud."""
    try:
        store = _get_store()
        return store.get_tags()
    except Exception as e:
        return {"error": str(e)}


def api_get_timeline() -> list:
    """GET /api/timeline — daily memory counts."""
    try:
        store = _get_store()
        return store.get_timeline()
    except Exception as e:
        return {"error": str(e)}


def api_get_duplicates(threshold: float = 0.9) -> list:
    """GET /api/duplicates — near-duplicate groups."""
    try:
        store = _get_store()
        return store.get_duplicates(threshold)
    except Exception as e:
        return {"error": str(e)}


def api_get_projection(n_neighbors: int = 15, min_dist: float = 0.1) -> list:
    """GET /api/projection — UMAP 2D projection."""
    try:
        store = _get_store()
        return store.get_projection(n_neighbors, min_dist)
    except Exception as e:
        return {"error": str(e)}


def api_get_clusters(threshold: float = 0.6, min_size: int = 2) -> list:
    """GET /api/clusters — semantic clusters."""
    try:
        store = _get_store()
        return store.get_clusters(threshold, min_size)
    except Exception as e:
        return {"error": str(e)}


def api_get_stale(days: int = 90, quality_max: float = 0.3) -> list:
    """GET /api/stale — old + low quality memories."""
    try:
        store = _get_store()
        return store.get_stale(days, quality_max)
    except Exception as e:
        return {"error": str(e)}


def api_get_conflicts(params: dict) -> list:
    """GET /api/conflicts - deterministic contradiction ledger."""
    try:
        status = params.get("status", "")
        if status not in {"", "open", "resolved"}:
            return {"error": "status must be empty, open, or resolved"}
        store = _get_store()
        return store.get_conflicts(
            status=status,
            memory_id=params.get("memory_id", ""),
            limit=min(max(int(params.get("limit", 100)), 1), 500),
        )
    except Exception as e:
        return {"error": str(e)}


def api_resolve_conflict(conflict_id: str, data: dict) -> dict:
    """POST /api/conflicts/:id/resolve — resolve with an audit trail."""
    resolution_note = str(data.get("resolution_note") or "").strip()
    resolved_by = str(data.get("resolved_by") or "user").strip()
    if not conflict_id:
        return {"error": "Missing conflict_id"}
    if not resolution_note:
        return {"error": "Missing resolution_note"}
    if not resolved_by:
        return {"error": "Missing resolved_by"}
    try:
        ok = _get_store().resolve_conflict(
            conflict_id,
            resolution_note=resolution_note,
            resolved_by=resolved_by,
        )
        if ok:
            _invalidate_cache()
            return {"success": True, "conflict_id": conflict_id}
        return {"error": "Conflict not found or already resolved"}
    except Exception as error:
        return {"error": str(error)}


def api_get_dashboard() -> dict:
    """GET /api/dashboard — enriched stats for the dashboard view.
    Uses cached computation to avoid full DB scan on every request.
    """
    try:
        shared = _get_cached_stats()
        return {
            "total": shared["total"],
            "total_memories": shared["total_memories"],
            "categories": shared["categories"],
            "types": shared["types"],
            "tiers": shared.get("tiers", {}),
            "tags": shared["tags"],
            "avg_quality": shared["avg_quality"],
            "total_access_count": shared["total_access_count"],
            "never_accessed": shared["never_accessed"],
            "top_accessed": shared["top_accessed"],
            "db_size_bytes": shared["db_size_bytes"],
            "db_size_mb": shared["db_size_mb"],
        }
    except Exception as e:
        return {"error": str(e), "total": 0, "categories": {}, "types": {}, "tags": {}, "top_accessed": []}


# ---------------------------------------------------------------------------
# POST handler helpers — new memviz endpoints
# ---------------------------------------------------------------------------

def api_update_memory(memory_id: str, data: dict) -> dict:
    """POST /api/memories/:id — update content/category/tags/quality/type."""
    if not memory_id:
        return {"error": "Missing memory_id"}
    try:
        store = _get_store()
        kwargs = {}
        for key in ("content", "category", "tags", "quality", "type"):
            if key in data:
                kwargs[key] = data[key]
        if not kwargs:
            return {"error": "No fields to update"}
        ok = store.update(memory_id, **kwargs)
        if ok:
            _invalidate_cache()
            return {"success": True, "message": f"Memory {memory_id} updated"}
        return {"error": "Memory not found or update failed"}
    except Exception as e:
        return {"error": str(e)}

def api_access_memory(memory_id: str) -> dict:
    """POST /api/memories/:id/access — increment access count."""
    if not memory_id:
        return {"error": "Missing memory_id"}
    try:
        store = _get_store()
        memory = store.get_by_id(memory_id)
        if not memory:
            return {"error": "Memory not found"}
        _invalidate_cache()
        return {"success": True, "access_count": memory["access_count"]}
    except Exception as e:
        return {"error": str(e)}

def api_bulk_delete(data: dict) -> dict:
    """POST /api/memories/bulk-delete — batch delete."""
    memory_ids = data.get("memory_ids", [])
    if not isinstance(memory_ids, list) or not memory_ids:
        return {"error": "Missing or empty memory_ids"}
    try:
        store = _get_store()
        result = store.bulk_delete(memory_ids)
        _reset_store()
        return result
    except Exception as e:
        _reset_store()
        return {"error": str(e), "deleted": 0, "errors": []}


def api_bulk_tag(data: dict) -> dict:
    """POST /api/memories/bulk-tag — add/remove tags on multiple memories."""
    memory_ids = data.get("memory_ids", [])
    add_tags = data.get("add_tags", [])
    remove_tags = data.get("remove_tags", [])
    if not isinstance(memory_ids, list) or not memory_ids:
        return {"error": "Missing or empty memory_ids"}
    try:
        store = _get_store()
        result = store.bulk_tag(memory_ids, add_tags=add_tags, remove_tags=remove_tags)
        _invalidate_cache()
        return result
    except Exception as e:
        return {"error": str(e), "updated": 0, "errors": []}


def api_bulk_type(data: dict) -> dict:
    """POST /api/memories/bulk-type — set type on multiple memories."""
    memory_ids = data.get("memory_ids", [])
    mem_type = data.get("type", "")
    if not isinstance(memory_ids, list) or not memory_ids:
        return {"error": "Missing or empty memory_ids"}
    if not mem_type:
        return {"error": "Missing type"}
    try:
        store = _get_store()
        updated = 0
        errors = []
        for mid in memory_ids:
            try:
                ok = store.update(mid, type=mem_type)
                if ok:
                    updated += 1
                else:
                    errors.append(mid)
            except Exception:
                errors.append(mid)
        _invalidate_cache()
        return {"updated": updated, "errors": errors}
    except Exception as e:
        return {"error": str(e), "updated": 0, "errors": []}


def api_rename_tag(data: dict) -> dict:
    """POST /api/tags/rename — rename a tag."""
    old_name = data.get("old_name", "")
    new_name = data.get("new_name", "")
    if not old_name or not new_name:
        return {"error": "Missing old_name or new_name"}
    try:
        store = _get_store()
        count = store.rename_tag(old_name, new_name)
        _invalidate_cache()
        return {"success": True, "updated": count}
    except Exception as e:
        return {"error": str(e), "updated": 0}


def api_delete_tag(data: dict) -> dict:
    """POST /api/tags/delete — delete a tag from all memories."""
    tag = data.get("tag", "")
    if not tag:
        return {"error": "Missing tag"}
    try:
        store = _get_store()
        count = store.delete_tag(tag)
        _invalidate_cache()
        return {"success": True, "updated": count}
    except Exception as e:
        return {"error": str(e), "updated": 0}


def api_merge_tags(data: dict) -> dict:
    """POST /api/tags/merge — merge multiple tags into one."""
    sources = data.get("sources", [])
    target = data.get("target", "")
    if not isinstance(sources, list) or not sources:
        return {"error": "Missing or empty sources"}
    if not target:
        return {"error": "Missing target"}
    try:
        store = _get_store()
        count = store.merge_tags(sources, target)
        _invalidate_cache()
        return {"success": True, "updated": count}
    except Exception as e:
        return {"error": str(e), "updated": 0}


# ---------------------------------------------------------------------------
# URL routing helpers
# ---------------------------------------------------------------------------

# Pattern for /api/memories/:id (captures the memory_id after /api/memories/)
_MEM_ID_RE = re.compile(r"^/api/memories/([a-zA-Z0-9_-]+?)(?:/access)?$")
_CONFLICT_RESOLVE_RE = re.compile(
    r"^/api/conflicts/([a-zA-Z0-9_-]+)/resolve$"
)


def _parse_memories_id_path(path: str):
    """Match /api/memories/:id or /api/memories/:id/access.
    Returns (memory_id, sub_action) or None.
    sub_action is '' for /api/memories/:id, 'access' for /api/memories/:id/access
    """
    # Check /api/memories/:id/access first
    if path.endswith("/access"):
        base = path[:-7]  # strip /access
        m = _MEM_ID_RE.match(base)
        if m:
            return m.group(1), "access"
    # Then check /api/memories/:id
    m = _MEM_ID_RE.match(path)
    if m:
        return m.group(1), ""
    return None


def _parse_conflict_resolution_path(path: str):
    """Return the conflict ID for POST /api/conflicts/:id/resolve."""
    match = _CONFLICT_RESOLVE_RE.match(path)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # --- Legacy endpoints (kept exactly as before) ---
        if path == "/api/graph":
            cluster = params.get("cluster", ["raw"])[0]
            threshold = float(params.get("threshold", ["0.65"])[0])
            self._send_json(get_graph_data(cluster=cluster, threshold=threshold))
        elif path == "/api/stats":
            self._send_json(get_stats())
        elif path == "/api/typed-edges":
            try:
                store = _get_store()
                self._send_json({"edges": store.get_typed_edges()})
            except Exception as e:
                self._send_json({"error": str(e)})
        elif path == "/api/search":
            query = params.get("q", [""])[0]
            top_k = int(params.get("top_k", ["20"])[0])
            self._send_json(search_memories(query, top_k=top_k))
        elif path == "/api/export":
            result = export_memories()
            self._send_json(result, download="lancedb-memories.json")
        elif path == "/api/memory":
            mem_id = params.get("id", [""])[0]
            threshold = float(params.get("threshold", ["0.65"])[0])
            self._send_json(get_memory_detail(mem_id, threshold))

        # --- New memviz GET endpoints ---
        elif path == "/api/memories":
            # Filtered list
            p = {k: v[0] for k, v in params.items()}
            self._send_json(api_get_memories(p))
        elif path == "/api/tags":
            self._send_json(api_get_tags())
        elif path == "/api/timeline":
            self._send_json(api_get_timeline())
        elif path == "/api/duplicates":
            threshold = float(params.get("threshold", ["0.9"])[0])
            self._send_json(api_get_duplicates(threshold))
        elif path == "/api/projection":
            n_neighbors = int(params.get("n_neighbors", ["15"])[0])
            min_dist = float(params.get("min_dist", ["0.1"])[0])
            self._send_json(api_get_projection(n_neighbors, min_dist))
        elif path == "/api/clusters":
            threshold = float(params.get("threshold", ["0.6"])[0])
            min_size = int(params.get("min_size", ["2"])[0])
            self._send_json(api_get_clusters(threshold, min_size))
        elif path == "/api/stale":
            days = int(params.get("days", ["90"])[0])
            quality_max = float(params.get("quality_max", ["0.3"])[0])
            self._send_json(api_get_stale(days, quality_max))
        elif path == "/api/conflicts":
            p = {k: v[0] for k, v in params.items()}
            self._send_json(api_get_conflicts(p))
        elif path == "/api/dashboard":
            self._send_json(api_get_dashboard())
        elif path == "/api/refresh":
            _reset_store()
            self._send_json({"status": "ok"})

        # --- /api/memories/:id GET (detail) ---
        else:
            match = _parse_memories_id_path(path)
            if match and match[1] == "":
                # GET /api/memories/:id — same as /api/memory?id=
                mem_id = match[0]
                threshold = float(params.get("threshold", ["0.65"])[0])
                self._send_json(get_memory_detail(mem_id, threshold))
            elif path == "/" or path == "/index.html":
                self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                filepath = STATIC_DIR / path[8:]
                self._serve_file(filepath)
            else:
                self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # --- Legacy POST endpoints (kept exactly as before) ---
        if path == "/api/delete":
            data = self._read_json()
            memory_id = data.get("memory_id", "")
            result = delete_memory(memory_id)
            self._send_json(result)
        elif path == "/api/update":
            data = self._read_json()
            result = update_memory(data)
            self._send_json(result)
        elif path == "/api/update_entities":
            data = self._read_json()
            result = update_memory_entities(data)
            self._send_json(result)
        elif path == "/api/import":
            data = self._read_json()
            result = import_memories(data)
            self._send_json(result)

        elif conflict_id := _parse_conflict_resolution_path(path):
            data = self._read_json()
            self._send_json(api_resolve_conflict(conflict_id, data))

        # --- New memviz POST endpoints ---
        elif path == "/api/memories/bulk-delete":
            data = self._read_json()
            self._send_json(api_bulk_delete(data))
        elif path == "/api/memories/bulk-tag":
            data = self._read_json()
            self._send_json(api_bulk_tag(data))
        elif path == "/api/memories/bulk-type":
            data = self._read_json()
            self._send_json(api_bulk_type(data))
        elif path == "/api/tags/rename":
            data = self._read_json()
            self._send_json(api_rename_tag(data))
        elif path == "/api/tags/delete":
            data = self._read_json()
            self._send_json(api_delete_tag(data))
        elif path == "/api/tags/merge":
            data = self._read_json()
            self._send_json(api_merge_tags(data))
        else:
            # Check /api/memories/:id or /api/memories/:id/access
            match = _parse_memories_id_path(path)
            if match:
                mem_id, sub = match
                if sub == "access":
                    # POST /api/memories/:id/access
                    self._send_json(api_access_memory(mem_id))
                else:
                    # POST /api/memories/:id — update memory
                    data = self._read_json()
                    self._send_json(api_update_memory(mem_id, data))
            else:
                self._send_json({"error": "Not found"}, 404)

    def _read_json(self) -> dict:
        """Read and parse JSON body from request."""
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        try:
            return json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _send_json(self, data: dict, status: int = 200, download: str = None):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        if download:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        else:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath: Path, mime: str | None = None):
        if not filepath.exists() or not filepath.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript",
            ".css": "text/css",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".json": "application/json",
        }
        mime = mime or content_types.get(filepath.suffix, "application/octet-stream")

        body = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default logging — use our own."""
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="LanceDB Memory Graph Visualizer")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port (default: {PORT})")
    parser.add_argument("--host", default=HOST, help=f"Host (default: {HOST})")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"LanceDB Memory Graph Visualizer")
    print(f"  Open:  http://{args.host}:{args.port}")
    print(f"  Data:  {LANCEDB_PATH}")
    print(f"  Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
