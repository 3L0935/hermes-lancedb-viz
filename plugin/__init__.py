"""LanceDB memory plugin — MemoryProvider for local vector memory.

Provides semantic search, graph export, and explicit memory write
through the MemoryProvider interface with 4 agent tools.
Auto-sync is intentionally disabled — only explicit memory writes.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .memory_contract import MemoryContractError, MemoryPatch, MemoryWrite
from .store import LanceDBStore, MemoryEmbeddingError  # noqa: F401 — used in initialize()

logger = logging.getLogger(__name__)


def _write_error(error: Exception) -> str:
    if isinstance(error, (MemoryContractError, MemoryEmbeddingError)):
        return json.dumps({
            "success": False,
            "error": error.to_dict(),
            "retryable": bool(getattr(error, "retryable", False)),
        }, ensure_ascii=False, default=str)
    return tool_error(str(error))

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_SEARCH_SCHEMA = {
    "name": "lancedb_search",
    "description": (
        "Semantic search over stored vector memories. "
        "Returns memories ranked by cosine similarity to your query, "
        "each result includes a quality score (0-1, auto-calculated from "
        "access frequency + links + freshness) and relations (typed links "
        "to related memories). Follow relations of top results for "
        "additional context. "
        "Use this before answering about the user's projects, preferences, "
        "or past decisions — avoids asking questions already stored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query — what you want to recall.",
            },
            "top_k": {
                "type": "integer",
                "description": "Max results (default: 10, max: 50).",
            },
            "category": {
                "type": "string",
                "enum": ["", "project", "tech", "fact", "correction", "user_pref", "decision", "insight", "reference", "pattern", "question"],
                "description": "Optional category filter.",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "hybrid", "lexical", "graph"],
                "description": "Local retrieval strategy. auto uses deterministic rules; graph adds bounded one-hop typed relations.",
            },
            "relation_depth": {
                "type": "integer",
                "enum": [0, 1],
                "description": "Typed-relation traversal depth. Only 0 or 1 is supported to keep recall bounded.",
            },
        },
        "required": ["query"],
    },
}

_CATEGORY_VALUES = [
    "project", "tech", "fact", "correction", "user_pref", "decision",
    "insight", "reference", "pattern", "question",
]
_RELATION_VALUES = [
    "part_of", "depends", "requires", "runs_on", "connects_to", "uses",
    "extends", "supersedes", "invalidates", "contradicts",
]
_RELATION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": _RELATION_VALUES},
        "target_id": {"type": "string", "minLength": 1},
        "target": {"type": "string", "minLength": 1},
    },
    "required": ["type"],
    "oneOf": [
        {"required": ["target_id"], "not": {"required": ["target"]}},
        {"required": ["target"], "not": {"required": ["target_id"]}},
    ],
    "additionalProperties": False,
}
_STRUCTURED_WRITE_PROPERTIES = {
    "domain": {"type": "string", "minLength": 1, "maxLength": 32},
    "subject": {"type": "string", "minLength": 1, "maxLength": 80},
    "facts": {
        "type": "array",
        "minItems": 1,
        "maxItems": 12,
        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        "description": "Dense inline facts; key=value and concise prose are both valid.",
    },
    "tier": {"type": "integer", "enum": [1, 2, 3]},
    "category": {"type": "string", "enum": _CATEGORY_VALUES},
    "relations": {"type": "array", "maxItems": 20, "items": _RELATION_SCHEMA},
}
_STRUCTURED_REQUIRED = ["domain", "subject", "facts", "tier", "category"]

_ADD_SCHEMA = {
    "name": "lancedb_add",
    "description": (
        "Store one validated durable memory from structured fields. "
        "create is non-destructive and returns update_suggested for an existing subject. "
        "Use upsert_subject only when replacement is explicitly intended."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_STRUCTURED_WRITE_PROPERTIES,
            "write_mode": {
                "type": "string",
                "enum": ["create", "upsert_subject"],
                "default": "create",
            },
        },
        "required": _STRUCTURED_REQUIRED,
        "additionalProperties": False,
    },
}

_GRAPH_SCHEMA = {
    "name": "lancedb_graph",
    "description": (
        "Export the full memory knowledge graph as nodes + edges. "
        "Nodes represent individual memories with content, category, quality (0-1), "
        "tier (1/2/3), tags, entities, and freshness (accessed_at). "
        "Edges represent entity-based links (2+ shared entities). "
        "Use this to understand how memories are connected."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_UPDATE_SCHEMA = {
    "name": "lancedb_update",
    "description": (
        "Replace an existing memory by ID using a complete structured canonical form. "
        "The response echoes both canonical and replaced content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "minLength": 1,
                "description": "Existing memory ID from search or get.",
            },
            **_STRUCTURED_WRITE_PROPERTIES,
            "tags": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string"},
                "description": "New tags (replaces existing tags entirely).",
            },
            "quality": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "type": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
            },
        },
        "required": ["memory_id", *_STRUCTURED_REQUIRED],
        "additionalProperties": False,
    },
}

_DELETE_SCHEMA = {
    "name": "lancedb_delete",
    "description": "Delete a memory by its ID. Rebuilds remaining links after deletion.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "ID of the memory to delete. Get IDs from lancedb_search, lancedb_graph, or the web UI.",
            },
        },
        "required": ["memory_id"],
    },
}

_GET_SCHEMA = {
    "name": "lancedb_get",
    "description": (
        "Get a single memory by its ID with all fields: "
        "content, category, quality, tier, tags, entities, "
        "relations (typed links to other memories), links (cosine-similar linked IDs), "
        "access_count, created_at, updated_at. "
        "Use after lancedb_search or lancedb_list to get full detail on a specific entry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "ID of the memory to retrieve.",
            },
        },
        "required": ["memory_id"],
    },
}

_LIST_SCHEMA = {
    "name": "lancedb_list",
    "description": (
        "List all memories with optional filters. "
        "Exact listing, no approximate search — use when you need every entry "
        "of a category, or when search is too broad/narrow. "
        "Returns content, category, quality, tier, tags, entities, "
        "access_count for each memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["", "project", "tech", "fact", "correction", "user_pref", "decision", "insight", "reference", "pattern", "question"],
                "description": "Optional category filter (empty = all).",
            },
            "quality_min": {
                "type": "number",
                "description": "Minimum quality score (0-1). Default 0. Filters out noise.",
            },
            "tier": {
                "type": "string",
                "enum": ["", "1", "2", "3"],
                "description": "Optional tier filter (1=critical, 2=useful, 3=contextual).",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default: 50, max: 200).",
            },
            "offset": {
                "type": "integer",
                "description": "Pagination offset (default: 0).",
            },
        },
        "required": [],
    },
}

_CONFLICTS_SCHEMA = {
    "name": "lancedb_conflicts",
    "description": (
        "List or resolve conservative local contradiction records. Conflicts are created only "
        "when memories with the same Domain:Subject key contain different explicit "
        "key=value claims. Detection is deterministic and never mutates either memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "resolve"],
                "default": "list",
                "description": "List conflicts or resolve one with an audit note.",
            },
            "status": {
                "type": "string",
                "enum": ["", "open", "resolved", "archived"],
                "description": "Filter by state/storage; empty returns the hot registry.",
            },
            "memory_id": {
                "type": "string",
                "description": "Optional memory ID to inspect.",
            },
            "limit": {
                "type": "integer",
                "default": 100,
                "description": "Maximum rows to return (1-500).",
            },
            "conflict_id": {
                "type": "string",
                "description": "Conflict ID required by the resolve action.",
            },
            "resolution_note": {
                "type": "string",
                "description": "Required audit note explaining a resolution.",
            },
            "resolved_by": {
                "type": "string",
                "description": "Actor recorded in the audit trail (default: user).",
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------


class LanceDBMemoryProvider(MemoryProvider):
    """Vector memory provider using LanceDB + Ollama embeddings."""

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._store = None
        self._db_path = None

    @property
    def name(self) -> str:
        return "lancedb"

    def is_available(self) -> bool:
        try:
            import lancedb  # noqa: F401
            return True
        except ImportError:
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "db_path",
                "description": "Path to LanceDB database directory",
                "default": "~/.hermes/lancedb",
            },
            {
                "key": "embed_model",
                "description": "Ollama embedding model name",
                "default": "nomic-embed-text",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        from pathlib import Path
        import yaml
        config_path = Path(hermes_home) / "config.yaml"
        existing = {}
        if config_path.exists():
            with open(config_path, encoding="utf-8-sig") as f:
                existing = yaml.safe_load(f) or {}
        existing.setdefault("memory", {}).setdefault("lancedb", {})
        for k, v in values.items():
            existing["memory"]["lancedb"][k] = v
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, default_flow_style=False)

    def initialize(self, session_id: str, **kwargs) -> None:
        from pathlib import Path
        from hermes_constants import get_hermes_home

        hermes_home = get_hermes_home()
        db_path = self._config.get("db_path", str(hermes_home / "lancedb"))
        # Resolve ~ and $HERMES_HOME
        db_path = db_path.replace("$HERMES_HOME", str(hermes_home))
        db_path = db_path.replace("${HERMES_HOME}", str(hermes_home))
        db_path = str(Path(db_path).expanduser())

        self._db_path = db_path

        # Set embed model env var if configured
        if embed_model := self._config.get("embed_model"):
            import os as _os
            _os.environ["LANCE_EMBED_MODEL"] = embed_model

        self._store = LanceDBStore(db_path)

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            count = self._store.count()
        except Exception:
            count = 0
        if count == 0:
            return (
                "# LanceDB Memory\n"
                "Active. Empty store — proactively add facts the user would expect you to remember.\n"
                "Use lancedb_add to store durable facts about projects, preferences, tech stack.\n"
                "Use lancedb_search to recall before answering about past decisions.\n"
                "Use lancedb_graph to explore how memories connect.\n"
            )
        return (
            f"# LanceDB Memory\n"
            f"Active. {count} memories stored with vector search and entity linking.\n"
            f"Use lancedb_search to recall context before answering.\n"
            f"  Each result includes quality (0-1, filter <0.3 as noise) and relations (typed links).\n"
            f"  Follow relations of top results for richer context.\n"
            f"Use lancedb_list to list ALL memories with filters (category, tier, quality_min).\n"
            f"  No approximate search — use when you need every entry of a category.\n"
            f"Use lancedb_get to get full detail on a single memory by ID (includes relations).\n"
            f"Use lancedb_add to store new facts as they come up.\n"
            f"Use lancedb_update to edit an existing memory in-place (content, category, tags, quality) — prefer over delete+recreate.\n"
            f"Use lancedb_graph to explore memory connections.\n"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._store or not query:
            return ""
        try:
            results = self._store.search(query, top_k=5)
            if not results:
                return ""
            lines = []
            for r in results:
                cat = r.get("category", "?")
                score = r.get("score", 0)
                quality = r.get("quality", 0.5) or 0.5
                content = r.get("content", "")
                rels = r.get("relations", [])
                rel_str = ""
                if rels:
                    rel_str = " →rels: " + ", ".join(f"{rel.get('type','?')}={rel.get('target','?')}" for rel in rels)
                lines.append(f"[{cat}] ({score:.2f} q={quality:.2f}) {content}{rel_str}")
            return "## LanceDB Memory\n" + "\n".join(lines)
        except Exception as e:
            logger.debug("LanceDB prefetch failed: %s", e)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list | None = None) -> None:
        """Auto-sync is intentionally disabled. Only explicit lancedb_add."""
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            _SEARCH_SCHEMA, _ADD_SCHEMA, _GRAPH_SCHEMA, _DELETE_SCHEMA,
            _UPDATE_SCHEMA, _GET_SCHEMA, _LIST_SCHEMA, _CONFLICTS_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "lancedb_search":
            return self._handle_search(args)
        elif tool_name == "lancedb_add":
            return self._handle_add(args)
        elif tool_name == "lancedb_graph":
            return self._handle_graph(args)
        elif tool_name == "lancedb_delete":
            return self._handle_delete(args)
        elif tool_name == "lancedb_update":
            return self._handle_update(args)
        elif tool_name == "lancedb_get":
            return self._handle_get(args)
        elif tool_name == "lancedb_list":
            return self._handle_list(args)
        elif tool_name == "lancedb_conflicts":
            return self._handle_conflicts(args)
        return tool_error(f"Unknown tool: {tool_name}")

    # -------------------------------------------------------------------
    # Tool handlers
    # -------------------------------------------------------------------

    def _handle_search(self, args: dict) -> str:
        query = args.get("query", "")
        top_k = min(max(int(args.get("top_k", 10)), 1), 50)
        category = args.get("category", "") or None
        mode = args.get("mode", "auto")
        relation_depth = 1 if int(args.get("relation_depth", 1)) > 0 else 0
        try:
            results = self._store.search(
                query,
                top_k=top_k,
                category=category,
                mode=mode,
                relation_depth=relation_depth,
            )
            return json.dumps({
                "query": query,
                "count": len(results),
                "results": results,
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return tool_error(str(e))

    def _handle_add(self, args: dict) -> str:
        try:
            memory = MemoryWrite.from_mapping(args)
            result = self._store.add_memory(memory)
            memory_id = result.get("memory_id", "")
            try:
                potential_conflicts = self._store.get_conflicts(
                    status="open", memory_id=memory_id, limit=20
                )
            except Exception as error:
                potential_conflicts = []
                warning = {
                    "code": "conflict_listing_failed",
                    "field": "conflicts",
                    "message": "memory result is valid but conflict listing failed",
                    "received": str(error),
                    "expected": "readable conflict registry",
                }
                logger.warning("Conflict listing failed after memory write: %s", error)
                result.setdefault("warnings", []).append(warning)
            result["potential_conflicts"] = potential_conflicts
            result["message"] = f"Memory {result['status']}: {result['canonical_content'][:80]}..."
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as error:
            return _write_error(error)

    def _handle_graph(self, args: dict) -> str:
        try:
            graph = self._store.graph()
            return json.dumps({
                "nodes": graph["nodes"],
                "edges": graph["edges"],
                "node_count": len(graph["nodes"]),
                "edge_count": len(graph["edges"]),
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return tool_error(str(e))

    def _handle_delete(self, args: dict) -> str:
        memory_id = args.get("memory_id", "")
        if not memory_id:
            return tool_error("memory_id is required")
        try:
            ok = self._store.delete(memory_id)
            if ok:
                return json.dumps({
                    "success": True,
                    "message": f"Memory {memory_id} deleted.",
                })
            return json.dumps({
                "success": False,
                "message": f"Memory {memory_id} not found.",
            })
        except Exception as e:
            return tool_error(str(e))

    def _handle_update(self, args: dict) -> str:
        try:
            replacement = MemoryWrite.from_mapping({
                field: args[field]
                for field in (*_STRUCTURED_REQUIRED, "relations")
                if field in args
            })
            patch_data = {
                "memory_id": args.get("memory_id"),
                "domain": replacement.domain,
                "subject": replacement.subject,
                "facts": list(replacement.facts),
                "tier": replacement.tier,
                "category": replacement.category,
                "relations": [relation.to_dict() for relation in replacement.relations],
            }
            for field in ("tags", "quality", "type"):
                if field in args:
                    patch_data[field] = args[field]
            result = self._store.update_memory(MemoryPatch.from_mapping(patch_data))
            result["updated_fields"] = [
                field for field in args if field != "memory_id"
            ]
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as error:
            return _write_error(error)

    def _handle_get(self, args: dict) -> str:
        memory_id = args.get("memory_id", "")
        if not memory_id:
            return tool_error("memory_id is required")
        try:
            mem = self._store.get_by_id(memory_id)
            if not mem:
                return json.dumps({"success": False, "message": f"Memory {memory_id} not found."})
            # Parse tier from content
            content = mem.get("content", "")
            tier = "none"
            for t in ["1", "2", "3"]:
                if f"[Tier={t}]" in content:
                    tier = t
                    break
            mem["tier"] = tier
            return json.dumps(mem, ensure_ascii=False, default=str)
        except Exception as e:
            return tool_error(str(e))

    def _handle_list(self, args: dict) -> str:
        try:
            category = args.get("category", "") or None
            quality_min = float(args.get("quality_min", 0))
            tier_filter = args.get("tier", "") or None
            limit = min(int(args.get("limit", 50)), 200)
            offset = int(args.get("offset", 0))

            all_mems = self._store.get_all()

            # Filter + enrich
            results = []
            for m in all_mems:
                # Category filter
                if category and m.get("category") != category:
                    continue
                # Quality filter
                if quality_min > 0 and (m.get("quality") or 0) < quality_min:
                    continue
                # Tier filter
                if tier_filter:
                    content = m.get("content", "")
                    if f"[Tier={tier_filter}]" not in content:
                        continue
                # Enrich with tier
                content = m.get("content", "")
                tier = "none"
                for t in ["1", "2", "3"]:
                    if f"[Tier={t}]" in content:
                        tier = t
                        break
                m["tier"] = tier
                results.append(m)

            total = len(results)
            page = results[offset:offset + limit]

            return json.dumps({
                "total": total,
                "count": len(page),
                "offset": offset,
                "limit": limit,
                "results": page,
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return tool_error(str(e))

    def _handle_conflicts(self, args: dict) -> str:
        try:
            action = args.get("action", "list")
            if action == "resolve":
                conflict_id = str(args.get("conflict_id") or "").strip()
                resolution_note = str(args.get("resolution_note") or "").strip()
                resolved_by = str(args.get("resolved_by") or "user").strip()
                if not conflict_id:
                    return tool_error("conflict_id is required for resolve")
                if not resolution_note:
                    return tool_error("resolution_note is required for resolve")
                if not resolved_by:
                    return tool_error("resolved_by is required for resolve")
                ok = self._store.resolve_conflict(
                    conflict_id,
                    resolution_note=resolution_note,
                    resolved_by=resolved_by,
                )
                if not ok:
                    return tool_error("Conflict not found or already resolved")
                return json.dumps({
                    "success": True,
                    "conflict_id": conflict_id,
                    "resolution_note": resolution_note,
                    "resolved_by": resolved_by,
                }, ensure_ascii=False)
            if action != "list":
                return tool_error("action must be list or resolve")
            status = args.get("status", "")
            if status not in {"", "open", "resolved", "archived"}:
                return tool_error("status must be empty, open, resolved, or archived")
            conflicts = self._store.get_conflicts(
                status=status,
                memory_id=args.get("memory_id", ""),
                limit=min(max(int(args.get("limit", 100)), 1), 500),
            )
            return json.dumps({
                "count": len(conflicts),
                "conflicts": conflicts,
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return tool_error(str(e))

    def shutdown(self) -> None:
        self._store = None


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register the LanceDB memory provider with Hermes plugin system."""
    from hermes_cli.config import cfg_get
    import yaml

    config = {}
    try:
        from hermes_constants import get_hermes_home
        config_path = get_hermes_home() / "config.yaml"
        if config_path.exists():
            with open(config_path, encoding="utf-8-sig") as f:
                all_config = yaml.safe_load(f) or {}
            config = cfg_get(all_config, "memory", "lancedb", default={}) or {}
    except Exception:
        pass

    provider = LanceDBMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
