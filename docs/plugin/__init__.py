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

from .store import LanceDBStore  # noqa: F401 — used in initialize()

logger = logging.getLogger(__name__)

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
        },
        "required": ["query"],
    },
}

_ADD_SCHEMA = {
    "name": "lancedb_add",
    "description": (
        "Store a durable fact in LanceDB vector memory. "
        "Auto-extracts entities and builds links to related memories. "
        "Use for project details, user preferences, tech stack, bugs, "
        "decisions — anything you'll want to recall later.\n\n"
        "PREFERRED: use domain + subject + content + tier (structured fields). "
        "The handler assembles 'Domain:Subject info [Tier=N]' automatically.\n"
        "LEGACY: pass a single 'content' string — must include Domain:Subject prefix and [Tier=N] suffix."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "Fact details (keys and values). "
                    "PREFERRED: omit this and use domain+subject+content+tier instead — the handler builds the full string. "
                    "LEGACY only: pass the full string 'Domain:Subject key=value key=value. [Tier=N]' — must include the prefix and tier marker."
                ),
            },
            "domain": {
                "type": "string",
                "description": "Domain namespace for clustering (e.g. Hermes, Projet, Tech, User, Correction, Fact, Config). Forms the 'Domain:' prefix.",
            },
            "subject": {
                "type": "string",
                "description": "Specific subject within the domain (e.g. Service, Profile, GPU, Pipeline). Forms ':Subject' suffix — combined with domain to form 'Domain:Subject' key.",
            },
            "tier": {
                "type": "string",
                "enum": ["1", "2", "3"],
                "description": "Importance tier: 1=critical (bugs, corrections, commands), 2=useful (stack, URLs, archi), 3=contextual (notes de fond). REQUIRED when using domain+subject.",
            },
            "category": {
                "type": "string",
                "enum": ["project", "tech", "fact", "correction", "user_pref", "decision", "insight", "reference", "pattern", "question"],
                "description": "Category for grouping in the graph (default: 'fact'). Priority order: correction > pattern > decision > user_pref > reference > insight > project > tech > fact > question. Pick the highest applicable.",
            },
        },
        "required": [],  # Use EITHER content (legacy) OR domain+subject+tier (+ optional content)
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
        return [_SEARCH_SCHEMA, _ADD_SCHEMA, _GRAPH_SCHEMA, _DELETE_SCHEMA, _GET_SCHEMA, _LIST_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "lancedb_search":
            return self._handle_search(args)
        elif tool_name == "lancedb_add":
            return self._handle_add(args)
        elif tool_name == "lancedb_graph":
            return self._handle_graph(args)
        elif tool_name == "lancedb_delete":
            return self._handle_delete(args)
        elif tool_name == "lancedb_get":
            return self._handle_get(args)
        elif tool_name == "lancedb_list":
            return self._handle_list(args)
        return tool_error(f"Unknown tool: {tool_name}")

    # -------------------------------------------------------------------
    # Tool handlers
    # -------------------------------------------------------------------

    def _handle_search(self, args: dict) -> str:
        query = args.get("query", "")
        top_k = min(int(args.get("top_k", 10)), 50)
        category = args.get("category", "") or None
        try:
            results = self._store.search(query, top_k=top_k, category=category)
            return json.dumps({
                "query": query,
                "count": len(results),
                "results": results,
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return tool_error(str(e))

    def _handle_add(self, args: dict) -> str:
        content = args.get("content", "")
        domain = args.get("domain", "")
        subject = args.get("subject", "")
        tier = args.get("tier", "")
        category = args.get("category", "fact")

        # Mode 1: structured fields — assemble Domain:Subject key=value [Tier=N]
        if domain or subject or tier:
            if not domain:
                return tool_error("domain is required when using structured fields")
            if not subject:
                return tool_error("subject is required when using structured fields")
            if not tier:
                return tool_error("tier is required when using structured fields (1=critical, 2=useful, 3=contextual)")

            content = f"{domain}:{subject}"
            if content_body := args.get("content", "").strip():
                content += f" {content_body}"
            content += f" [Tier={tier}]"
        # Mode 2: legacy — use content as-is
        else:
            if not content.strip():
                return tool_error("content is required (use EITHER 'content' alone OR 'domain+subject+tier+content')")

        # Validate category against enum
        VALID_CATEGORIES = {"project", "tech", "fact", "correction", "user_pref", "decision", "insight", "reference", "pattern", "question"}
        if category not in VALID_CATEGORIES:
            category = "fact"
        try:
            mem_id = self._store.add(content, category=category)
            return json.dumps({
                "success": True,
                "memory_id": mem_id,
                "message": f"Memory stored: {content[:80]}...",
            }, ensure_ascii=False)
        except Exception as e:
            return tool_error(str(e))

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