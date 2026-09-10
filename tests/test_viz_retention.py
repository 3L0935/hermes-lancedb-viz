import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np

from plugin.store import LanceDBStore


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lancedb_viz_server", ROOT / "server" / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class FakeStore:
    def __init__(self):
        self.calls = []

    def get_conflicts(self, status="", limit=100, memory_id=""):
        self.calls.append((status, limit, memory_id))
        return [{"id": "conflict-1", "status": status or "open"}]

    def resolve_conflict(self, conflict_id, resolution_note, resolved_by="user"):
        self.calls.append((conflict_id, resolution_note, resolved_by))
        return True


class FakeUpdateStore:
    def __init__(self):
        self.calls = []

    def _get_by_id_raw(self, memory_id):
        return {"id": memory_id, "category": "project"}

    def update_memory(self, memory_patch):
        self.calls.append(memory_patch)
        return {
            "success": True,
            "canonical_content": "Project:Alpha port=7778 [Tier=2]",
            "replaced_content": "Project:Alpha port=7777 [Tier=2]",
        }


class VizRetentionTests(unittest.TestCase):
    def setUp(self):
        self.previous = server._store_instance
        self.store = FakeStore()
        server._store_instance = self.store

    def tearDown(self):
        server._store_instance = self.previous

    def test_conflicts_api_returns_flat_rows_with_filters(self):
        result = server.api_get_conflicts({
            "status": "open", "limit": "25", "memory_id": "memory-1"
        })

        self.assertEqual([{"id": "conflict-1", "status": "open"}], result)
        self.assertEqual([("open", 25, "memory-1")], self.store.calls)

        archived = server.api_get_conflicts({"status": "archived", "limit": "25"})
        self.assertEqual([{"id": "conflict-1", "status": "archived"}], archived)

    def test_conflict_resolution_api_is_auditable(self):
        result = server.api_resolve_conflict("conflict-1", {
            "resolution_note": "approved after review",
            "resolved_by": "elo",
        })

        self.assertEqual({"success": True, "conflict_id": "conflict-1"}, result)
        self.assertEqual([
            ("conflict-1", "approved after review", "elo")
        ], self.store.calls)
        self.assertEqual(
            "conflict-1",
            server._parse_conflict_resolution_path("/api/conflicts/conflict-1/resolve"),
        )

    def test_stats_refreshes_mvcc_snapshot_and_db_size_is_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = LanceDBStore(Path(tmp))
            writer = LanceDBStore(Path(tmp))
            original_embed = LanceDBStore._embed
            LanceDBStore._embed = lambda _self, _text: np.pad(
                np.ones(1, dtype=np.float32), (0, 767)
            )
            try:
                writer.add(
                    "Project:Alpha state=active [Tier=2]",
                    category="project",
                    legacy=True,
                )
                server._store_instance = reader
                self.assertEqual(1, server._compute_stats_fast()["total_memories"])

                reader._db_size = -1
                reader._compute_db_size = lambda: 12345
                self.assertEqual(12345, reader.db_size)
            finally:
                LanceDBStore._embed = original_embed

    def test_export_import_round_trip_preserves_relations_and_conflict_audit(self):
        original_embed = LanceDBStore._embed
        LanceDBStore._embed = lambda _self, _text: np.pad(
            np.ones(1, dtype=np.float32), (0, 767)
        )
        try:
            with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as dest_tmp:
                source = LanceDBStore(Path(source_tmp))
                target_id = source.add("Project:Target state=active [Tier=2]", legacy=True)
                source_id = source.add(
                    "Project:Source state=active [Tier=2]",
                    relations=[
                        {"type": "depends", "target_id": target_id},
                        {"type": "uses", "target": "Project:Missing"},
                    ],
                    legacy=True,
                )
                source.add("Project:Claim port=7777 [Tier=2]", legacy=True)
                claim_id = source.add("Project:Claim port=7778 [Tier=2]", legacy=True)
                conflict_id = source.get_conflicts(status="open")[0]["id"]
                source.resolve_conflict(conflict_id, "7778 approved", "elo")
                server._store_instance = source

                payload = server.export_memories()

                self.assertEqual(1, len(payload["conflicts"]))
                destination = LanceDBStore(Path(dest_tmp))
                server._store_instance = destination
                result = server.import_memories(payload)
                self.assertTrue(result["success"], result)

                restored = LanceDBStore(Path(dest_tmp))
                edges = [
                    edge for edge in restored.get_typed_edges(include_unresolved=True)
                    if edge["from"] == source_id
                ]
                self.assertEqual(2, len(edges))
                self.assertEqual({target_id, ""}, {edge["to"] for edge in edges})
                conflicts = restored.get_conflicts(status="resolved", memory_id=claim_id)
                self.assertEqual(1, len(conflicts))
                self.assertEqual("human", conflicts[0]["resolution_type"])
                self.assertEqual("7778 approved", conflicts[0]["resolution_note"])
                self.assertEqual("elo", conflicts[0]["resolved_by"])
        finally:
            LanceDBStore._embed = original_embed

    def test_import_rebuilds_conflicts_when_legacy_export_has_no_registry(self):
        original_embed = LanceDBStore._embed
        LanceDBStore._embed = lambda _self, _text: np.pad(
            np.ones(1, dtype=np.float32), (0, 767)
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server._store_instance = LanceDBStore(Path(tmp))
                result = server.import_memories({"memories": [
                    {
                        "id": "claim-a",
                        "content": "Project:Alpha PORT = 7777 [Tier=2]",
                        "category": "project",
                        "relations": [],
                    },
                    {
                        "id": "claim-b",
                        "content": "Project:Alpha port=7778 [Tier=2]",
                        "category": "project",
                        "relations": [],
                    },
                ]})

                self.assertTrue(result["success"], result)
                restored = LanceDBStore(Path(tmp))
                self.assertEqual(1, len(restored.get_conflicts(status="open")))
        finally:
            LanceDBStore._embed = original_embed

    def test_export_uses_typed_edge_table_as_relation_source_of_truth(self):
        original_embed = LanceDBStore._embed
        LanceDBStore._embed = lambda _self, _text: np.pad(
            np.ones(1, dtype=np.float32), (0, 767)
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                source = LanceDBStore(Path(tmp))
                target_id = source.add("Project:Target state=active [Tier=2]", legacy=True)
                source_id = source.add("Project:Source state=active [Tier=2]", legacy=True)
                source._ensure_edges_table().add([{
                    "source_id": source_id,
                    "relation_type": "depends",
                    "target_id": target_id,
                    "target_label": "Project:Target",
                    "created_at": 12.0,
                }])
                server._store_instance = source

                payload = server.export_memories()

                exported_source = next(
                    row for row in payload["memories"] if row["id"] == source_id
                )
                self.assertEqual([{
                    "type": "depends",
                    "target_id": target_id,
                    "target": "Project:Target",
                }], exported_source["relations"])
                self.assertEqual(1, len(payload["typed_edges"]))
        finally:
            LanceDBStore._embed = original_embed

    def test_import_rerun_repairs_relations_after_partial_run(self):
        original_embed = LanceDBStore._embed
        LanceDBStore._embed = lambda _self, _text: np.pad(
            np.ones(1, dtype=np.float32), (0, 767)
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = LanceDBStore(Path(tmp))
                base_items = [
                    {"id": "target", "content": "Project:Target state=active [Tier=2]", "category": "project"},
                    {"id": "source", "content": "Project:Source state=active [Tier=2]", "category": "project"},
                ]
                store.import_records(base_items)
                server._store_instance = store
                payload = {
                    "memories": [
                        dict(base_items[0], relations=[]),
                        dict(base_items[1], relations=[{
                            "type": "depends",
                            "target_id": "target",
                            "target": "Project:Target",
                        }]),
                    ],
                    "typed_edges": [{
                        "from": "source",
                        "to": "target",
                        "relation_type": "depends",
                        "target_label": "Project:Target",
                        "created_at": 1.0,
                    }],
                }

                result = server.import_memories(payload)

                self.assertTrue(result["success"], result)
                self.assertEqual(0, result["imported"])
                self.assertEqual(2, result["skipped"])
                restored = LanceDBStore(Path(tmp))
                self.assertEqual(1, len(restored.get_typed_edges()))
        finally:
            LanceDBStore._embed = original_embed

    def test_conflicts_page_contract_is_wired(self):
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()

        self.assertIn('data-page="conflicts"', html)
        self.assertIn('id="page-conflicts"', html)
        self.assertIn('id="conflict-status"', html)
        self.assertIn("async function loadConflicts()", app)
        self.assertIn("API + '/conflicts?'", app)
        self.assertIn("name === 'conflicts'", app)

    def test_typed_highlight_reset_defines_shadow_size(self):
        graph = (ROOT / "static" / "graph.js").read_text()
        match = re.search(
            r"function resetTypedHighlights\(\) \{(?P<body>.*?)\n\}",
            graph,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertRegex(match.group("body"), r"\bageShadow\s*=")

    def test_legacy_update_routes_through_structured_store_update(self):
        update_store = FakeUpdateStore()
        server._store_instance = update_store

        result = server.update_memory({
            "memory_id": "memory-1",
            "content": "Project:Alpha port=7778 [Tier=2]",
            "category": "project",
        })

        self.assertTrue(result["success"])
        self.assertEqual("Project:Alpha port=7778 [Tier=2]", result["canonical_content"])
        self.assertEqual(1, len(update_store.calls))
        patch = update_store.calls[0]
        self.assertEqual("memory-1", patch.memory_id)
        self.assertEqual(("port=7778",), patch.facts)

    def test_server_has_no_direct_table_update_and_graph_uses_memory_endpoint(self):
        source = (ROOT / "server" / "server.py").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()

        self.assertNotIn("store._table.update", source)
        self.assertIn("fetch('/api/memories/' + nodeId", graph)
        self.assertNotIn("fetch('/api/update'", graph)


if __name__ == "__main__":
    unittest.main()
