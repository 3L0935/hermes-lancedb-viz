import importlib.util
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit-memory-format.py"
SPEC = importlib.util.spec_from_file_location("audit_memory_format", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class MemoryFormatAuditTests(unittest.TestCase):
    def test_contract_resolves_when_deployed_beside_the_script(self):
        """The script must load in the deployed layout, where plugin/ does not exist.

        The container and the deployed viz directory hold scripts/ without a sibling
        plugin/, so a single hardcoded ../plugin path raised FileNotFoundError at import
        and took the whole Review page down. This runs the real script from a temporary
        deployment that mimics that layout.
        """
        with tempfile.TemporaryDirectory() as tmp:
            deployed = Path(tmp) / "scripts"
            deployed.mkdir(parents=True)
            shutil.copy(SCRIPT, deployed / "audit-memory-format.py")
            # The contract beside the script, exactly as deploy-local.sh ships it.
            shutil.copy(SCRIPT.parents[1] / "plugin" / "memory_contract.py",
                        deployed / "memory_contract.py")

            spec = importlib.util.spec_from_file_location(
                "audit_deployed", deployed / "audit-memory-format.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            self.assertEqual(deployed / "memory_contract.py", module.CONTRACT_PATH)
            self.assertTrue(callable(module.audit_rows))

    def test_contract_resolution_fails_loudly_instead_of_using_a_missing_path(self):
        """A missing contract must raise a clear error naming what was searched for.

        The old code fell back to a candidate path whether or not it existed, so the
        failure surfaced as a bare "[Errno 2] No such file or directory" pointing at a
        path that could never exist, with no hint that a search had happened at all.
        """
        with mock.patch.object(audit, "_candidate_contract_paths",
                               return_value=[Path("/nonexistent/one.py"),
                                             Path("/nonexistent/two.py")]):
            with self.assertRaises(FileNotFoundError) as ctx:
                audit._find_contract_path()
        message = str(ctx.exception)
        self.assertIn("memory_contract.py", message)
        self.assertIn("/nonexistent/one.py", message)
        self.assertIn("/nonexistent/two.py", message)

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
