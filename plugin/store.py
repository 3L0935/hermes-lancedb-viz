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

logger = logging.getLogger(__name__)

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
        self._db_size = self._compute_db_size()

    @property
    def db_size(self) -> int:
        return self._db_size

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
        """Create memory_edges table if it doesn't exist."""
        import pyarrow as pa
        try:
            return self._db.open_table("memory_edges")
        except Exception:
            schema = pa.schema([
                pa.field("source_id", pa.string()),
                pa.field("relation_type", pa.string()),
                pa.field("target_label", pa.string()),
                pa.field("created_at", pa.float64()),
            ])
            return self._db.create_table("memory_edges", schema=schema)

    def _write_relations(self, mem_id: str, content: str):
        """Parse and write ::relations:: edges for a memory."""
        relations = self._parse_relations(content)
        if not relations:
            return
        try:
            tbl = self._ensure_edges_table()
            now = time.time()
            for r in relations:
                tbl.add([{
                    "source_id": mem_id,
                    "relation_type": r["type"],
                    "target_label": r["target"],
                    "created_at": now,
                }])
        except Exception as e:
            logger.warning("Failed to write relations for %s: %s", mem_id, e)

    def _write_relations_list(self, mem_id: str, relations: list[dict]):
        """Write typed edges to memory_edges for a list of {type, target}."""
        if not relations:
            return
        try:
            tbl = self._ensure_edges_table()
            now = time.time()
            for r in relations:
                tbl.add([{
                    "source_id": mem_id,
                    "relation_type": r["type"],
                    "target_label": r["target"],
                    "created_at": now,
                }])
        except Exception as e:
            logger.warning("Failed to write relations for %s: %s", mem_id, e)

    def get_typed_edges(self) -> list[dict]:
        """Return all typed edges from memory_edges."""
        try:
            tbl = self._ensure_edges_table()
            data = tbl.to_arrow().to_pydict()
            edges = []
            for i in range(len(data.get("source_id", []))):
                edges.append({
                    "from": str(data["source_id"][i]),
                    "relation_type": str(data["relation_type"][i]),
                    "target_label": str(data["target_label"][i]),
                    "created_at": float(data["created_at"][i]),
                })
            return edges
        except Exception:
            return []

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

    def add(self, content: str, category: str = "fact", source: str = "",
            session_id: str = "", user_id: str = "",
            tags: list[str] | None = None,
            quality: float | None = None,
            type_: str | None = None) -> str:
        """Add a new memory. Extracts entities, embeds, links."""
        from uuid import uuid4
        mem_id = str(uuid4())[:12]

        # Fallback invalid category
        if category not in VALID_CATEGORIES:
            category = "fact"

        # Separate relations from content
        clean_content, relations_json = self._strip_relations_from_content(content)
        relations_list = json.loads(relations_json)

        vector = self._embed(clean_content)
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

        # Write typed relations to memory_edges
        self._write_relations_list(mem_id, relations_list)

        self._update_db_size()

        return mem_id

    def update(self, memory_id: str, **kwargs) -> bool:
        """Update fields of a memory. Accepts: content, category, tags, quality, type, entities."""
        try:
            existing = self._get_by_id_raw(memory_id)
            if not existing:
                return False

            updates = {}
            now = time.time()

            if "content" in kwargs and kwargs["content"]:
                clean_content, relations_json = self._strip_relations_from_content(kwargs["content"])
                updates["content"] = clean_content
                updates["relations"] = relations_json
                # Re-extract entities
                updates["entities"] = json.dumps(extract_entities(clean_content))
                # Re-embed
                vector = self._embed(clean_content)
                updates["vector"] = vector.tolist()
                # Re-write relations
                relations_list = json.loads(relations_json)
                self._write_relations_list(memory_id, relations_list)

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

            if not updates:
                return False

            updates["updated_at"] = now
            self._table.update(f"id = '{memory_id}'", updates)

            # Rebuild links if entities changed
            if "entities" in updates:
                self._rebuild_all_links()

            return True
        except Exception as e:
            logger.error("Update failed: %s", e)
            return False

    def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID."""
        try:
            existing = self._table.search().where(f"id = '{memory_id}'").limit(1).to_list()
            if not existing:
                return False
            self._table.delete(f"id = '{memory_id}'")
            self._rebuild_all_links()
            self._update_db_size()
            return True
        except Exception as e:
            logger.error("Delete failed: %s", e)
            return False

    def bulk_delete(self, memory_ids: list[str]) -> dict:
        """Delete multiple memories. Returns {deleted: N, errors: [...]}."""
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

    def search(self, query: str, top_k: int = 10, category: str | None = None) -> list[dict]:
        """Semantic search using vector + FTS hybrid (BM25).
        Falls back to pure vector search if FTS index is unavailable.
        
        LanceDB 0.33 hybrid API: .search(query_type='hybrid').text(q).vector(v)"""
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

    def graph(self) -> dict:
        """Return all memories as graph nodes + edges (entity-based links)."""
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
