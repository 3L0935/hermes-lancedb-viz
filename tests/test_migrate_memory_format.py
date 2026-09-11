import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate-memory-format.py"
SPEC = importlib.util.spec_from_file_location("migrate_memory_format", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class MemoryFormatMigrationTests(unittest.TestCase):
    def test_classification_only_auto_plans_safe_normalization(self):
        plan = migration.classify_rows([
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
            {
                "id": "duplicate-tier",
                "content": "Project:Gamma state=active [Tier=2] [Tier=2]",
                "category": "project",
            },
            {
                "id": "duplicate-wrapper",
                "content": "Project:Delta Project:Delta state=active [Tier=2]",
                "category": "project",
            },
        ])

        self.assertEqual({
            "canonical": 1,
            "quarantine": 1,
            "auto_fix": 3,
            "warning_only": 1,
        }, plan["summary"])
        manual = next(row for row in plan["rows"] if row["memory_id"] == "invalid")
        self.assertEqual("missing_tier_marker", manual["error"]["code"])

    def test_cli_has_no_apply_or_write_helpers(self):
        with self.assertRaises(SystemExit):
            migration.build_parser().parse_args(["--apply"])
        self.assertFalse(hasattr(migration, "apply_plan"))
        self.assertFalse(hasattr(migration, "backup_working_copy"))


if __name__ == "__main__":
    unittest.main()
