import importlib.util
import re
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
