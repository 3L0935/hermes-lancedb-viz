import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_graph_retention.py"
SPEC = importlib.util.spec_from_file_location("migrate_graph_retention", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class GraphMigrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
