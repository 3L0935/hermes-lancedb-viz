import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from plugin.store import LanceDBStore


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate-memory-format.py"
SPEC = importlib.util.spec_from_file_location("migrate_memory_format", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def fake_embed(_self, _text):
    vector = np.zeros(768, dtype=np.float32)
    vector[0] = 1.0
    return vector


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
        ])

        self.assertEqual({
            "canonical": 1,
            "manual_review": 1,
            "safe_normalize": 1,
            "warning_only": 1,
        }, plan["summary"])
        manual = next(row for row in plan["rows"] if row["memory_id"] == "invalid")
        self.assertEqual("missing_tier_marker", manual["error"]["code"])

    def test_work_root_outside_tmp_is_refused(self):
        with self.assertRaisesRegex(ValueError, "child of /tmp"):
            migration.validate_work_root(Path("/home/elo/memory-migration"))

    def test_apply_changes_verified_copy_and_preserves_source_and_backup(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as work_tmp:
            source_path = Path(source_tmp)
            with patch.object(LanceDBStore, "_embed", fake_embed):
                source = LanceDBStore(source_path)
                memory_id = source.add(
                    "Project:Alpha port=7777 [Tier=2]",
                    category="project",
                    legacy=True,
                )
                source._table.update(
                    f"id = '{memory_id}'",
                    {"content": "Project:Alpha  PORT = 7777 [tier=2]"},
                )

                def store_factory(path):
                    store = LanceDBStore(path)
                    store._embed = lambda _text: fake_embed(store, _text)
                    return store

                report = migration.run_migration(
                    source_path,
                    Path(work_tmp),
                    apply=True,
                    store_factory=store_factory,
                )

            self.assertTrue(report["copy_verified"])
            self.assertTrue(report["backup"]["backup_verified"])
            self.assertFalse(report["source_mutated"])
            self.assertEqual(1, report["apply"]["updated"])
            self.assertEqual([], report["apply"]["errors"])

            source_rows = migration.audit.read_rows(source_path)
            copy_rows = migration.audit.read_rows(Path(report["working_copy"]))
            backup_rows = migration.audit.read_rows(Path(report["backup"]["backup_path"]))
            self.assertIn("  PORT = ", source_rows[0]["content"])
            self.assertEqual("Project:Alpha port=7777 [Tier=2]", copy_rows[0]["content"])
            self.assertIn("  PORT = ", backup_rows[0]["content"])


if __name__ == "__main__":
    unittest.main()
