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

    def update(self, memory_id, **kwargs):
        self.calls.append((memory_id, kwargs))
        return True


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
            LanceDBStore._embed = lambda _self, _text: np.zeros(768, dtype=np.float32)
            try:
                writer.add("Project:Alpha state=active [Tier=2]", category="project")
                server._store_instance = reader
                self.assertEqual(1, server._compute_stats_fast()["total_memories"])

                reader._db_size = -1
                reader._compute_db_size = lambda: 12345
                self.assertEqual(12345, reader.db_size)
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

    def test_legacy_update_routes_through_store_update(self):
        update_store = FakeUpdateStore()
        server._store_instance = update_store

        result = server.update_memory({
            "memory_id": "memory-1",
            "content": "Project:Alpha port=7778 [Tier=2]",
            "category": "project",
        })

        self.assertEqual({"success": True, "message": "Memory updated"}, result)
        self.assertEqual([
            ("memory-1", {
                "content": "Project:Alpha port=7778 [Tier=2]",
                "category": "project",
            })
        ], update_store.calls)

    def test_server_has_no_direct_table_update_and_graph_uses_memory_endpoint(self):
        source = (ROOT / "server" / "server.py").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()

        self.assertNotIn("store._table.update", source)
        self.assertIn("fetch('/api/memories/' + nodeId", graph)
        self.assertNotIn("fetch('/api/update'", graph)


if __name__ == "__main__":
    unittest.main()
