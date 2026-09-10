import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from plugin.store import LanceDBStore


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_graph_retention.py"
SPEC = importlib.util.spec_from_file_location("migrate_graph_retention", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class GraphMigrationTests(unittest.TestCase):
    @staticmethod
    def fake_embed(_self, _text):
        vector = np.zeros(768, dtype=np.float32)
        vector[0] = 1.0
        return vector

    def test_resolution_plan_handles_exact_short_ambiguous_and_orphaned_edges(self):
        memories = [
            {"id": "a", "content": "Project:Alpha state=active [Tier=2]"},
            {"id": "b", "content": "Hermes:MemoryWriting state=active [Tier=2]"},
            {"id": "c", "content": "Docs:Shared state=active [Tier=3]"},
            {"id": "d", "content": "Project:Shared state=active [Tier=3]"},
        ]
        edges = [
            {"source_id": "a", "relation_type": "depends", "target_label": "Hermes:MemoryWriting", "target_id": None},
            {"source_id": "a", "relation_type": "uses", "target_label": "MemoryWriting", "target_id": None},
            {"source_id": "a", "relation_type": "uses", "target_label": "Shared", "target_id": None},
            {"source_id": "missing", "relation_type": "depends", "target_label": "Project:Alpha", "target_id": None},
        ]

        planned, summary = migration.plan_edge_resolution(memories, edges)

        self.assertEqual("b", planned[0]["target_id"])
        self.assertEqual("b", planned[1]["target_id"])
        self.assertEqual("", planned[2]["target_id"])
        self.assertEqual("orphaned_source", planned[3]["resolution"])
        self.assertEqual({"resolved": 2, "ambiguous_or_missing": 1, "orphaned_source": 1}, summary)

    def test_apply_clears_invalid_unresolved_target_id_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with patch.object(LanceDBStore, "_embed", self.fake_embed):
                store = LanceDBStore(path)
                source_id = store.add("Project:Source state=active [Tier=2]", legacy=True)
                store._ensure_edges_table().add([{
                    "source_id": source_id,
                    "relation_type": "depends",
                    "target_id": "deleted-id",
                    "target_label": "Project:Missing",
                    "created_at": 1.0,
                }])

                first = migration.migrate(path, apply=True)
                first_rows = store.get_typed_edges(include_unresolved=True)
                second = migration.migrate(path, apply=True)
                second_rows = store.get_typed_edges(include_unresolved=True)

            self.assertEqual(1, first["ambiguous_or_missing"])
            self.assertEqual("", first_rows[0]["to"])
            self.assertEqual(first_rows, second_rows)
            self.assertEqual(0, second["conflicts_created_or_reopened"])

    def test_apply_can_resume_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with patch.object(LanceDBStore, "_embed", self.fake_embed):
                store = LanceDBStore(path)
                target_id = store.add("Project:Target state=active [Tier=2]", legacy=True)
                source_id = store.add("Project:Source state=active [Tier=2]", legacy=True)
                store._ensure_edges_table().add([{
                    "source_id": source_id,
                    "relation_type": "depends",
                    "target_id": "",
                    "target_label": "Project:Target",
                    "created_at": 1.0,
                }])

                with patch.object(
                    LanceDBStore,
                    "detect_conflicts_for",
                    side_effect=RuntimeError("interrupted"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "interrupted"):
                        migration.migrate(path, apply=True)

                migration.migrate(path, apply=True)
                rows = store.get_typed_edges(include_unresolved=True)

            self.assertEqual(target_id, rows[0]["to"])

    def test_rerun_does_not_reopen_human_resolved_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with patch.object(LanceDBStore, "_embed", self.fake_embed):
                store = LanceDBStore(path)
                store.add(
                    "Project:Alpha port=7777 [Tier=2]",
                    category="project",
                    legacy=True,
                )
                store.add(
                    "Project:Alpha port=7778 [Tier=2]",
                    category="project",
                    legacy=True,
                )
                conflict = store.get_conflicts(status="open")[0]
                store._ensure_conflicts_table().update(
                    f"id = '{conflict['id']}'",
                    {"status": "resolved", "resolved_at": 1.0},
                )

                migration.migrate(path, apply=True)
                migration.migrate(path, apply=True)

                self.assertEqual([], store.get_conflicts(status="open"))
                self.assertEqual(1, len(store.get_conflicts(status="resolved")))


if __name__ == "__main__":
    unittest.main()
