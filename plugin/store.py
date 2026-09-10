"""LanceDBStore — vector memory using LanceDB with Ollama embeddings.

CRUD, entity extraction, link building, semantic search.
Embeddings via Ollama (nomic-embed-text), local only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .memory_contract import (
    ContractIssue,
    MemoryContractError,
    MemoryPatch,
    MemoryWrite,
    contract_warnings,
    memory_fingerprint,
    parse_content,
    parse_content_parts,
    render_content,
)

logger = logging.getLogger(__name__)


class MemoryEmbeddingError(RuntimeError):
    """Retryable failure raised before a strict write reaches LanceDB."""

    retryable = True

    def __init__(self, detail: str = "embedding service returned no usable vector"):
        self.issue = ContractIssue(
            code="embedding_failed",
            field="content",
            message="memory was not written because embedding failed",
            received=detail,
            expected="a non-zero 768-dimensional embedding",
        )
        super().__init__(self.issue.message)

    def to_dict(self) -> dict[str, Any]:
        return self.issue.to_dict()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EMBED_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/embed"
EMBED_MODEL = os.environ.get("LANCE_EMBED_MODEL", "nomic-embed-text")

# Regex patterns that disqualify an entity/tag — paths, key=value, special chars, etc
_TAG_REJECT_PATTERNS = [
    re.compile(r'[/\\]'),                     # path separators
    re.compile(r'^~'),                        # starts with ~
    re.compile(r'^\.'),                       # starts with .
    re.compile(r'%[a-zA-Z]'),                 # %h, %u env-var patterns
    re.compile(r'='),                         # key=value patterns
    re.compile(r'[()[\]{}<>|&$!?*+@#^`~]'), # special chars
    re.compile(r'[^\x00-\x7F]'),              # non-ASCII
    re.compile(r'^[\d_\-.:]+$'),              # pure numbers/symbols
    re.compile(r'\.(py|yaml|yml|toml|json|md|txt|cfg|ini|conf|sh|bash|fish|html|css|js|ts|vue|svelte|go|rs|cpp|c|h|hpp)$', re.IGNORECASE),  # file extensions
]


def _is_valid_tag(tag: str) -> bool:
    """Reject tags that look like paths, key=value, or garbage patterns."""
    if not isinstance(tag, str) or len(tag) < 2:
        return False
    for pat in _TAG_REJECT_PATTERNS:
        if pat.search(tag):
            return False
    return True


# Words that pass _is_valid_tag but are still worthless as tags — French/English
# generic words, adverbs, verbs, noise from code snippet extraction, etc.
_TAG_NOISE_WORDS: set[str] = {
    # French generics
    "toujours", "jamais", "sans", "plus", "rien", "quand", "avant", "peut",
    "chaque", "doit", "chez", "dans", "avec", "cette",
    "couleurs", "contexte", "cible", "solution", "exact", "chercher",
    "envoyer", "ajouter", "fixé", "fixe", "utilise", "connecte", "couvre",
    "contenu", "priorite", "ancien", "nouveaux", "nouveau",
    "discuter", "fonctionne", "accepter", "confirme", "prefere", "evite",
    "adresse", "assigner", "exclure", "prendre",
    "sarcasme", "obligatoire", "applicable", "creer", "simplifie",
    "adapte", "cree", "reecrit", "modes", "recueil",
    "analyse", "plusieurs", "ordre", "sujet", "sujets",
    "pour", "trois", "ouvre", "titre", "pointage",
    "verifier", "prerequis", "matche", "logique",
    "dictionnaire", "raison", "chemin",
    # English generics
    "setup", "clean", "cleanup", "format", "gateway", "worker", "ghost",
    "root", "magic", "pool", "purge", "poll", "mount", "certs",
    "alldata", "register", "frontmatter", "tasks", "works", "types",
    "default", "broad", "deal", "free", "better", "phase", "only",
    "must", "deep", "object", "stop", "file",
    "access", "score", "scoring", "drive", "sync", "links", "liens",
    "query", "search", "profile", "button", "bouton", "boutons",
    "prev", "next", "back", "home", "page", "list", "view",
    "code", "data", "info", "text", "type", "name", "mode",
    "button", "input", "field", "form", "value", "total",
    "menu", "tab", "tag", "tags", "hover", "click",
    "todo", "note", "notes", "book", "file", "files",
    "left", "right", "top", "bottom", "item", "items",
    "select", "delete", "update", "create", "read", "write",
    "main", "base", "side", "open", "close", "show",
    "account", "manage", "change", "added", "removed",
    "server", "client", "local", "remote", "live", "beta",
    "sample", "test", "demo", "prod",
    "source", "target", "input", "output",
    "check", "auto", "manual", "this", "docs", "script",
}


def _select_tags(entities: list[str]) -> list[str]:
    """Pick curated tags from entities — tech keywords, project names, acronyms.
    Filters out generic noise words (French/English adverbs, verbs, generic nouns).
    Returns max 5 tags, prioritizing tech keywords then meaningful names.
    """
    project_re = re.compile(r'^[A-Z][a-z]+[A-Z]')  # Multi-Capital CamelCase
    acro_re = re.compile(r'^[A-Z]{3,}$')            # ALL CAPS acronyms

    scored = []
    for e in entities:
        if len(e) < 3:
            continue
        e_lower = e.lower()
        if e_lower in _TAG_NOISE_WORDS:
            continue

        # Score: tech keywords highest, project names next, acronyms last
        if e_lower in _TECH_KEYWORDS:
            score = 3
        elif project_re.match(e) and len(e) >= 5:
            score = 2
        elif acro_re.match(e):
            score = 2
        else:
            score = 1  # keep it but lower priority

        scored.append((score, e))

    # Sort by score desc, then by length desc (longer = more specific)
    scored.sort(key=lambda x: (-x[0], -len(x[1])))
    return [e for _, e in scored[:5]]


_STOP_ENTITIES: set[str] = {
    "projet", "projets", "game", "games", "plugin", "plugins",
    "dll", "framework", "cache", "build", "config", "tools",
    "tool", "service", "services", "stack", "support", "app",
    "apps", "mod", "mods", "repo", "repos", "git", "github",
    "url", "cli", "gui", "ui", "ux", "system", "custom",
    "install", "compile", "outils", "map", "maps", "path",
    "go", "code", "type", "notes", "usage", "version", "api",
    "site", "bash", "lib", "vue", "description", "overview",
    "true", "init", "max", "session", "status", "false", "liste",
    "recents", "tous", "vite", "pas", "main", "new", "fix",
    "up", "down", "back", "name", "id", "set", "get", "run",
    "use", "make", "port", "local", "default", "base",
}

_TECH_KEYWORDS: set[str] = {
    # Langages
    "python", "rust", "c++", "c++17", "c++20", "c++23", "c", "typescript",
    "javascript", "js", "ts", "golang", "go-lang", "lua", "gdscript",
    "bash", "sh", "shell", "html", "css", "sql", "kotlin", "java",
    # Outils
    "git", "github", "docker", "podman", "kubernetes", "k8s",
    "vscode", "zed", "neovim", "nvim", "vim", "intellij", "clion",
    "obsidian", "notion", "linear", "jira", "notion",
    "alacritty", "kitty", "wezterm", "tmux", "zellij",
    "fish", "bash", "zsh", "nushell",
    "chocolatey", "winget", "brew", "pip", "cargo", "npm", "yarn",
    "gcc", "clang", "llvm", "cmake", "make", "ninja",
    "gdb", "lldb", "valgrind", "perf", "htop", "btop",
    "ripgrep", "rg", "fd", "bat", "eza", "jq", "yq",
    "curl", "wget", "httpie",
    # Frameworks
    "fastapi", "flask", "django", "svelte", "react", "vue",
    "nextjs", "nuxt", "sveltekit", "tailwind", "bootstrap",
    "pytorch", "tensorflow", "jax", "transformers",
    "bepinex", "harmony", "mono", "net48", "net472",
    # OS
    "linux", "arch", "cachyos", "ubuntu", "debian", "fedora",
    "windows", "macos", "wsl", "wayland", "x11",
    "niri", "hyprland", "kde", "gnome", "plasma", "i3", "sway",
    "systemd", "docker", "podman",
    # Hardware
    "amd", "nvidia", "intel", "ryzen", "threadripper",
    "7900xt", "7900xtx", "4090", "5090", "5070",
    "steamdeck", "rogally", "legion",
    # Media / Gaming
    "beyondallreason", "bar", "bo3", "blackops3",
    "godot", "unity", "unreal", "unrealengine",
    "steam", "proton", "wine", "lutris", "heroic",
    "obs", "ffmpeg", "ffprobe", "gimp", "kdenlive",
    "spotify", "deezer", "tidal", "yt-dlp", "youtube",
    # AI / LLM
    "ollama", "lancedb", "hermes", "openai", "anthropic",
    "claude", "gpt", "llama", "deepseek", "mistral",
    "qwen", "gemma", "phi", "nomic", "nomic-embed-text",
    "gguf", "transformers", "sentence-transformers",
    # Cloud / Hosting
    "hetzner", "aws", "gcp", "azure", "ovh",
    "cloudflare", "vercel", "netlify",
    # Réseau
    "nginx", "caddy", "apache", "traefik",
    "cloudflare", "tailscale", "wireguard", "openvpn",
    "pi-hole", "adguard",
}

# Valid categories (old + new)
VALID_CATEGORIES = {
    "user_pref", "project", "tech", "correction", "fact",
    "decision", "insight", "reference", "pattern", "question",
}

VALID_RELATION_TYPES = {
    "part_of", "depends", "requires", "runs_on", "connects_to",
    "uses", "extends", "supersedes", "invalidates", "contradicts",
}

CONFLICT_REGISTRY_RESOLVED_LIMIT = max(
    0, int(os.environ.get("LANCEDB_CONFLICT_RESOLVED_LIMIT", "1000"))
)


def canonical_subject(content: str) -> str:
    """Return the normalized Domain:Subject key from a memory body."""
    first = (content or "").strip().split(None, 1)[0] if (content or "").strip() else ""
    return first.rstrip(":,.;").lower() if ":" in first else ""


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _conflict_key(memory_a_id: str, memory_b_id: str, claim_key: str) -> str:
    """Return a stable logical key for a memory pair and normalized claim key."""
    import hashlib

    pair = sorted((str(memory_a_id), str(memory_b_id)))
    raw = "\x1f".join((pair[0], pair[1], str(claim_key).casefold()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def route_search_mode(query: str) -> str:
    """Choose a local retrieval strategy using cheap deterministic rules."""
    text = (query or "").strip()
    lower = text.lower()
    lexical_markers = ("exact", "verbatim", "littéral", "literal")
    if (
        (len(text) >= 2 and text[0] == text[-1] == '"')
        or re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", lower)
        or re.search(r"(?:^|\s)(?:~/|/)[^\s]+", text)
        or any(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", lower) for marker in lexical_markers)
    ):
        return "lexical"
    graph_markers = (
        "related", "relation", "connected", "connects", "linked", "links",
        "depends", "dependency", "part of", "requires", "uses",
        "relié", "relie", "relation", "connecté", "connecte", "dépend",
        "depend", "dépendance", "dependance", "nécessite", "necessite",
    )
    if any(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", lower) for marker in graph_markers):
        return "graph"
    return "hybrid"


_CLAIM_PATTERN = re.compile(
    r"(?<![\w.-])([A-Za-z][A-Za-z0-9_.-]*)\s*=\s*"
    r"(\"[^\"]*\"|'[^']*'|[^\s]+)"
)


def extract_claims(content: str) -> dict[str, str]:
    """Extract explicit key=value claims for conservative conflict checks."""
    from decimal import Decimal, InvalidOperation
    import unicodedata

    claims = {}
    for key, raw_value in _CLAIM_PATTERN.findall(content or ""):
        key = unicodedata.normalize("NFKC", key).casefold()
        if key == "tier":
            continue
        value = raw_value.strip("\"' ,.;")
        value = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
        if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", value):
            try:
                number = Decimal(value)
                if number.is_finite():
                    value = format(number.normalize(), "f")
                    if value == "-0":
                        value = "0"
            except InvalidOperation:
                pass
        if value:
            claims[key] = value
    return claims

# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------


def _clean_entity(e: str) -> str:
    """Normalize entity for comparison."""
    return e.strip().rstrip(".,;:!?)").lower()


def extract_entities(text: str) -> list[str]:
    """Extract entities from text using keyword + regex patterns."""
    if not text:
        return []
    entities: set[str] = set()
    lower = text.lower()

    # 1. Tech keywords — skip ultra-short (≤2 chars) to avoid substring noise
    for kw in _TECH_KEYWORDS:
        if len(kw) < 3:
            continue
        if kw in lower:
            entities.add(kw)

    # 2. CamelCase / TitleCase words (skip domain prefix and stop list)
    words = text.split()
    start = 1 if words and ":" in words[0] else 0
    for w in words[start:]:
        clean = _clean_entity(w)
        if not clean or len(clean) < 3:
            continue
        if clean in _STOP_ENTITIES:
            continue
        if re.match(r"^[A-Z][a-z]+[A-Z]", w) or re.match(r"^[a-z]+[A-Z]", w):
            entities.add(clean)
        if re.match(r"^[A-Z][a-z]{2,}", w) and len(w) >= 4:
            entities.add(clean)

    # 3. Numbers with units or version suffixes
    for token in lower.split():
        if re.match(r"^\d+(\.\d+)*[a-z]?$", token) and 3 <= len(token) <= 10:
            entities.add(token)

    # 4. Words in ALL CAPS (acronyms) — 3+ chars
    for w in words:
        clean = _clean_entity(w)
        if len(clean) >= 3 and clean.isalpha() and clean.isupper() and clean not in _STOP_ENTITIES:
            entities.add(clean)

    # Filter out tags that look like paths or garbage before returning
    return sorted(e for e in entities if _is_valid_tag(e))


# ---------------------------------------------------------------------------
# LanceDBStore
# ---------------------------------------------------------------------------


class LanceDBStore:
    """Vector memory store using LanceDB with Ollama embeddings."""

    def __init__(self, db_path: str | Path):
        import lancedb
        self._path = Path(db_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self._path))
        self._table_name = "memories"
        self._table: "lancedb.table.LanceTable" = self._init_table()
        self._conflicts_schema_checked = False
        self._db_size = self._compute_db_size()

    @property
    def db_size(self) -> int:
        return self._compute_db_size()

    # -----------------------------------------------------------------------
    # Schema
    # -----------------------------------------------------------------------

    def _get_schema(self):
        import pyarrow as pa
        return pa.schema([
            pa.field("id", pa.string()),
            pa.field("content", pa.string()),
            pa.field("category", pa.string()),
            pa.field("entities", pa.string()),
            pa.field("links", pa.string()),
            pa.field("relations", pa.string()),
            pa.field("tags", pa.string()),          # NEW: libre tags (JSON array)
            pa.field("quality", pa.float64()),       # NEW: 0-1 quality score
            pa.field("type", pa.string()),           # NEW: sub-type (granular)
            pa.field("source", pa.string()),
            pa.field("session_id", pa.string()),
            pa.field("user_id", pa.string()),
            pa.field("created_at", pa.float64()),
            pa.field("updated_at", pa.float64()),
            pa.field("access_count", pa.int64()),
            pa.field("accessed_at", pa.float64()),
            pa.field("vector", pa.list_(pa.float32(), 768)),
        ])

    def _init_table(self):
        try:
            tbl = self._db.open_table(self._table_name)
            if "tags" in tbl.schema.names:
                self._ensure_fts_index(tbl)
                return tbl
            logger.warning("Table has old schema — missing tags/quality/type")
            return tbl
        except Exception:
            tbl = self._db.create_table(self._table_name, schema=self._get_schema())
            self._ensure_fts_index(tbl)
            return tbl

    def _ensure_fts_index(self, tbl):
        """Create FTS (BM25) index on content column for hybrid search.
        Idempotent via replace=True — safe to call on every init."""
        try:
            try:
                from lancedb.index import FTS
                tbl.create_index("content", config=FTS(), replace=True)
            except (ImportError, TypeError, AttributeError):
                tbl.create_fts_index("content", replace=True)
            logger.info("FTS index ready on content column")
        except Exception as e:
            logger.debug("FTS index creation skipped: %s", e)

    def _compute_db_size(self) -> int:
        try:
            total = 0
            for f in self._path.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
            return total
        except Exception:
            return 0

    # -----------------------------------------------------------------------
    # Relations (typed edges)
    # -----------------------------------------------------------------------

    def _fresh(self) -> None:
        """Re-sync the table handle to the latest version (MVCC).

        Concurrent writers (gateway sessions, kanban workers, viz) each hold a
        table handle pinned to the version it opened. A long-lived handle reads
        a stale snapshot and commits (update/delete) that clobber newer writes.
        Call before every read/write that goes through self._table."""
        try:
            self._table.checkout_latest()
        except Exception:
            self._table = self._init_table()

    def _parse_relations(self, content: str) -> list[dict]:
        """Parse ::relations:: block from content. Returns list of {type, target}.
        Uses the LAST occurrence of ::relations:: (usually after [Tier=N]) to avoid
        matching ::relations:: used as a word in content text.
        """
        import re
        matches = list(re.finditer(r'::relations::\s*', content))
        if not matches:
            return []
        last = matches[-1]
        rest = content[last.end():]
        endline = rest.find('\n')
        if endline >= 0:
            rel_line = rest[:endline]
        else:
            rel_line = rest
        rel_line = rel_line.strip()
        relations = []
        for part in rel_line.split('|'):
            part = part.strip()
            if '=' in part:
                rtype, target = part.split('=', 1)
                rtype = rtype.strip()
                target = target.strip()
                if rtype and target:
                    relations.append({"type": rtype, "target": target})
        return relations

    def _ensure_edges_table(self):
        """Open the edge table and add target_id to legacy schemas."""
        import pyarrow as pa
        try:
            table = self._db.open_table("memory_edges")
            try:
                table.checkout_latest()
            except Exception:
                pass
            if "target_id" not in table.schema.names:
                table.add_columns(pa.field("target_id", pa.string()))
                table.checkout_latest()
            if "edge_key" not in table.schema.names:
                table.add_columns(pa.field("edge_key", pa.string()))
                table.checkout_latest()
            return table
        except Exception:
            schema = pa.schema([
                pa.field("edge_key", pa.string()),
                pa.field("source_id", pa.string()),
                pa.field("relation_type", pa.string()),
                pa.field("target_id", pa.string()),
                pa.field("target_label", pa.string()),
                pa.field("created_at", pa.float64()),
            ])
            return self._db.create_table("memory_edges", schema=schema)

    def _resolve_relation_target(self, relation: dict) -> tuple[str, str]:
        """Resolve a relation target without guessing between duplicate subjects."""
        target_id = str(relation.get("target_id") or "").strip()
        target_label = str(relation.get("target") or relation.get("target_label") or "").strip()
        memories = self._get_all_raw()
        by_id = {str(memory["id"]): memory for memory in memories}

        if target_id and target_id in by_id:
            if not target_label:
                target_label = (by_id[target_id].get("content") or "").split(None, 1)[0]
            return target_id, target_label

        wanted = canonical_subject(target_label) or target_label.rstrip(":,.;").lower()
        matches = []
        for memory in memories:
            memory_key = canonical_subject(memory.get("content", ""))
            exact_match = wanted and memory_key == wanted
            short_match = wanted and ":" not in wanted and memory_key.partition(":")[2] == wanted
            if exact_match or short_match:
                matches.append(str(memory["id"]))
        return (matches[0], target_label) if len(matches) == 1 else ("", target_label)

    def _replace_relations(self, mem_id: str, relations: list[dict]) -> list[dict]:
        """Atomically replace outgoing relations and verify the committed state."""
        import hashlib
        import pyarrow as pa

        table = self._ensure_edges_table()
        normalized = []
        now = time.time()
        rows = []
        seen = set()
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            relation_type = str(relation.get("type") or "").strip().lower()
            if relation_type not in VALID_RELATION_TYPES:
                continue
            target_id, target_label = self._resolve_relation_target(relation)
            if not target_id and not target_label:
                continue
            logical_key = (relation_type, target_id, target_label)
            if logical_key in seen:
                continue
            seen.add(logical_key)
            normalized_relation = {
                "type": relation_type,
                "target_id": target_id,
                "target": target_label,
            }
            normalized.append(normalized_relation)
            rows.append({
                "edge_key": hashlib.sha256(
                    "\x1f".join((mem_id, relation_type, target_id, target_label)).encode("utf-8")
                ).hexdigest(),
                "source_id": mem_id,
                "relation_type": relation_type,
                "target_id": target_id,
                "target_label": target_label,
                "created_at": now,
            })

        source = pa.Table.from_pylist(rows, schema=table.schema)
        (
            table.merge_insert("edge_key")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .when_not_matched_by_source_delete(f"source_id = '{mem_id}'")
            .execute(source)
        )
        table.checkout_latest()
        committed = table.search().where(f"source_id = '{mem_id}'").to_list()
        committed_keys = {
            (
                str(row.get("relation_type") or ""),
                str(row.get("target_id") or ""),
                str(row.get("target_label") or ""),
            )
            for row in committed
        }
        if len(committed) != len(rows) or committed_keys != seen:
            raise RuntimeError(f"Relation replacement postcondition failed for {mem_id}")
        return normalized

    def _cleanup_edges_for_memory(self, mem_id: str) -> bool:
        """Atomically remove incoming/outgoing edges and verify the result."""
        try:
            table = self._ensure_edges_table()
            table.delete(f"source_id = '{mem_id}' OR target_id = '{mem_id}'")
            table.checkout_latest()
            remaining = table.search().where(
                f"source_id = '{mem_id}' OR target_id = '{mem_id}'"
            ).limit(1).to_list()
            if remaining:
                logger.error("Relation cleanup postcondition failed for %s", mem_id)
                return False
            return True
        except Exception as error:
            logger.warning("Failed to clean relations for %s: %s", mem_id, error)
            return False

    def _write_relations(self, mem_id: str, content: str):
        """Compatibility wrapper for relation blocks embedded in content."""
        return self._replace_relations(mem_id, self._parse_relations(content))

    def _write_relations_list(self, mem_id: str, relations: list[dict]):
        """Replace typed edges from a list of relation dictionaries."""
        return self._replace_relations(mem_id, relations)

    def get_typed_edges(self, include_unresolved: bool = False) -> list[dict]:
        """Return typed edges with a concrete ``to`` ID when resolved."""
        try:
            table = self._ensure_edges_table()
            data = table.to_arrow().to_pydict()
            edges = []
            for i in range(len(data.get("source_id", []))):
                target_id = str((data.get("target_id") or [""])[i] or "")
                if not include_unresolved and not target_id:
                    continue
                edges.append({
                    "from": str(data["source_id"][i]),
                    "to": target_id,
                    "relation_type": str(data["relation_type"][i]),
                    "target_label": str(data["target_label"][i]),
                    "created_at": float(data["created_at"][i]),
                })
            return edges
        except Exception as error:
            logger.warning("Failed to read typed relations: %s", error)
            return []

    def _conflict_schema(self):
        import pyarrow as pa
        return pa.schema([
            pa.field("id", pa.string()),
            pa.field("memory_a_id", pa.string()),
            pa.field("memory_b_id", pa.string()),
            pa.field("subject", pa.string()),
            pa.field("claim_key", pa.string()),
            pa.field("value_a", pa.string()),
            pa.field("value_b", pa.string()),
            pa.field("status", pa.string()),
            pa.field("confidence", pa.float64()),
            pa.field("created_at", pa.float64()),
            pa.field("resolved_at", pa.float64()),
            pa.field("resolution_type", pa.string()),
            pa.field("resolution_note", pa.string()),
            pa.field("resolved_by", pa.string()),
            pa.field("conflict_key", pa.string()),
            pa.field("archived_at", pa.float64()),
        ])

    def _ensure_conflicts_archive_table(self):
        try:
            table = self._db.open_table("memory_conflicts_archive")
            table.checkout_latest()
            return table
        except Exception:
            return self._db.create_table(
                "memory_conflicts_archive", schema=self._conflict_schema()
            )

    @staticmethod
    def _conflict_row_priority(row: dict) -> tuple[int, float, str]:
        human_resolution = (
            row.get("status") == "resolved"
            and row.get("resolution_type") != "auto"
        )
        priority = 3 if human_resolution else 2 if row.get("status") == "open" else 1
        return priority, float(row.get("created_at") or 0.0), str(row.get("id") or "")

    def _normalize_conflict_registry(self, table) -> None:
        """Backfill logical keys and consolidate legacy duplicate records."""
        rows = table.to_arrow().to_pylist()
        for row in rows:
            logical_key = _conflict_key(
                row.get("memory_a_id", ""),
                row.get("memory_b_id", ""),
                row.get("claim_key", ""),
            )
            updates = {}
            if row.get("conflict_key") != logical_key:
                updates["conflict_key"] = logical_key
            if row.get("archived_at") is None:
                updates["archived_at"] = 0.0
            if updates:
                table.update(f"id = {_sql_literal(row['id'])}", updates)

        table.checkout_latest()
        grouped = {}
        for row in table.to_arrow().to_pylist():
            grouped.setdefault(row["conflict_key"], []).append(row)
        for duplicates in grouped.values():
            if len(duplicates) < 2:
                continue
            keep = max(duplicates, key=self._conflict_row_priority)
            for duplicate in duplicates:
                if duplicate["id"] != keep["id"]:
                    table.delete(f"id = {_sql_literal(duplicate['id'])}")

    def _archive_resolved_conflicts(self, table) -> None:
        """Keep the hot resolved ledger bounded while retaining cold audit rows."""
        import pyarrow as pa

        table.checkout_latest()
        resolved = [
            row for row in table.to_arrow().to_pylist()
            if row.get("status") == "resolved"
        ]
        overflow = len(resolved) - CONFLICT_REGISTRY_RESOLVED_LIMIT
        if overflow <= 0:
            return
        resolved.sort(key=lambda row: (
            float(row.get("resolved_at") or row.get("created_at") or 0.0),
            str(row.get("id") or ""),
        ))
        to_archive = []
        archived_at = time.time()
        for row in resolved[:overflow]:
            archived = dict(row)
            archived["archived_at"] = archived_at
            to_archive.append(archived)

        archive = self._ensure_conflicts_archive_table()
        source = pa.Table.from_pylist(to_archive, schema=archive.schema)
        (
            archive.merge_insert("conflict_key")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(source)
        )
        for row in to_archive:
            table.delete(f"id = {_sql_literal(row['id'])}")

    def _ensure_conflicts_table(self):
        """Create, migrate, and normalize the deterministic contradiction ledger."""
        import pyarrow as pa
        try:
            table = self._db.open_table("memory_conflicts")
            try:
                table.checkout_latest()
            except Exception:
                pass
            audit_fields = (
                pa.field("resolution_type", pa.string()),
                pa.field("resolution_note", pa.string()),
                pa.field("resolved_by", pa.string()),
                pa.field("conflict_key", pa.string()),
                pa.field("archived_at", pa.float64()),
            )
            for field in audit_fields:
                if field.name not in table.schema.names:
                    table.add_columns(field)
                    table.checkout_latest()
        except Exception:
            table = self._db.create_table(
                "memory_conflicts", schema=self._conflict_schema()
            )
        if not self._conflicts_schema_checked:
            self._normalize_conflict_registry(table)
            self._archive_resolved_conflicts(table)
            self._conflicts_schema_checked = True
        return table

    def get_all_conflict_records(self, include_archived: bool = False) -> list[dict]:
        """Return raw conflict records for lossless export and maintenance."""
        active = self._ensure_conflicts_table().to_arrow().to_pylist()
        rows = list(active)
        if include_archived:
            rows.extend(self._ensure_conflicts_archive_table().to_arrow().to_pylist())
        unique = {}
        for row in rows:
            key = row.get("conflict_key") or _conflict_key(
                row.get("memory_a_id", ""), row.get("memory_b_id", ""), row.get("claim_key", "")
            )
            previous = unique.get(key)
            if previous is None or self._conflict_row_priority(row) > self._conflict_row_priority(previous):
                unique[key] = dict(row)
        return sorted(
            unique.values(),
            key=lambda row: (float(row.get("created_at") or 0.0), str(row.get("id") or "")),
        )

    def import_conflict_records(self, records: list[dict]) -> int:
        """Restore exported conflict audit rows using their logical unique key."""
        from uuid import uuid4
        import pyarrow as pa

        memory_ids = {str(row["id"]) for row in self._get_all_raw()}
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                continue
            memory_a_id = str(record.get("memory_a_id") or "")
            memory_b_id = str(record.get("memory_b_id") or "")
            claim_key = str(record.get("claim_key") or "").casefold()
            if not claim_key or memory_a_id not in memory_ids or memory_b_id not in memory_ids:
                continue
            status = record.get("status") if record.get("status") in {"open", "resolved"} else "open"
            normalized.append({
                "id": str(record.get("id") or str(uuid4())[:12]),
                "memory_a_id": memory_a_id,
                "memory_b_id": memory_b_id,
                "subject": str(record.get("subject") or ""),
                "claim_key": claim_key,
                "value_a": str(record.get("value_a") or ""),
                "value_b": str(record.get("value_b") or ""),
                "status": status,
                "confidence": float(record.get("confidence") or 1.0),
                "created_at": float(record.get("created_at") or time.time()),
                "resolved_at": float(record.get("resolved_at") or 0.0),
                "resolution_type": str(record.get("resolution_type") or ""),
                "resolution_note": str(record.get("resolution_note") or ""),
                "resolved_by": str(record.get("resolved_by") or ""),
                "conflict_key": _conflict_key(memory_a_id, memory_b_id, claim_key),
                "archived_at": 0.0,
            })
        if not normalized:
            return 0
        table = self._ensure_conflicts_table()
        source = pa.Table.from_pylist(normalized, schema=table.schema)
        (
            table.merge_insert("conflict_key")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(source)
        )
        self._archive_resolved_conflicts(table)
        return len(normalized)

    def detect_conflicts_for(self, memory_id: str) -> list[dict]:
        """Record explicit same-subject key=value contradictions, without mutation."""
        from uuid import uuid4
        import pyarrow as pa

        current = self._get_by_id_raw(memory_id)
        if not current:
            return []
        subject = canonical_subject(current.get("content", ""))
        claims = extract_claims(current.get("content", ""))
        if not subject or not claims:
            return []

        relevant_categories = {"decision", "correction", "project", "user_pref", "tech", "fact"}
        if current.get("category") not in relevant_categories:
            return []

        table = self._ensure_conflicts_table()
        existing_rows = self.get_all_conflict_records(include_archived=True)
        existing_by_key = {
            row.get("conflict_key") or _conflict_key(
                row["memory_a_id"], row["memory_b_id"], row["claim_key"]
            ): row
            for row in existing_rows
        }
        created = []
        to_insert = []
        now = time.time()
        for other in self._get_all_raw():
            other_id = str(other["id"])
            if other_id == memory_id or other.get("category") not in relevant_categories:
                continue
            if canonical_subject(other.get("content", "")) != subject:
                continue
            other_claims = extract_claims(other.get("content", ""))
            for claim_key in sorted(set(claims) & set(other_claims)):
                if claims[claim_key] == other_claims[claim_key]:
                    continue
                dedupe_key = _conflict_key(memory_id, other_id, claim_key)
                existing = existing_by_key.get(dedupe_key)
                if existing:
                    if existing.get("status") == "open":
                        continue
                    # A legacy resolved row has no resolution_type and is treated
                    # as a human decision. Only automatic claim-change closures
                    # may be reopened when the contradiction reappears.
                    if existing.get("resolution_type") != "auto":
                        continue
                    if existing["memory_a_id"] == other_id:
                        values = {
                            "value_a": other_claims[claim_key],
                            "value_b": claims[claim_key],
                        }
                    else:
                        values = {
                            "value_a": claims[claim_key],
                            "value_b": other_claims[claim_key],
                        }
                    values.update({
                        "status": "open",
                        "created_at": now,
                        "resolved_at": 0.0,
                        "resolution_type": "",
                        "resolution_note": "",
                        "resolved_by": "",
                        "conflict_key": dedupe_key,
                        "archived_at": 0.0,
                    })
                    if float(existing.get("archived_at") or 0.0) > 0:
                        restored = dict(existing)
                        restored.update(values)
                        source = pa.Table.from_pylist([restored], schema=table.schema)
                        (
                            table.merge_insert("conflict_key")
                            .when_matched_update_all()
                            .when_not_matched_insert_all()
                            .execute(source)
                        )
                        archive = self._ensure_conflicts_archive_table()
                        archive.delete(f"id = {_sql_literal(existing['id'])}")
                    else:
                        table.update(f"id = {_sql_literal(existing['id'])}", values)
                    existing.update(values)
                    created.append(dict(existing))
                    continue
                row = {
                    "id": str(uuid4())[:12],
                    "memory_a_id": other_id,
                    "memory_b_id": memory_id,
                    "subject": subject,
                    "claim_key": claim_key,
                    "value_a": other_claims[claim_key],
                    "value_b": claims[claim_key],
                    "status": "open",
                    "confidence": 1.0,
                    "created_at": now,
                    "resolved_at": 0.0,
                    "resolution_type": "",
                    "resolution_note": "",
                    "resolved_by": "",
                    "conflict_key": dedupe_key,
                    "archived_at": 0.0,
                }
                created.append(row)
                to_insert.append(row)
                existing_by_key[dedupe_key] = row
        if to_insert:
            source = pa.Table.from_pylist(to_insert, schema=table.schema)
            (
                table.merge_insert("conflict_key")
                .when_not_matched_insert_all()
                .execute(source)
            )
        return created

    def get_conflicts(self, status: str = "", limit: int = 100,
                      memory_id: str = "") -> list[dict]:
        """List contradiction records with current memory content for inspection."""
        if status == "archived":
            rows = [
                row for row in self.get_all_conflict_records(include_archived=True)
                if float(row.get("archived_at") or 0.0) > 0
            ]
        else:
            table = self._ensure_conflicts_table()
            rows = table.to_arrow().to_pylist()
        memories = {str(row["id"]): row for row in self._get_all_raw()}
        result = []
        for row in rows:
            if status and status != "archived" and row.get("status") != status:
                continue
            if memory_id and memory_id not in {row.get("memory_a_id"), row.get("memory_b_id")}:
                continue
            item = dict(row)
            item["memory_a_content"] = (memories.get(item["memory_a_id"], {}).get("content") or "")
            item["memory_b_content"] = (memories.get(item["memory_b_id"], {}).get("content") or "")
            result.append(item)
        result.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return result[:max(1, min(int(limit), 500))]

    def _close_conflicts_for_memory(self, memory_id: str,
                                    reason: str = "claims changed") -> bool:
        """Close open conflicts before a memory's claims are rewritten."""
        try:
            table = self._ensure_conflicts_table()
            table.update(
                f"(memory_a_id = '{memory_id}' OR memory_b_id = '{memory_id}') AND status = 'open'",
                {
                    "status": "resolved",
                    "resolved_at": time.time(),
                    "resolution_type": "auto",
                    "resolution_note": reason,
                    "resolved_by": "system",
                },
            )
            self._archive_resolved_conflicts(table)
            return True
        except Exception as error:
            logger.warning("Failed to close conflicts for %s: %s", memory_id, error)
            return False

    def resolve_conflict(self, conflict_id: str, resolution_note: str,
                         resolved_by: str = "user") -> bool:
        """Resolve an open conflict while preserving an explicit audit trail."""
        if not conflict_id or not resolution_note.strip() or not resolved_by.strip():
            return False
        table = self._ensure_conflicts_table()
        row = next(
            (item for item in table.to_arrow().to_pylist() if item.get("id") == conflict_id),
            None,
        )
        if not row or row.get("status") != "open":
            return False
        table.update(
            f"id = '{row['id']}' AND status = 'open'",
            {
                "status": "resolved",
                "resolved_at": time.time(),
                "resolution_type": "human",
                "resolution_note": resolution_note.strip(),
                "resolved_by": resolved_by.strip(),
            },
        )
        table.checkout_latest()
        updated = next(
            (item for item in table.to_arrow().to_pylist() if item.get("id") == conflict_id),
            None,
        )
        success = bool(
            updated
            and updated.get("status") == "resolved"
            and updated.get("resolution_type") == "human"
            and updated.get("resolution_note") == resolution_note.strip()
            and updated.get("resolved_by") == resolved_by.strip()
        )
        if success:
            self._archive_resolved_conflicts(table)
        return success

    # -----------------------------------------------------------------------
    # Embedding
    # -----------------------------------------------------------------------

    def _embed(self, text: str) -> np.ndarray:
        """Embed text via Ollama. Returns normalized 768-dim vector."""
        import httpx
        if len(text) > 4096:
            text = text[:4096]
        try:
            resp = httpx.post(
                EMBED_URL,
                json={"model": EMBED_MODEL, "input": [text], "keep_alive": "30s"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            embedding = data.get("embeddings", [])
            if not embedding:
                raise ValueError("Empty embedding response")
            vec = np.array(embedding[0], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0.001:
                vec = vec / norm
            return vec
        except Exception as e:
            logger.warning("Embedding failed: %s — returning zero vector", e)
            return np.zeros(768, dtype=np.float32)

    def _require_embedding(self, text: str) -> np.ndarray:
        """Return a usable embedding or fail before any memory row is written."""
        vector = np.asarray(self._embed(text), dtype=np.float32)
        if vector.shape != (768,) or not np.isfinite(vector).all():
            raise MemoryEmbeddingError(f"invalid vector shape or values: {vector.shape}")
        norm = float(np.linalg.norm(vector))
        if norm < 0.001:
            raise MemoryEmbeddingError("zero vector")
        return vector / norm

    # -----------------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------------

    def _strip_relations_from_content(self, content: str) -> tuple[str, str]:
        """Separate ::relations:: block from content.
        Returns (clean_content, relations_json).
        """
        import re
        matches = list(re.finditer(r'\n::relations::\s*(.+?)(?:\n|$)', content))
        if not matches:
            return content.strip(), '[]'
        last = matches[-1]
        clean = content[:last.start()].strip()
        rel_line = last.group(1).strip()
        relations = []
        for part in rel_line.split('|'):
            part = part.strip()
            if '=' in part:
                rtype, target = part.split('=', 1)
                rtype = rtype.strip()
                target = target.strip()
                if rtype and target:
                    relations.append({"type": rtype, "target": target})
        return clean, json.dumps(relations)

    @staticmethod
    def _contract_error(
        code: str,
        field: str,
        message: str,
        received: Any,
        expected: Any,
    ) -> MemoryContractError:
        return MemoryContractError(ContractIssue(
            code=code,
            field=field,
            message=message,
            received=received,
            expected=expected,
        ))

    @staticmethod
    def _relation_lists_match(
        requested: tuple | list,
        stored: list[dict],
    ) -> bool:
        """Compare requested relations with their persisted, resolved form."""
        if len(requested) != len(stored):
            return False
        remaining = list(stored)
        for relation in requested:
            candidate = relation.to_dict() if hasattr(relation, "to_dict") else relation
            relation_type = str(candidate.get("type") or "")
            target_id = str(candidate.get("target_id") or "").strip()
            target = str(candidate.get("target") or "").strip().casefold()
            match_index = next((
                index
                for index, persisted in enumerate(remaining)
                if str(persisted.get("type") or "") == relation_type
                and (
                    (target_id and str(persisted.get("target_id") or "").strip() == target_id)
                    or (target and str(persisted.get("target") or "").strip().casefold() == target)
                )
            ), None)
            if match_index is None:
                return False
            remaining.pop(match_index)
        return not remaining

    def _preflight_memory_write(
        self,
        memory: MemoryWrite,
        *,
        exclude_id: str = "",
    ) -> tuple[dict | None, list[dict], list[dict]]:
        """Find exact, same-subject, and conflicting rows without embedding."""
        canonical_content = render_content(memory)
        subject = canonical_subject(canonical_content)
        claims = extract_claims(canonical_content)
        exact = None
        same_subject = []
        conflicts = []

        for row in self._get_all_raw():
            if str(row.get("id") or "") == exclude_id:
                continue
            if canonical_subject(str(row.get("content") or "")) != subject:
                continue
            same_subject.append(row)

            stored_relations = row.get("relations")
            if not isinstance(stored_relations, list):
                stored_relations = []
            try:
                stored_memory = parse_content(
                    row.get("content"),
                    category=str(row.get("category") or "fact"),
                    relations=[relation.to_dict() for relation in memory.relations],
                )
            except MemoryContractError:
                stored_memory = None
            if (
                stored_memory is not None
                and self._relation_lists_match(memory.relations, stored_relations)
                and memory_fingerprint(stored_memory) == memory_fingerprint(memory)
            ):
                exact = row
                break

            other_claims = extract_claims(str(row.get("content") or ""))
            for key in sorted(set(claims) & set(other_claims)):
                if claims[key] != other_claims[key]:
                    conflicts.append({
                        "memory_id": str(row.get("id") or ""),
                        "claim_key": key,
                        "existing": other_claims[key],
                        "received": claims[key],
                    })
        return exact, same_subject, conflicts

    def _insert_memory(
        self,
        *,
        content: str,
        category: str,
        source: str = "",
        session_id: str = "",
        user_id: str = "",
        tags: list[str] | None = None,
        quality: float | None = None,
        type_: str | None = None,
        relations: list[dict] | None = None,
        warnings: list[str] | None = None,
    ) -> str:
        """Persist already validated content. Callers must run preflight first."""
        self._fresh()
        from uuid import uuid4
        mem_id = str(uuid4())[:12]

        clean_content = content.strip()
        relations_list = relations or []
        relations_json = json.dumps(relations_list)

        vector = self._require_embedding(clean_content)
        entities = extract_entities(clean_content)
        now = time.time()

        mem_type = type_ or category

        # Auto-tags: curated from entities — tech keywords, project names, acronyms only
        safe_entities = [e for e in entities if _is_valid_tag(e)]
        auto_tags = tags if tags is not None else _select_tags(safe_entities)

        # Auto-quality: start at 0.5, adjust based on context
        auto_quality = quality if quality is not None else 0.5

        self._table.add([{
            "id": mem_id,
            "content": clean_content,
            "category": category,
            "entities": json.dumps(entities),
            "links": json.dumps([]),
            "relations": relations_json,
            "tags": json.dumps(auto_tags),
            "quality": auto_quality,
            "type": mem_type,
            "vector": vector.tolist(),
            "source": source,
            "session_id": session_id,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
            "access_count": 0,
            "accessed_at": now,
        }])

        # Build links for the new memory
        self._rebuild_links_for(mem_id)

        # Replace typed relations and persist their resolved IDs in the memory row.
        normalized_relations = self._write_relations_list(mem_id, relations_list)
        self._table.update(
            f"id = '{mem_id}'",
            {"relations": json.dumps(normalized_relations)},
        )

        # Conservative local contradiction check. This never mutates memories.
        try:
            self.detect_conflicts_for(mem_id)
        except Exception as error:
            warning = f"Conflict detection failed after memory write: {error}"
            logger.warning("%s", warning)
            if warnings is not None:
                warnings.append(warning)

        self._update_db_size()

        return mem_id

    def add_memory(
        self,
        memory: MemoryWrite,
        *,
        source: str = "",
        session_id: str = "",
        user_id: str = "",
        tags: list[str] | None = None,
        quality: float | None = None,
        type_: str | None = None,
    ) -> dict[str, Any]:
        """Strict structured write with idempotency and subject preflight."""
        if not isinstance(memory, MemoryWrite):
            raise self._contract_error(
                "invalid_type", "request", "add_memory requires MemoryWrite",
                type(memory).__name__, "MemoryWrite",
            )
        memory = MemoryWrite.from_mapping({
            "domain": memory.domain,
            "subject": memory.subject,
            "facts": list(memory.facts),
            "tier": memory.tier,
            "category": memory.category,
            "relations": [relation.to_dict() for relation in memory.relations],
            "write_mode": memory.write_mode,
        })
        self._fresh()
        canonical_content = render_content(memory)
        warning_dicts = [warning.to_dict() for warning in contract_warnings(memory)]
        exact, same_subject, conflicts = self._preflight_memory_write(memory)

        if exact is not None:
            return {
                "success": True,
                "status": "idempotent",
                "memory_id": str(exact["id"]),
                "canonical_content": canonical_content,
                "warnings": warning_dicts,
            }

        if same_subject and memory.write_mode == "create":
            existing = same_subject[0]
            if conflicts:
                raise self._contract_error(
                    "conflicting_claims",
                    "facts",
                    "same-subject write conflicts with existing explicit claims",
                    conflicts,
                    "resolve the conflict or explicitly update the existing memory",
                )
            return {
                "success": False,
                "status": "update_suggested",
                "memory_id": str(existing["id"]),
                "existing_content": str(existing.get("content") or ""),
                "canonical_content": canonical_content,
                "warnings": warning_dicts,
            }

        if same_subject and memory.write_mode == "upsert_subject":
            if len(same_subject) != 1:
                raise self._contract_error(
                    "ambiguous_subject",
                    "subject",
                    "subject-level upsert requires exactly one existing memory",
                    [str(row.get("id") or "") for row in same_subject],
                    "one existing memory ID; use update_memory when multiple rows exist",
                )
            existing = same_subject[0]
            result = self.update_memory(MemoryPatch.from_mapping({
                "memory_id": str(existing["id"]),
                "domain": memory.domain,
                "subject": memory.subject,
                "facts": list(memory.facts),
                "tier": memory.tier,
                "category": memory.category,
                "relations": [relation.to_dict() for relation in memory.relations],
            }), _allow_claim_replacement=True)
            result["warnings"] = warning_dicts
            return result

        memory_id = self._insert_memory(
            content=canonical_content,
            category=memory.category,
            source=source,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            quality=quality,
            type_=type_,
            relations=[relation.to_dict() for relation in memory.relations],
        )
        return {
            "success": True,
            "status": "created",
            "memory_id": memory_id,
            "canonical_content": canonical_content,
            "warnings": warning_dicts,
        }

    def add(
        self,
        content: str,
        category: str = "fact",
        source: str = "",
        session_id: str = "",
        user_id: str = "",
        tags: list[str] | None = None,
        quality: float | None = None,
        type_: str | None = None,
        relations: list[dict] | None = None,
        warnings: list[str] | None = None,
        *,
        legacy: bool = False,
    ) -> str:
        """Legacy import/migration adapter; normal callers must use add_memory."""
        if not legacy:
            raise self._contract_error(
                "legacy_api_disabled",
                "content",
                "raw add is fenced; use add_memory(MemoryWrite)",
                content,
                "structured MemoryWrite",
            )
        clean_content, relation_json = self._strip_relations_from_content(content)
        relations_list = relations if relations is not None else json.loads(relation_json)
        parsed = parse_content(
            clean_content,
            category=category,
            relations=relations_list,
        )
        return self._insert_memory(
            content=render_content(parsed),
            category=parsed.category,
            source=source,
            session_id=session_id,
            user_id=user_id,
            tags=tags,
            quality=quality,
            type_=type_,
            relations=[relation.to_dict() for relation in parsed.relations],
            warnings=warnings,
        )

    def _apply_update(self, memory_id: str, **kwargs) -> bool:
        """Apply validated fields to one row."""
        self._fresh()
        try:
            existing = self._get_by_id_raw(memory_id)
            if not existing:
                return False

            updates = {}
            now = time.time()
            relations_to_replace = None

            if "content" in kwargs and kwargs["content"]:
                clean_content, relations_json = self._strip_relations_from_content(kwargs["content"])
                updates["content"] = clean_content
                relations_to_replace = json.loads(relations_json)
                # Re-extract entities
                updates["entities"] = json.dumps(extract_entities(clean_content))
                # Re-embed
                vector = self._require_embedding(clean_content)
                updates["vector"] = vector.tolist()

            if "relations" in kwargs and isinstance(kwargs["relations"], list):
                relations_to_replace = kwargs["relations"]

            if "category" in kwargs and kwargs["category"] in VALID_CATEGORIES:
                updates["category"] = kwargs["category"]

            if "tags" in kwargs and isinstance(kwargs["tags"], list):
                updates["tags"] = json.dumps(kwargs["tags"])

            if "quality" in kwargs and isinstance(kwargs["quality"], (int, float)):
                updates["quality"] = float(kwargs["quality"])

            if "type" in kwargs and kwargs["type"]:
                updates["type"] = kwargs["type"]

            if "entities" in kwargs and isinstance(kwargs["entities"], list):
                updates["entities"] = json.dumps(kwargs["entities"])

            if not updates and relations_to_replace is None:
                return False

            claims_may_change = "content" in updates or "category" in updates
            if claims_may_change and not self._close_conflicts_for_memory(memory_id):
                return False

            if updates:
                updates["updated_at"] = now
                self._table.update(f"id = '{memory_id}'", updates)

            if claims_may_change:
                self.detect_conflicts_for(memory_id)

            if relations_to_replace is not None:
                normalized_relations = self._write_relations_list(memory_id, relations_to_replace)
                self._table.update(
                    f"id = '{memory_id}'",
                    {"relations": json.dumps(normalized_relations), "updated_at": now},
                )

            # Rebuild links if entities changed
            if "entities" in updates:
                self._rebuild_all_links()

            return True
        except MemoryEmbeddingError:
            raise
        except Exception as e:
            logger.error("Update failed: %s", e)
            return False

    def update_memory(
        self,
        patch: MemoryPatch,
        *,
        _allow_claim_replacement: bool = False,
    ) -> dict[str, Any]:
        """Apply a typed patch while preserving the current memory by default."""
        if not isinstance(patch, MemoryPatch):
            raise self._contract_error(
                "invalid_type", "request", "update_memory requires MemoryPatch",
                type(patch).__name__, "MemoryPatch",
            )
        raw_patch: dict[str, Any] = {"memory_id": patch.memory_id}
        for field in (
            "domain", "subject", "facts", "tier", "category", "relations",
            "tags", "quality", "type",
        ):
            value = getattr(patch, field)
            if value is None:
                continue
            if field in {"facts", "tags"}:
                raw_patch[field] = list(value)
            elif field == "relations":
                raw_patch[field] = [relation.to_dict() for relation in value]
            else:
                raw_patch[field] = value
        patch = MemoryPatch.from_mapping(raw_patch)
        self._fresh()
        existing = self._get_by_id_raw(patch.memory_id)
        if not existing:
            raise self._contract_error(
                "memory_not_found", "memory_id", "memory does not exist",
                patch.memory_id, "existing memory ID",
            )

        replaced_content = str(existing.get("content") or "")
        content_fields_changed = any(
            value is not None
            for value in (patch.domain, patch.subject, patch.facts, patch.tier)
        )
        kwargs: dict[str, Any] = {}
        warning_dicts: list[dict[str, Any]] = []

        if content_fields_changed or patch.category is not None:
            parts = parse_content_parts(replaced_content)
            candidate = MemoryWrite.from_mapping({
                "domain": patch.domain or parts.domain,
                "subject": patch.subject or parts.subject,
                "facts": list(patch.facts) if patch.facts is not None else [parts.body],
                "tier": patch.tier or parts.tier,
                "category": patch.category or str(existing.get("category") or "fact"),
                "relations": [relation.to_dict() for relation in (patch.relations or ())],
            })
            canonical_content = render_content(candidate)
            exact, same_subject, conflicts = self._preflight_memory_write(
                candidate,
                exclude_id=patch.memory_id,
            )
            if exact is not None:
                raise self._contract_error(
                    "duplicate_memory", "facts", "update would duplicate another memory",
                    str(exact.get("id") or ""), "unique canonical memory",
                )
            if conflicts and not _allow_claim_replacement:
                raise self._contract_error(
                    "conflicting_claims",
                    "facts",
                    "update conflicts with another same-subject memory",
                    conflicts,
                    "resolve the conflict before updating",
                )
            if len(same_subject) > 1 and _allow_claim_replacement:
                raise self._contract_error(
                    "ambiguous_subject", "subject", "upsert target became ambiguous",
                    [str(row.get("id") or "") for row in same_subject],
                    "one same-subject memory",
                )
            if content_fields_changed:
                kwargs["content"] = canonical_content
            if patch.category is not None:
                kwargs["category"] = candidate.category
            warning_dicts = [warning.to_dict() for warning in contract_warnings(candidate)]
        else:
            canonical_content = replaced_content

        if patch.relations is not None:
            kwargs["relations"] = [relation.to_dict() for relation in patch.relations]
        if patch.tags is not None:
            kwargs["tags"] = list(patch.tags)
        if patch.quality is not None:
            kwargs["quality"] = patch.quality
        if patch.type is not None:
            kwargs["type"] = patch.type

        if not self._apply_update(patch.memory_id, **kwargs):
            raise self._contract_error(
                "update_failed", "memory_id", "memory update did not commit",
                patch.memory_id, "committed update",
            )
        return {
            "success": True,
            "status": "updated",
            "memory_id": patch.memory_id,
            "canonical_content": canonical_content,
            "replaced_content": replaced_content,
            "warnings": warning_dicts,
        }

    def update(self, memory_id: str, *, legacy: bool = False, **kwargs) -> bool:
        """Legacy import/migration adapter; normal callers use update_memory."""
        if not legacy:
            raise self._contract_error(
                "legacy_api_disabled",
                "content",
                "raw update is fenced; use update_memory(MemoryPatch)",
                sorted(kwargs),
                "structured MemoryPatch",
            )
        existing = self._get_by_id_raw(memory_id)
        if not existing:
            return False
        if "content" in kwargs and kwargs["content"]:
            clean_content, relation_json = self._strip_relations_from_content(kwargs["content"])
            relations = kwargs.get("relations", json.loads(relation_json))
            parsed = parse_content(
                clean_content,
                category=kwargs.get("category", str(existing.get("category") or "fact")),
                relations=relations,
            )
            kwargs["content"] = render_content(parsed)
            kwargs["category"] = parsed.category
            kwargs["relations"] = [relation.to_dict() for relation in parsed.relations]
        elif "category" in kwargs and kwargs["category"] not in VALID_CATEGORIES:
            raise self._contract_error(
                "invalid_category", "category", "unknown category is rejected",
                kwargs["category"], sorted(VALID_CATEGORIES),
            )
        return self._apply_update(memory_id, **kwargs)

    def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID."""
        self._fresh()
        try:
            existing = self._table.search().where(f"id = '{memory_id}'").limit(1).to_list()
            if not existing:
                return False
            if not self._close_conflicts_for_memory(memory_id, reason="memory deleted"):
                return False
            if not self._cleanup_edges_for_memory(memory_id):
                return False
            self._table.delete(f"id = '{memory_id}'")
            self._rebuild_all_links()
            self._update_db_size()
            return True
        except Exception as e:
            logger.error("Delete failed: %s", e)
            return False

    def import_records(self, items: list[dict],
                       conflict_records: list[dict] | None = None,
                       edge_records: list[dict] | None = None) -> dict:
        """Import portable memory rows, then rebuild typed edges and conflicts."""
        from uuid import uuid4

        self._fresh()
        existing_by_id = {
            str(row["id"]): row for row in self._get_all_raw()
        }
        existing_ids = set(existing_by_id)
        imported = 0
        skipped = 0
        errors = []
        now = time.time()
        rows_to_add = []
        relations_by_id = {}

        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            mem_id = str(item.get("id") or "")
            content = str(item.get("content") or "")
            relations = item.get("relations") if isinstance(item.get("relations"), list) else []
            if not content.strip():
                skipped += 1
                continue
            if mem_id and mem_id in existing_ids:
                skipped += 1
                if str(existing_by_id[mem_id].get("content") or "") == content:
                    relations_by_id[mem_id] = relations
                continue
            if not mem_id:
                mem_id = str(uuid4())[:12]

            category = str(item.get("category") or "fact")
            contract_relations = []
            for relation in relations:
                if not isinstance(relation, dict):
                    continue
                normalized_relation = {"type": relation.get("type")}
                if relation.get("target_id"):
                    normalized_relation["target_id"] = relation["target_id"]
                elif relation.get("target"):
                    normalized_relation["target"] = relation["target"]
                contract_relations.append(normalized_relation)
            try:
                parsed = parse_content(
                    content,
                    category=category,
                    relations=contract_relations,
                )
                content = render_content(parsed)
                category = parsed.category
                vector = self._require_embedding(content)
            except MemoryContractError as error:
                skipped += 1
                errors.append({"memory_id": mem_id, "error": error.to_dict()})
                continue
            entities = item.get("entities") if isinstance(item.get("entities"), list) else []
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            created_at = float(item.get("created_at") or now)

            rows_to_add.append({
                "id": mem_id,
                "content": content,
                "category": category,
                "entities": json.dumps(entities),
                "links": json.dumps([]),
                "relations": json.dumps(relations),
                "tags": json.dumps(tags),
                "quality": float(item.get("quality") or 0.5),
                "type": str(item.get("type") or category),
                "source": str(item.get("source") or "import"),
                "session_id": str(item.get("session_id") or ""),
                "user_id": str(item.get("user_id") or "hermes-user"),
                "created_at": created_at,
                "updated_at": float(item.get("updated_at") or now),
                "access_count": int(item.get("access_count") or 0),
                "accessed_at": float(item.get("accessed_at") or created_at),
                "vector": vector.tolist(),
            })
            relations_by_id[mem_id] = relations
            existing_ids.add(mem_id)
            imported += 1

        if edge_records is not None:
            for mem_id in relations_by_id:
                relations_by_id[mem_id] = []
            for edge in edge_records:
                if not isinstance(edge, dict):
                    continue
                source_id = str(edge.get("from") or edge.get("source_id") or "")
                if source_id not in relations_by_id:
                    continue
                relations_by_id[source_id].append({
                    "type": str(edge.get("relation_type") or edge.get("type") or ""),
                    "target_id": str(edge.get("to") or edge.get("target_id") or ""),
                    "target": str(edge.get("target_label") or edge.get("target") or ""),
                })

        if rows_to_add:
            self._table.add(rows_to_add)

        edge_count = 0
        for mem_id, relations in relations_by_id.items():
            normalized = self._write_relations_list(mem_id, relations)
            self._table.update(
                f"id = {_sql_literal(mem_id)}",
                {"relations": json.dumps(normalized)},
            )
            edge_count += len(normalized)

        self._rebuild_all_links()
        for mem_id in relations_by_id:
            self.detect_conflicts_for(mem_id)
        restored = self.import_conflict_records(conflict_records or [])
        self._update_db_size()
        return {
            "success": True,
            "imported": imported,
            "skipped": skipped,
            "relations_rebuilt": edge_count,
            "conflicts_restored": restored,
            "errors": errors,
        }

    def bulk_delete(self, memory_ids: list[str]) -> dict:
        """Delete multiple memories. Returns {deleted: N, errors: [...]}."""
        self._fresh()
        deleted = 0
        errors = []
        for mid in memory_ids:
            try:
                if self.delete(mid):
                    deleted += 1
                else:
                    errors.append(mid)
            except Exception as e:
                errors.append(mid)
                logger.error("Bulk delete failed for %s: %s", mid, e)
        return {"deleted": deleted, "errors": errors}

    def _compute_quality(self, memory: dict) -> float:
        """Compute dynamic quality score based on access_count, links count, age, and decay.

        Decay: entries not accessed recently lose importance over time.
        Uses accessed_at (last access) not created_at — a memory accessed yesterday
        stays fresh even if created months ago. Based on PMB's forgetting curve concept
        (factor_per_day ~0.985 -> ~50% importance after ~46 days without access).
        """
        access_count = memory.get('access_count', 0) or 0
        links = memory.get('links', [])
        if isinstance(links, str):
            try: links = json.loads(links)
            except: links = []
        n_links = len(links) if isinstance(links, (list, set)) else 0
        age_h = (time.time() - (memory.get('created_at', time.time()) or time.time())) / 3600

        # Decay based on time since last access (accessed_at)
        last_access = memory.get('accessed_at', memory.get('created_at', time.time())) or time.time()
        days_since_access = max(0, (time.time() - last_access) / 86400)
        # 0.985^days -> ~1.0 at day 0, ~0.5 at day 46, ~0.25 at day 93
        decay_factor = 0.985 ** days_since_access

        quality = 0.5
        if access_count >= 10: quality += 0.2
        elif access_count >= 5: quality += 0.15
        elif access_count >= 2: quality += 0.1
        quality += min(n_links * 0.05, 0.15)
        if age_h < 168: quality += 0.1
        elif age_h < 720: quality += 0.05
        # Apply decay — scale the bonus portion by decay factor, keep base at 0.5*decay
        # This way a stale memory drops toward 0.1 but never hits exactly 0
        quality = (quality * decay_factor) + (0.5 * (1.0 - decay_factor) * 0.1)
        return max(0.1, min(1.0, round(quality, 2)))

    def get_by_id(self, memory_id: str) -> dict | None:
        """Get a memory by ID (vector removed)."""
        self._fresh()
        try:
            result = self._table.search().where(f"id = '{memory_id}'").limit(1).to_list()
            if not result:
                return None
            mem = dict(result[0])
            mem.pop("vector", None)
            self._parse_json_fields(mem)
            # Increment access count + recalc quality
            new_access = (mem.get("access_count", 0) or 0) + 1
            mem["access_count"] = new_access
            mem["accessed_at"] = time.time()
            new_quality = self._compute_quality(mem)
            self._table.update(
                f"id = '{memory_id}'",
                {"access_count": new_access,
                 "accessed_at": time.time(),
                 "quality": new_quality},
            )
            mem["quality"] = new_quality
            return mem
        except Exception as e:
            logger.error("get_by_id failed: %s", e)
            return None

    def _get_by_id_raw(self, memory_id: str) -> dict | None:
        """Get a memory by ID INCLUDING vector (for similarity computation)."""
        self._fresh()
        try:
            result = self._table.search().where(f"id = '{memory_id}'").limit(1).to_list()
            if not result:
                return None
            mem = dict(result[0])
            self._parse_json_fields(mem)
            return mem
        except Exception:
            return None

    def _get_all_raw(self) -> list[dict]:
        """Get all memories WITH vectors."""
        self._fresh()
        try:
            results = self._table.to_arrow()
            memories = []
            for i in range(results.num_rows):
                mem = {}
                for col in results.column_names:
                    val = results.column(col)[i].as_py()
                    mem[col] = val
                self._parse_json_fields(mem)
                memories.append(mem)
            return memories
        except Exception as e:
            logger.error("_get_all_raw failed: %s", e)
            return []

    def get_all(self) -> list[dict]:
        """Get all memories WITHOUT vectors."""
        memories = self._get_all_raw()
        for m in memories:
            m.pop("vector", None)
        return memories

    def count(self) -> int:
        self._fresh()
        try:
            return self._table.count_rows()
        except Exception:
            return 0

    def _parse_json_fields(self, mem: dict):
        """Parse JSON string fields in-place."""
        for field in ("entities", "links", "tags"):
            if isinstance(mem.get(field), str):
                try:
                    mem[field] = json.loads(mem[field])
                except (json.JSONDecodeError, TypeError):
                    mem[field] = [] if field != "relations" else "[]"
        # relations is special — keep as JSON string for storage, parse for display
        if isinstance(mem.get("relations"), str):
            try:
                mem["relations"] = json.loads(mem["relations"])
            except (json.JSONDecodeError, TypeError):
                mem["relations"] = []

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    def _search_hybrid(self, query: str, top_k: int = 10,
                       category: str | None = None) -> list[dict]:
        """Run LanceDB BM25/vector hybrid retrieval."""
        self._fresh()
        vector = self._embed(query)
        try:
            # Hybrid: vector + BM25 RRF fusion
            base = (
                self._table.search(query_type='hybrid')
                .text(query)
                .vector(vector.tolist())
                .limit(top_k)
            )

            if category:
                results = base.where(f"category = '{category}'").to_list()
            else:
                results = base.to_list()
        except Exception as e:
            logger.error("Search failed: %s", e)
            return []

        memories = []
        now = time.time()
        ids_to_touch = []
        for r in results:
            mem = dict(r)
            mem.pop("vector", None)
            self._parse_json_fields(mem)
            mem["score"] = r.get("_relevance_score", 1.0 - mem.get("_distance", 0.0))
            # Precision gate: skip results with very low relevance score
            # Hybrid RRF scores are typically 0.015-0.035; below 0.005 is noise
            if mem["score"] < 0.005:
                continue
            mem["accessed_at"] = now if mem.get("accessed_at") is None else mem["accessed_at"]
            memories.append(mem)
            ids_to_touch.append(mem["id"])
        for mid in ids_to_touch:
            try:
                existing = self._get_by_id_raw(mid)
                if existing:
                    self._table.update(
                        f"id = '{mid}'",
                        {"access_count": (existing.get("access_count", 0) or 0) + 1,
                         "accessed_at": now},
                    )
            except Exception:
                pass
        return memories

    def _search_lexical(self, query: str, top_k: int = 10,
                        category: str | None = None) -> list[dict]:
        """Run BM25-only retrieval, with a deterministic substring fallback."""
        self._fresh()
        try:
            base = self._table.search(query, query_type="fts").limit(top_k)
            if category:
                rows = base.where(f"category = '{category}'").to_list()
            else:
                rows = base.to_list()
        except Exception:
            needle = query.strip().strip('"').lower()
            rows = [
                row for row in self._get_all_raw()
                if needle in (row.get("content") or "").lower()
                and (not category or row.get("category") == category)
            ][:top_k]

        memories = []
        for rank, row in enumerate(rows):
            memory = dict(row)
            memory.pop("vector", None)
            self._parse_json_fields(memory)
            memory["score"] = float(row.get("_score", 1.0 / (rank + 1)))
            memories.append(memory)
        return memories

    def _expand_relation_context(self, direct: list[dict], top_k: int,
                                 category: str | None = None) -> list[dict]:
        """Append one-hop typed neighbors while preserving direct-result priority."""
        results = list(direct[:top_k])
        seen = {str(memory.get("id")) for memory in results}
        if len(results) >= top_k:
            return results

        edges = sorted(
            self.get_typed_edges(),
            key=lambda edge: (
                str(edge.get("from", "")),
                str(edge.get("to", "")),
                str(edge.get("relation_type", "")),
                str(edge.get("target_label", "")),
                float(edge.get("created_at", 0.0)),
            ),
        )
        for seed in direct:
            seed_id = str(seed.get("id"))
            for edge in edges:
                neighbor_id = ""
                direction = "outgoing"
                if edge["from"] == seed_id:
                    neighbor_id = edge["to"]
                elif edge["to"] == seed_id:
                    neighbor_id = edge["from"]
                    direction = "incoming"
                if not neighbor_id or neighbor_id in seen:
                    continue
                neighbor = self._get_by_id_raw(neighbor_id)
                if not neighbor or (category and neighbor.get("category") != category):
                    continue
                neighbor.pop("vector", None)
                neighbor["score"] = float(seed.get("score", 0.0)) * 0.8
                neighbor["retrieval_source"] = "relation"
                neighbor["relation_type"] = edge["relation_type"]
                neighbor["relation_direction"] = direction
                neighbor["relation_seed_id"] = seed_id
                results.append(neighbor)
                seen.add(neighbor_id)
                if len(results) >= top_k:
                    return results
        return results

    def search(self, query: str, top_k: int = 10, category: str | None = None,
               mode: str = "auto", relation_depth: int = 1) -> list[dict]:
        """Search locally with deterministic routing and optional one-hop expansion."""
        selected_mode = route_search_mode(query) if mode == "auto" else mode
        if selected_mode not in {"hybrid", "lexical", "graph"}:
            selected_mode = "hybrid"
        seed_limit = (
            max(1, min(5, (top_k + 1) // 2))
            if selected_mode == "graph" and relation_depth > 0
            else top_k
        )
        direct = (
            self._search_lexical(query, seed_limit, category)
            if selected_mode == "lexical"
            else self._search_hybrid(query, seed_limit, category)
        )
        routing_fallback = ""
        if selected_mode == "lexical" and not direct:
            direct = self._search_hybrid(query, seed_limit, category)
            selected_mode = "hybrid"
            routing_fallback = "lexical_empty"
        for memory in direct:
            memory["retrieval_source"] = "direct"
            memory["search_mode"] = selected_mode
            if routing_fallback:
                memory["routing_fallback"] = routing_fallback
        if selected_mode == "graph" and relation_depth > 0:
            return self._expand_relation_context(direct, top_k, category)
        return direct[:top_k]

    def graph(self) -> dict:
        """Return all memories as graph nodes + edges (entity-based links)."""
        self._fresh()
        memories = self._get_all_raw()
        nodes = []
        for m in memories:
            content = m["content"]
            # Extract tier from content
            tier = "none"
            for t in ["1", "2", "3"]:
                if f"[Tier={t}]" in content:
                    tier = t
                    break
            nodes.append({
                "id": m["id"],
                "label": content[:30],
                "content": content,
                "category": m.get("category", "fact"),
                "entities": m.get("entities", []),
                "tags": m.get("tags", []),
                "quality": m.get("quality", 0.5),
                "type": m.get("type", m.get("category", "fact")),
                "tier": tier,
                "relations": m.get("relations", []),
                "created_at": m.get("created_at"),
                "access_count": m.get("access_count", 0),
            })
        edges = []
        for m in memories:
            links = m.get("links", [])
            if isinstance(links, str):
                try:
                    links = json.loads(links)
                except (json.JSONDecodeError, TypeError):
                    links = []
            for target in links:
                edges.append({"from": m["id"], "to": target})
        return {"nodes": nodes, "edges": edges}

    # -----------------------------------------------------------------------
    # Tag operations
    # -----------------------------------------------------------------------

    def get_tags(self) -> dict[str, int]:
        """Return all tags with their counts across all memories."""
        memories = self.get_all()
        tag_counts: dict[str, int] = {}
        for m in memories:
            tags = m.get("tags", [])
            if tags is None:
                tags = []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return dict(sorted(tag_counts.items(), key=lambda x: -x[1]))

    def update_tags(self, memory_id: str, tags: list[str]) -> bool:
        """Replace tags for a memory."""
        try:
            existing = self._get_by_id_raw(memory_id)
            if not existing:
                return False
            self._table.update(
                f"id = '{memory_id}'",
                {"tags": json.dumps(tags), "updated_at": time.time()},
            )
            return True
        except Exception:
            return False

    def bulk_tag(self, memory_ids: list[str], add_tags: list[str] | None = None,
                 remove_tags: list[str] | None = None) -> dict:
        """Add/remove tags from multiple memories."""
        results = {"updated": 0, "errors": []}
        for mid in memory_ids:
            try:
                mem = self._get_by_id_raw(mid)
                if not mem:
                    results["errors"].append(mid)
                    continue
                current_tags = set(mem.get("tags", []))
                if isinstance(current_tags, str):
                    try:
                        current_tags = set(json.loads(current_tags))
                    except (json.JSONDecodeError, TypeError):
                        current_tags = set()
                if add_tags:
                    current_tags.update(add_tags)
                if remove_tags:
                    current_tags.difference_update(remove_tags)
                self._table.update(
                    f"id = '{mid}'",
                    {"tags": json.dumps(sorted(current_tags)), "updated_at": time.time()},
                )
                results["updated"] += 1
            except Exception as e:
                results["errors"].append(mid)
                logger.error("Bulk tag failed for %s: %s", mid, e)
        return results

    def rename_tag(self, old_name: str, new_name: str) -> int:
        """Rename a tag across all memories. Returns number of memories updated."""
        memories = self.get_all()
        updated = 0
        for m in memories:
            tags = m.get("tags", [])
            if tags is None:
                tags = []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            if old_name in tags:
                new_tags = [new_name if t == old_name else t for t in tags]
                self._table.update(
                    f"id = '{m['id']}'",
                    {"tags": json.dumps(new_tags), "updated_at": time.time()},
                )
                updated += 1
        return updated

    def delete_tag(self, tag: str) -> int:
        """Remove a tag from all memories. Returns number of memories updated."""
        memories = self.get_all()
        updated = 0
        for m in memories:
            tags = m.get("tags", [])
            if tags is None:
                tags = []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            if tag in tags:
                new_tags = [t for t in tags if t != tag]
                self._table.update(
                    f"id = '{m['id']}'",
                    {"tags": json.dumps(new_tags), "updated_at": time.time()},
                )
                updated += 1
        return updated

    def merge_tags(self, sources: list[str], target: str) -> int:
        """Merge multiple tags into one. Returns number of memories updated."""
        memories = self.get_all()
        updated = 0
        source_set = set(sources)
        for m in memories:
            tags = m.get("tags", [])
            if tags is None:
                tags = []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            has_source = any(t in source_set for t in tags)
            if has_source:
                new_tags = set(tags)
                new_tags.difference_update(source_set)
                new_tags.add(target)
                self._table.update(
                    f"id = '{m['id']}'",
                    {"tags": json.dumps(sorted(new_tags)), "updated_at": time.time()},
                )
                updated += 1
        return updated

    # -----------------------------------------------------------------------
    # Filtered queries
    # -----------------------------------------------------------------------

    def get_by_filters(self, category: str | None = None, type_: str | None = None,
                       tag: str | None = None, quality_min: float | None = None,
                       quality_max: float | None = None,
                       date_from: float | None = None, date_to: float | None = None,
                       search_query: str | None = None,
                       offset: int = 0, limit: int = 20) -> dict:
        """Get memories with filters. Returns {memories, total, offset, limit}."""
        memories = self.get_all()

        # Apply filters
        if category:
            memories = [m for m in memories if m.get("category") == category]
        if type_:
            memories = [m for m in memories if m.get("type") == type_]
        if tag:
            memories = [m for m in memories if tag in (m.get("tags") or [])]
        if quality_min is not None:
            memories = [m for m in memories if (m.get("quality") or 0) >= quality_min]
        if quality_max is not None:
            memories = [m for m in memories if (m.get("quality") or 0) <= quality_max]
        if date_from:
            memories = [m for m in memories if (m.get("created_at") or 0) >= date_from]
        if date_to:
            memories = [m for m in memories if (m.get("created_at") or 0) <= date_to]
        if search_query:
            q = search_query.lower()
            memories = [m for m in memories if q in m.get("content", "").lower()]

        # Sort by created_at desc
        memories.sort(key=lambda m: m.get("created_at", 0), reverse=True)

        total = len(memories)
        page = memories[offset:offset + limit]

        return {"memories": page, "total": total, "offset": offset, "limit": limit}

    def get_timeline(self) -> list[dict]:
        """Group memories by day. Returns list of {date, count, memories}."""
        memories = self.get_all()
        from collections import defaultdict
        groups = defaultdict(list)
        for m in memories:
            ts = m.get("created_at", 0)
            day = time.strftime("%Y-%m-%d", time.gmtime(ts))
            groups[day].append(m)

        result = []
        for day in sorted(groups.keys(), reverse=True):
            result.append({
                "date": day,
                "count": len(groups[day]),
                "memories": sorted(groups[day], key=lambda m: m.get("created_at", 0), reverse=True),
            })
        return result

    def get_duplicates(self, threshold: float = 0.9) -> list[dict]:
        """Find duplicate memories by vector similarity. Returns groups of similar memories."""
        raw = self._get_all_raw()
        if len(raw) < 2:
            return []

        vectors = []
        mems = []
        for r in raw:
            vec = r.get("vector")
            if vec is None or not isinstance(vec, (list, np.ndarray)) or len(np.array(vec, dtype=np.float32)) < 2:
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

        # Find groups using connected components
        visited = set()
        groups = []
        for i in range(n):
            if i in visited:
                continue
            group = [i]
            visited.add(i)
            for j in range(i + 1, n):
                if j in visited:
                    continue
                if float(sim_matrix[i][j]) >= threshold:
                    group.append(j)
                    visited.add(j)
            if len(group) >= 2:
                groups.append([mems[idx] for idx in group])

        result = []
        for g in groups:
            result.append({
                "size": len(g),
                "memories": [{
                    "id": m["id"],
                    "content": m["content"][:200],
                    "category": m.get("category", "fact"),
                    "type": m.get("type", m.get("category", "fact")),
                    "tags": m.get("tags", []),
                    "quality": m.get("quality", 0.5),
                    "created_at": m.get("created_at", 0),
                } for m in g],
            })
        return result

    def get_stale(self, days: int = 90, quality_max: float = 0.3) -> list[dict]:
        """Find stale memories — old + low quality."""
        now = time.time()
        cutoff = now - (days * 86400)
        memories = self.get_all()
        stale = []
        for m in memories:
            created = m.get("created_at", now)
            quality = m.get("quality", 0.5)
            if created < cutoff and quality <= quality_max:
                stale.append(m)
        stale.sort(key=lambda m: m.get("created_at", 0))
        return stale

    def get_projection(self, n_neighbors: int = 15, min_dist: float = 0.1) -> list[dict]:
        """Compute UMAP 2D projection of all memory embeddings."""
        raw = self._get_all_raw()
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

        if len(vectors) < 3:
            return []

        try:
            import umap
            vec_matrix = np.array(vectors, dtype=np.float32)
            reducer = umap.UMAP(n_neighbors=min(n_neighbors, len(vectors) - 1),
                                min_dist=min_dist, random_state=42)
            embedding = reducer.fit_transform(vec_matrix)
            points = []
            for i, m in enumerate(mems):
                points.append({
                    "id": m["id"],
                    "x": float(embedding[i][0]),
                    "y": float(embedding[i][1]),
                    "content": m["content"][:100],
                    "category": m.get("category", "fact"),
                    "type": m.get("type", m.get("category", "fact")),
                    "tags": m.get("tags", []),
                    "quality": m.get("quality", 0.5),
                    "created_at": m.get("created_at", 0),
                })
            return points
        except ImportError:
            logger.warning("umap-learn not installed — skipping projection")
            return []
        except Exception as e:
            logger.warning("UMAP projection failed: %s", e)
            return []

    def get_clusters(self, threshold: float = 0.6, min_size: int = 2) -> list[dict]:
        """Find semantic clusters using label propagation on vector similarity."""
        raw = self._get_all_raw()
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

        if len(vectors) < 3:
            return []

        vec_matrix = np.array(vectors, dtype=np.float32)
        sim_matrix = np.dot(vec_matrix, vec_matrix.T)
        n = len(mems)

        # Build adjacency
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
        import random
        while changed and it < max_iter:
            changed = False
            it += 1
            order = list(range(n))
            random.shuffle(order)
            for i in order:
                if not adj[i]:
                    continue
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

        # Group by label
        groups = {}
        for i, lbl in enumerate(labels):
            if lbl not in groups:
                groups[lbl] = []
            groups[lbl].append(i)

        # Build result
        clusters = []
        for lbl, indices in groups.items():
            if len(indices) < min_size:
                continue
            members = []
            for idx in indices:
                m = mems[idx]
                members.append({
                    "id": m["id"],
                    "content": m["content"][:100],
                    "category": m.get("category", "fact"),
                    "type": m.get("type", m.get("category", "fact")),
                    "tags": m.get("tags", []),
                    "quality": m.get("quality", 0.5),
                    "created_at": m.get("created_at", 0),
                })
            # Centroid = average of member vectors projected
            member_vecs = np.array([vectors[idx] for idx in indices], dtype=np.float32)
            centroid = member_vecs.mean(axis=0)
            clusters.append({
                "id": lbl,
                "size": len(members),
                "members": members,
                "centroid": {"x": float(centroid[0]), "y": float(centroid[1])},
            })

        clusters.sort(key=lambda c: -c["size"])
        return clusters

    # -----------------------------------------------------------------------
    # Linking
    # -----------------------------------------------------------------------

    def _rebuild_links_for(self, memory_id: str) -> None:
        """Build links for a single memory against all other memories."""
        target = self._get_by_id_raw(memory_id)
        if not target:
            return
        target_entities = set(target.get("entities", []))
        target_sig = {e for e in target_entities if e not in _STOP_ENTITIES}

        all_memories = self._get_all_raw()
        linked = []
        for m in all_memories:
            if m["id"] == memory_id:
                continue
            m_entities = set(m.get("entities", []))
            m_sig = {e for e in m_entities if e not in _STOP_ENTITIES}
            shared = target_sig & m_sig
            if len(shared) >= 2:
                linked.append(m["id"])
        linked = linked[:8]
        self._table.update(f"id = '{memory_id}'", {"links": json.dumps(linked)})

        for lid in linked:
            row = self._table.search().where(f"id = '{lid}'").limit(1).to_list()
            if row:
                existing = json.loads(row[0].get("links", "[]") or "[]")
                if memory_id not in existing:
                    existing = list(existing) + [memory_id]
                    existing = existing[:8]
                    self._table.update(f"id = '{lid}'", {"links": json.dumps(existing)})

    def _rebuild_all_links(self) -> None:
        """Rebuild all entity-based links across the entire store."""
        all_memories = self._get_all_raw()
        mem_sigs = {}
        for m in all_memories:
            entities = set(m.get("entities", []))
            sig = {e for e in entities if e not in _STOP_ENTITIES}
            mem_sigs[m["id"]] = sig

        all_ids = list(mem_sigs.keys())
        for i, mid in enumerate(all_ids):
            linked = []
            for j, other in enumerate(all_ids):
                if i == j:
                    continue
                shared = mem_sigs[mid] & mem_sigs[other]
                if len(shared) >= 2:
                    linked.append(other)
            linked = linked[:8]
            self._table.update(f"id = '{mid}'", {"links": json.dumps(linked)})

    def update_entities(self, memory_id: str, entities: list[str]) -> bool:
        """Update entities/tags for a memory. Rebuilds links."""
        try:
            existing = self._get_by_id_raw(memory_id)
            if not existing:
                return False
            self._table.update(
                f"id = '{memory_id}'",
                {"entities": json.dumps(entities), "updated_at": time.time()},
            )
            self._rebuild_all_links()
            return True
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _update_db_size(self):
        self._db_size = self._compute_db_size()
