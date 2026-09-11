import json
from pathlib import Path
import unittest

from plugin.memory_contract import (
    MAX_FACTS,
    MAX_FACT_CHARS,
    MAX_TOTAL_FACT_CHARS,
    MemoryContractError,
    MemoryPatch,
    MemoryWrite,
    canonicalize_content,
    contract_warnings,
    memory_fingerprint,
    render_content,
)


class MemoryContractTests(unittest.TestCase):
    def test_versioned_fixture_freezes_observed_baseline_and_examples(self):
        fixture_path = Path(__file__).parent / "fixtures" / "memory_contract_v2.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(2, fixture["contract_version"])
        self.assertEqual({
            "rows": 450,
            "missing_valid_wrapper": 18,
            "missing_or_invalid_tier": 5,
            "multiple_tier_markers": 7,
            "duplicate_subject_prefix": 2,
            "content_over_500_chars": 88,
        }, fixture["baseline"])
        for case in fixture["valid"]:
            MemoryWrite.from_mapping(case["request"])
        for case in fixture["invalid"]:
            with self.assertRaises(MemoryContractError) as caught:
                if "request" in case:
                    MemoryWrite.from_mapping(case["request"])
                else:
                    canonicalize_content(case["content"])
            self.assertEqual(case["code"], caught.exception.issue.code, case["name"])

    def test_dense_fact_within_measured_cap_is_valid(self):
        dense_fact = "root_cause=MVCC " + "dense correction detail " * 20

        memory = MemoryWrite.from_mapping({
            "domain": "Correction",
            "subject": "LanceDB_MVCC",
            "facts": [dense_fact],
            "tier": 1,
            "category": "correction",
        })

        self.assertEqual("Correction", memory.domain)
        self.assertEqual("LanceDB_MVCC", memory.subject)
        self.assertLessEqual(len(memory.facts[0]), MAX_FACT_CHARS)
        self.assertTrue(render_content(memory).endswith("[Tier=1]"))

    def test_safe_normalization_is_deterministic(self):
        memory = MemoryWrite.from_mapping({
            "domain": "  Project  ",
            "subject": "Memory   Writing",
            "facts": ["  PORT = 7777   owner=elo.  "],
            "tier": 2,
            "category": "project",
        })

        self.assertEqual("Project", memory.domain)
        self.assertEqual("Memory_Writing", memory.subject)
        self.assertEqual(("port=7777 owner=elo.",), memory.facts)
        self.assertEqual(
            "Project:Memory_Writing port=7777 owner=elo. [Tier=2]",
            render_content(memory),
        )

    def test_canonicalization_is_idempotent(self):
        raw = "  Project:Alpha   PORT = 7777   owner=elo.  [Tier=2]  "
        canonical = canonicalize_content(raw, category="project")

        self.assertEqual(canonical, canonicalize_content(canonical, category="project"))
        self.assertEqual("Project:Alpha port=7777 owner=elo. [Tier=2]", canonical)

    def test_missing_wrapper_is_rejected(self):
        self.assert_contract_error(
            "missing_subject_prefix",
            lambda: canonicalize_content("subjectless prose [Tier=2]"),
            field="content",
        )

    def test_nested_tier_inside_fact_is_rejected(self):
        self.assert_contract_error(
            "nested_tier_marker",
            lambda: MemoryWrite.from_mapping({
                "domain": "Project",
                "subject": "Alpha",
                "facts": ["state=active [Tier=2]"],
                "tier": 2,
                "category": "project",
            }),
            field="facts[0]",
        )

    def test_duplicated_domain_subject_inside_fact_is_rejected(self):
        self.assert_contract_error(
            "duplicate_subject_prefix",
            lambda: MemoryWrite.from_mapping({
                "domain": "Correction",
                "subject": "Hermes",
                "facts": ["Correction:Hermes root_cause=bad_schema"],
                "tier": 1,
                "category": "correction",
            }),
            field="facts[0]",
        )

    def test_invalid_category_is_rejected_not_coerced(self):
        self.assert_contract_error(
            "invalid_category",
            lambda: MemoryWrite.from_mapping({
                "domain": "Project",
                "subject": "Alpha",
                "facts": ["state=active"],
                "tier": 2,
                "category": "architecture",
            }),
            field="category",
        )

    def test_label_injection_is_rejected(self):
        self.assert_contract_error(
            "invalid_subject",
            lambda: MemoryWrite.from_mapping({
                "domain": "Project",
                "subject": "Alpha:Injected",
                "facts": ["state=active"],
                "tier": 2,
                "category": "project",
            }),
            field="subject",
        )

    def test_multiple_tier_markers_are_rejected(self):
        self.assert_contract_error(
            "multiple_tier_markers",
            lambda: canonicalize_content(
                "Project:Alpha state=active [Tier=2] [Tier=2]",
                category="project",
            ),
            field="content",
        )

    def test_fact_and_total_density_caps_are_enforced(self):
        base = {
            "domain": "Project",
            "subject": "Alpha",
            "tier": 2,
            "category": "project",
        }
        MemoryWrite.from_mapping({**base, "facts": ["x" * MAX_FACT_CHARS]})

        self.assert_contract_error(
            "fact_too_long",
            lambda: MemoryWrite.from_mapping({
                **base,
                "facts": ["x" * (MAX_FACT_CHARS + 1)],
            }),
            field="facts[0]",
        )
        self.assert_contract_error(
            "facts_too_long",
            lambda: MemoryWrite.from_mapping({
                **base,
                "facts": ["x" * MAX_FACT_CHARS, "y" * MAX_FACT_CHARS, "z"],
            }),
            field="facts",
        )

    def test_fact_count_cap_is_enforced(self):
        self.assert_contract_error(
            "too_many_facts",
            lambda: MemoryWrite.from_mapping({
                "domain": "Project",
                "subject": "Alpha",
                "facts": [f"fact={index}" for index in range(MAX_FACTS + 1)],
                "tier": 2,
                "category": "project",
            }),
            field="facts",
        )

    def test_broad_subject_emits_warning_not_error(self):
        memory = MemoryWrite.from_mapping({
            "domain": "Bodycam",
            "subject": "Bodycam",
            "facts": ["port=7777"],
            "tier": 1,
            "category": "correction",
        })

        warnings = contract_warnings(memory)
        self.assertEqual("overly_broad_subject", warnings[0].code)
        self.assertIn("more specific subject", warnings[0].message)
        self.assertEqual("subject", warnings[0].field)

    def test_fingerprint_ignores_safe_formatting_and_relation_order(self):
        first = MemoryWrite.from_mapping({
            "domain": "Project",
            "subject": "Alpha",
            "facts": ["PORT = 7777"],
            "tier": 2,
            "category": "project",
            "relations": [
                {"type": "uses", "target_id": "b"},
                {"type": "depends", "target_id": "a"},
            ],
        })
        second = MemoryWrite.from_mapping({
            "domain": " Project ",
            "subject": "Alpha",
            "facts": ["port=7777"],
            "tier": 2,
            "category": "project",
            "relations": [
                {"type": "depends", "target_id": "a"},
                {"type": "uses", "target_id": "b"},
            ],
        })

        self.assertEqual(memory_fingerprint(first), memory_fingerprint(second))

    def test_fingerprint_preserves_case_sensitive_claim_values(self):
        upper = MemoryWrite.from_mapping({
            "domain": "Project",
            "subject": "Alpha",
            "facts": ["path=/tmp/Alpha"],
            "tier": 2,
            "category": "project",
        })
        lower = MemoryWrite.from_mapping({
            "domain": "Project",
            "subject": "Alpha",
            "facts": ["PATH = /tmp/alpha"],
            "tier": 2,
            "category": "project",
        })

        self.assertNotEqual(memory_fingerprint(upper), memory_fingerprint(lower))

    def test_write_mode_defaults_to_create_and_rejects_unknown_values(self):
        base = {
            "domain": "Project",
            "subject": "Alpha",
            "facts": ["state=active"],
            "tier": 2,
            "category": "project",
        }
        self.assertEqual("create", MemoryWrite.from_mapping(base).write_mode)
        self.assert_contract_error(
            "invalid_write_mode",
            lambda: MemoryWrite.from_mapping({**base, "write_mode": "overwrite"}),
            field="write_mode",
        )

    def test_patch_requires_id_and_at_least_one_structured_field(self):
        patch = MemoryPatch.from_mapping({"memory_id": "abc", "tier": 1})
        self.assertEqual("abc", patch.memory_id)
        self.assertEqual(1, patch.tier)

        self.assert_contract_error(
            "empty_patch",
            lambda: MemoryPatch.from_mapping({"memory_id": "abc"}),
            field="patch",
        )

    def test_error_shape_is_machine_readable(self):
        try:
            MemoryWrite.from_mapping({
                "domain": "Project",
                "subject": "Alpha",
                "facts": ["state=active"],
                "tier": 9,
                "category": "project",
            })
        except MemoryContractError as error:
            self.assertEqual(
                {"code", "field", "message", "received", "expected"},
                set(error.to_dict()),
            )
            self.assertEqual("invalid_tier", error.to_dict()["code"])
        else:
            self.fail("MemoryContractError was not raised")

    def assert_contract_error(self, code, call, *, field):
        with self.assertRaises(MemoryContractError) as caught:
            call()
        self.assertEqual(code, caught.exception.issue.code)
        self.assertEqual(field, caught.exception.issue.field)


if __name__ == "__main__":
    unittest.main()
