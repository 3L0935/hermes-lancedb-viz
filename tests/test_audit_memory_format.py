import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit-memory-format.py"
SPEC = importlib.util.spec_from_file_location("audit_memory_format", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class MemoryFormatAuditTests(unittest.TestCase):
    def test_report_classifies_canonical_drift_invalid_and_warning_rows(self):
        report = audit.audit_rows([
            {
                "id": "canonical",
                "content": "Project:Alpha port=7777 [Tier=2]",
                "category": "project",
            },
            {
                "id": "drift",
                "content": "Project:Beta  PORT = 7778 [tier=2]",
                "category": "project",
            },
            {
                "id": "invalid",
                "content": "subjectless prose",
                "category": "fact",
            },
            {
                "id": "warning",
                "content": "Hermes:Hermes state=active [Tier=1]",
                "category": "correction",
            },
        ])

        self.assertEqual(2, report["schema_version"])
        self.assertTrue(report["read_only"])
        self.assertEqual({
            "total": 4,
            "canonical": 2,
            "drift": 1,
            "invalid": 1,
            "warning_rows": 1,
        }, report["summary"])
        self.assertEqual(1, report["counts_by_code"]["missing_tier_marker"])
        self.assertEqual(1, report["counts_by_code"]["noncanonical_content"])
        self.assertEqual(1, report["counts_by_code"]["overly_broad_subject"])

    def test_invalid_category_is_reported_without_coercion(self):
        report = audit.audit_rows([{
            "id": "bad-category",
            "content": "Project:Alpha state=active [Tier=2]",
            "category": "made-up",
        }])

        self.assertEqual(1, report["summary"]["invalid"])
        self.assertEqual("invalid_category", report["findings"][0]["error"]["code"])


if __name__ == "__main__":
    unittest.main()
