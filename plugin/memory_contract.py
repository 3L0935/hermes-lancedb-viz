"""Pure validation and canonical formatting for LanceDB memory writes.

This module deliberately imports neither LanceDB nor Ollama.  It is the shared
contract for agent tools, cron jobs, migrations, and the persistent store.

Density limits were selected from a read-only Arrow scan of the live table on
2026-09-10 (451 rows; 422 structurally canonical rows).  Existing body lengths
were p50=215, p75=350, p90=698, p95=952, p99=1342, max=1650 characters.

Treating each existing body as one fact, the candidate per-fact caps retained:

    cap       kept       rejected
    240       248/422     174
    480       353/422      69
    768       385/422      37
    1000      405/422      17   (96.0%; selected)
    1200      412/422      10
    1500      420/422       2

The 2,000-character aggregate cap preserves every structurally canonical row
when a genuinely multi-part body is represented by more than one fact.  The
12-fact cap leaves headroom above the measured p99 explicit-claim count of 8;
421/422 canonical rows had at most 10 explicit key=value claims.  Migration
must classify longer legacy rows for review, never fragment them automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping, Sequence


MAX_DOMAIN_CHARS = 32
MAX_SUBJECT_CHARS = 80
MAX_FACTS = 12
MAX_FACT_CHARS = 1000
MAX_TOTAL_FACT_CHARS = 2000
MAX_RELATIONS = 20
MAX_TAGS = 20
MAX_TAG_CHARS = 80

VALID_CATEGORIES = frozenset({
    "user_pref", "project", "tech", "correction", "fact",
    "decision", "insight", "reference", "pattern", "question",
})
VALID_RELATION_TYPES = frozenset({
    "part_of", "depends", "requires", "runs_on", "connects_to",
    "uses", "extends", "supersedes", "invalidates", "contradicts",
})
VALID_WRITE_MODES = frozenset({"create", "upsert_subject"})

_GENERIC_SUBJECTS = frozenset({
    "app", "application", "config", "configuration", "correction",
    "fact", "general", "memory", "memories", "misc", "plugin",
    "preference", "project", "projet", "service", "setup", "system",
    "tech", "tool", "tools", "user",
})
_FORBIDDEN_LABEL_RE = re.compile(r"[:\[\]\r\n]")
_TIER_MARKER_RE = re.compile(r"\[\s*tier\s*=\s*([^\]]*)\]", re.IGNORECASE)
_RELATION_MARKER_RE = re.compile(r"::\s*relations\s*::", re.IGNORECASE)
_CLAIM_KEY_RE = re.compile(
    r"(?<![\w.-])([A-Za-z][A-Za-z0-9_.-]*)\s*=\s*"
)
_PREFIX_RE = re.compile(
    r"^(?P<domain>[^\s:]+):(?P<subject>\S+)(?:\s+(?P<body>.*))?$",
    re.DOTALL,
)


@dataclass(frozen=True)
class ContractIssue:
    code: str
    field: str
    message: str
    received: Any
    expected: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "field": self.field,
            "message": self.message,
            "received": self.received,
            "expected": self.expected,
        }


class MemoryContractError(ValueError):
    """A single actionable contract failure with a stable JSON shape."""

    def __init__(self, issue: ContractIssue):
        super().__init__(issue.message)
        self.issue = issue

    def to_dict(self) -> dict[str, Any]:
        return self.issue.to_dict()


def _raise(
    code: str,
    field: str,
    message: str,
    received: Any,
    expected: Any,
) -> None:
    raise MemoryContractError(ContractIssue(
        code=code,
        field=field,
        message=message,
        received=received,
        expected=expected,
    ))


def _normalize_label(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        _raise("invalid_type", field, f"{field} must be a string", type(value).__name__, "string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"\s+", "_", normalized)
    if not normalized:
        _raise("required_field", field, f"{field} is required", value, "non-empty string")
    if len(normalized) > max_chars:
        _raise(
            f"{field}_too_long",
            field,
            f"{field} exceeds {max_chars} characters",
            len(normalized),
            f"1..{max_chars} characters",
        )
    if _FORBIDDEN_LABEL_RE.search(normalized):
        _raise(
            f"invalid_{field}",
            field,
            f"{field} cannot contain colons, brackets, control characters, or newlines",
            value,
            "a single stable label",
        )
    return normalized


def _normalize_fact(
    value: Any,
    index: int,
    *,
    domain: str,
    subject: str,
    enforce_density: bool = True,
) -> str:
    field = f"facts[{index}]"
    if not isinstance(value, str):
        _raise("invalid_type", field, "each fact must be a string", type(value).__name__, "string")
    if "\n" in value or "\r" in value:
        _raise(
            "multiline_fact",
            field,
            "facts must be dense inline text, not multiline paragraphs",
            "contains newline",
            "single-line fact",
        )

    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[\t ]+", " ", normalized)
    if not normalized:
        _raise("empty_fact", field, "facts cannot be empty", value, "non-empty string")
    if _TIER_MARKER_RE.search(normalized):
        _raise(
            "nested_tier_marker",
            field,
            "tier belongs in the tier field, not inside a fact",
            value,
            "fact text without [Tier=N]",
        )
    if _RELATION_MARKER_RE.search(normalized):
        _raise(
            "nested_relation_marker",
            field,
            "relations belong in the relations field, not inside a fact",
            value,
            "fact text without ::relations::",
        )

    expected_prefix = f"{domain}:{subject}"
    if re.match(rf"^{re.escape(expected_prefix)}(?:\s|$)", normalized, re.IGNORECASE):
        _raise(
            "duplicate_subject_prefix",
            field,
            "domain and subject must not be repeated inside a fact",
            value,
            f"fact text without {expected_prefix}",
        )

    normalized = _CLAIM_KEY_RE.sub(lambda match: f"{match.group(1).lower()}=", normalized)
    if enforce_density and len(normalized) > MAX_FACT_CHARS:
        _raise(
            "fact_too_long",
            field,
            f"one fact exceeds the measured {MAX_FACT_CHARS}-character cap",
            len(normalized),
            f"1..{MAX_FACT_CHARS} characters",
        )
    return normalized


def _normalize_category(value: Any) -> str:
    if not isinstance(value, str):
        _raise("invalid_type", "category", "category must be a string", type(value).__name__, sorted(VALID_CATEGORIES))
    normalized = value.strip().lower()
    if normalized not in VALID_CATEGORIES:
        _raise(
            "invalid_category",
            "category",
            "unknown categories are rejected instead of being coerced to fact",
            value,
            sorted(VALID_CATEGORIES),
        )
    return normalized


def _normalize_tier(value: Any, *, field: str = "tier") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2, 3):
        _raise("invalid_tier", field, "tier must be integer 1, 2, or 3", value, [1, 2, 3])
    return value


@dataclass(frozen=True)
class MemoryRelation:
    type: str
    target_id: str | None = None
    target: str | None = None

    @classmethod
    def from_mapping(cls, value: Any, index: int) -> "MemoryRelation":
        field = f"relations[{index}]"
        if not isinstance(value, Mapping):
            _raise("invalid_type", field, "each relation must be an object", type(value).__name__, "object")
        unknown = set(value) - {"type", "target_id", "target"}
        if unknown:
            _raise("unknown_field", field, "relation contains unknown fields", sorted(unknown), ["type", "target_id", "target"])

        relation_type = value.get("type")
        if not isinstance(relation_type, str) or relation_type not in VALID_RELATION_TYPES:
            _raise(
                "invalid_relation_type",
                f"{field}.type",
                "unknown relation type",
                relation_type,
                sorted(VALID_RELATION_TYPES),
            )

        target_id = value.get("target_id")
        target = value.get("target")
        if bool(target_id) == bool(target):
            _raise(
                "invalid_relation_target",
                field,
                "relation requires exactly one of target_id or target",
                {"target_id": target_id, "target": target},
                "exactly one non-empty target field",
            )
        selected_field = "target_id" if target_id else "target"
        selected = target_id if target_id else target
        if not isinstance(selected, str):
            _raise("invalid_type", f"{field}.{selected_field}", "relation target must be a string", type(selected).__name__, "string")
        normalized = unicodedata.normalize("NFKC", selected).strip()
        if not normalized or len(normalized) > 160 or "\n" in normalized or "\r" in normalized:
            _raise(
                "invalid_relation_target",
                f"{field}.{selected_field}",
                "relation target must be a non-empty single-line value up to 160 characters",
                selected,
                "1..160 characters",
            )
        return cls(
            type=relation_type,
            target_id=normalized if target_id else None,
            target=normalized if target else None,
        )

    def to_dict(self) -> dict[str, str]:
        result = {"type": self.type}
        if self.target_id is not None:
            result["target_id"] = self.target_id
        if self.target is not None:
            result["target"] = self.target
        return result


def _normalize_relations(value: Any) -> tuple[MemoryRelation, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _raise("invalid_type", "relations", "relations must be an array", type(value).__name__, "array")
    if len(value) > MAX_RELATIONS:
        _raise("too_many_relations", "relations", "too many relations", len(value), f"0..{MAX_RELATIONS}")
    return tuple(MemoryRelation.from_mapping(relation, index) for index, relation in enumerate(value))


@dataclass(frozen=True)
class MemoryContentParts:
    domain: str
    subject: str
    body: str
    tier: int


@dataclass(frozen=True)
class MemoryWrite:
    domain: str
    subject: str
    facts: tuple[str, ...]
    tier: int
    category: str
    relations: tuple[MemoryRelation, ...] = ()
    write_mode: str = "create"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MemoryWrite":
        if not isinstance(value, Mapping):
            _raise("invalid_type", "request", "memory write must be an object", type(value).__name__, "object")
        allowed = {"domain", "subject", "facts", "tier", "category", "relations", "write_mode"}
        unknown = set(value) - allowed
        if unknown:
            _raise("unknown_field", "request", "memory write contains unknown fields", sorted(unknown), sorted(allowed))

        for field in ("domain", "subject", "facts", "tier", "category"):
            if field not in value:
                _raise("required_field", field, f"{field} is required", None, "present")

        domain = _normalize_label(value["domain"], field="domain", max_chars=MAX_DOMAIN_CHARS)
        subject = _normalize_label(value["subject"], field="subject", max_chars=MAX_SUBJECT_CHARS)

        raw_facts = value["facts"]
        if isinstance(raw_facts, (str, bytes)) or not isinstance(raw_facts, Sequence):
            _raise("invalid_type", "facts", "facts must be an array of strings", type(raw_facts).__name__, "array")
        if not raw_facts:
            _raise("required_field", "facts", "at least one fact is required", [], "1 or more facts")
        if len(raw_facts) > MAX_FACTS:
            _raise("too_many_facts", "facts", "too many facts in one memory", len(raw_facts), f"1..{MAX_FACTS}")
        facts = tuple(
            _normalize_fact(fact, index, domain=domain, subject=subject)
            for index, fact in enumerate(raw_facts)
        )
        total_chars = sum(len(fact) for fact in facts)
        if total_chars > MAX_TOTAL_FACT_CHARS:
            _raise(
                "facts_too_long",
                "facts",
                f"combined facts exceed the measured {MAX_TOTAL_FACT_CHARS}-character cap",
                total_chars,
                f"1..{MAX_TOTAL_FACT_CHARS} characters",
            )

        write_mode = value.get("write_mode", "create")
        if write_mode not in VALID_WRITE_MODES:
            _raise(
                "invalid_write_mode",
                "write_mode",
                "write_mode must be create unless subject-level upsert is explicitly requested",
                write_mode,
                sorted(VALID_WRITE_MODES),
            )

        return cls(
            domain=domain,
            subject=subject,
            facts=facts,
            tier=_normalize_tier(value["tier"]),
            category=_normalize_category(value["category"]),
            relations=_normalize_relations(value.get("relations")),
            write_mode=write_mode,
        )


@dataclass(frozen=True)
class MemoryPatch:
    memory_id: str
    domain: str | None = None
    subject: str | None = None
    facts: tuple[str, ...] | None = None
    tier: int | None = None
    category: str | None = None
    relations: tuple[MemoryRelation, ...] | None = None
    tags: tuple[str, ...] | None = None
    quality: float | None = None
    type: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MemoryPatch":
        if not isinstance(value, Mapping):
            _raise("invalid_type", "request", "memory patch must be an object", type(value).__name__, "object")
        allowed = {
            "memory_id", "domain", "subject", "facts", "tier", "category",
            "relations", "tags", "quality", "type",
        }
        unknown = set(value) - allowed
        if unknown:
            _raise("unknown_field", "request", "memory patch contains unknown fields", sorted(unknown), sorted(allowed))

        memory_id = value.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id.strip():
            _raise("required_field", "memory_id", "memory_id is required", memory_id, "non-empty string")
        changed = set(value) - {"memory_id"}
        if not changed:
            _raise("empty_patch", "patch", "at least one structured field must be updated", [], sorted(allowed - {"memory_id"}))

        domain = _normalize_label(value["domain"], field="domain", max_chars=MAX_DOMAIN_CHARS) if "domain" in value else None
        subject = _normalize_label(value["subject"], field="subject", max_chars=MAX_SUBJECT_CHARS) if "subject" in value else None
        tier = _normalize_tier(value["tier"]) if "tier" in value else None
        category = _normalize_category(value["category"]) if "category" in value else None
        relations = _normalize_relations(value["relations"]) if "relations" in value else None

        tags = None
        if "tags" in value:
            raw_tags = value["tags"]
            if isinstance(raw_tags, (str, bytes)) or not isinstance(raw_tags, Sequence):
                _raise("invalid_type", "tags", "tags must be an array of strings", type(raw_tags).__name__, "array")
            if len(raw_tags) > MAX_TAGS:
                _raise("too_many_tags", "tags", "too many tags", len(raw_tags), f"0..{MAX_TAGS}")
            normalized_tags = []
            for index, tag in enumerate(raw_tags):
                normalized_tag = _normalize_label(tag, field=f"tags[{index}]", max_chars=MAX_TAG_CHARS)
                if normalized_tag not in normalized_tags:
                    normalized_tags.append(normalized_tag)
            tags = tuple(normalized_tags)

        quality = None
        if "quality" in value:
            raw_quality = value["quality"]
            if isinstance(raw_quality, bool) or not isinstance(raw_quality, (int, float)):
                _raise("invalid_type", "quality", "quality must be a number", type(raw_quality).__name__, "number from 0 to 1")
            quality = float(raw_quality)
            if not 0.0 <= quality <= 1.0:
                _raise("invalid_quality", "quality", "quality must be between 0 and 1", raw_quality, "0..1")

        memory_type = None
        if "type" in value:
            memory_type = _normalize_label(value["type"], field="type", max_chars=MAX_TAG_CHARS)

        facts = None
        if "facts" in value:
            raw_facts = value["facts"]
            if isinstance(raw_facts, (str, bytes)) or not isinstance(raw_facts, Sequence):
                _raise("invalid_type", "facts", "facts must be an array of strings", type(raw_facts).__name__, "array")
            if not raw_facts:
                _raise("required_field", "facts", "at least one fact is required", [], "1 or more facts")
            if len(raw_facts) > MAX_FACTS:
                _raise("too_many_facts", "facts", "too many facts in one memory", len(raw_facts), f"1..{MAX_FACTS}")
            fact_domain = domain or "Existing"
            fact_subject = subject or "Existing"
            facts = tuple(
                _normalize_fact(fact, index, domain=fact_domain, subject=fact_subject)
                for index, fact in enumerate(raw_facts)
            )
            total_chars = sum(len(fact) for fact in facts)
            if total_chars > MAX_TOTAL_FACT_CHARS:
                _raise("facts_too_long", "facts", "combined facts exceed the measured cap", total_chars, f"1..{MAX_TOTAL_FACT_CHARS} characters")

        return cls(
            memory_id=memory_id.strip(),
            domain=domain,
            subject=subject,
            facts=facts,
            tier=tier,
            category=category,
            relations=relations,
            tags=tags,
            quality=quality,
            type=memory_type,
        )


def render_content(memory: MemoryWrite) -> str:
    rendered_facts = []
    for index, fact in enumerate(memory.facts):
        rendered = fact
        if index < len(memory.facts) - 1 and not rendered.endswith((".", "!", "?", ";")):
            rendered += "."
        rendered_facts.append(rendered)
    body = " ".join(rendered_facts)
    return f"{memory.domain}:{memory.subject} {body} [Tier={memory.tier}]"


def parse_content_parts(content: Any) -> MemoryContentParts:
    """Parse and normalize wrappers without applying density caps to the body."""
    if not isinstance(content, str):
        _raise("invalid_type", "content", "content must be a string", type(content).__name__, "string")
    normalized = unicodedata.normalize("NFKC", content).strip()
    markers = _TIER_MARKER_RE.findall(normalized)
    if not markers:
        _raise("missing_tier_marker", "content", "legacy content is missing [Tier=N]", content, "one final [Tier=1|2|3]")
    if len(markers) > 1:
        _raise("multiple_tier_markers", "content", "legacy content contains multiple tier markers", len(markers), "exactly one final tier marker")
    if markers[0].strip() not in {"1", "2", "3"}:
        _raise("invalid_tier", "content", "legacy tier must be 1, 2, or 3", markers[0], [1, 2, 3])
    final_tier = re.search(r"\[\s*tier\s*=\s*[123]\s*\]\s*$", normalized, re.IGNORECASE)
    if final_tier is None:
        _raise("tier_not_final", "content", "the tier marker must be the final content token", content, "Domain:Subject facts [Tier=N]")

    without_tier = normalized[:final_tier.start()].strip()
    prefix = _PREFIX_RE.fullmatch(without_tier)
    if prefix is None:
        _raise("missing_subject_prefix", "content", "legacy content must start with Domain:Subject", content, "Domain:Subject facts [Tier=N]")
    body = (prefix.group("body") or "").strip()
    if not body:
        _raise("required_field", "facts", "legacy content has no fact body", content, "one or more facts")

    domain = _normalize_label(prefix.group("domain"), field="domain", max_chars=MAX_DOMAIN_CHARS)
    subject = _normalize_label(prefix.group("subject"), field="subject", max_chars=MAX_SUBJECT_CHARS)
    body = _normalize_fact(
        body,
        0,
        domain=domain,
        subject=subject,
        enforce_density=False,
    )
    return MemoryContentParts(
        domain=domain,
        subject=subject,
        body=body,
        tier=int(markers[0].strip()),
    )


def parse_content(
    content: Any,
    *,
    category: str = "fact",
    relations: Sequence[Mapping[str, Any]] | None = None,
    write_mode: str = "create",
) -> MemoryWrite:
    parts = parse_content_parts(content)

    return MemoryWrite.from_mapping({
        "domain": parts.domain,
        "subject": parts.subject,
        "facts": [parts.body],
        "tier": parts.tier,
        "category": category,
        "relations": list(relations or []),
        "write_mode": write_mode,
    })


def canonicalize_content(content: Any, *, category: str = "fact") -> str:
    return render_content(parse_content(content, category=category))


def contract_warnings(memory: MemoryWrite) -> tuple[ContractIssue, ...]:
    subject = memory.subject.casefold()
    if subject == memory.domain.casefold() or subject in _GENERIC_SUBJECTS:
        suggestion = f"{memory.subject}_<component>_<failure>"
        return (ContractIssue(
            code="overly_broad_subject",
            field="subject",
            message=(
                f"'{memory.domain}:{memory.subject}' is overly broad; use a more specific subject "
                f"such as '{memory.domain}:{suggestion}' to avoid unrelated claim conflicts"
            ),
            received=memory.subject,
            expected="component-, bug-, or decision-specific subject",
        ),)
    return ()


def memory_fingerprint(memory: MemoryWrite) -> str:
    relations = sorted(
        (relation.to_dict() for relation in memory.relations),
        key=lambda relation: json.dumps(relation, sort_keys=True, ensure_ascii=False),
    )
    payload = {
        "content": render_content(memory).casefold(),
        "category": memory.category,
        "relations": relations,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
