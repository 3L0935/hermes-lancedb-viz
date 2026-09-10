import json
import unittest

from plugin import LanceDBMemoryProvider


class FakeStore:
    def __init__(self):
        self.added = None

    def add(self, content, category="fact", relations=None, warnings=None):
        self.added = (content, category, relations)
        return "new-id"

    def get_conflicts(self, status="", limit=100, memory_id=""):
        rows = [{"id": "conflict-1", "status": "open", "memory_b_id": "new-id"}]
        if memory_id and memory_id != "new-id":
            return []
        return rows[:limit]


class ProviderRetentionTests(unittest.TestCase):
    def setUp(self):
        self.provider = LanceDBMemoryProvider()
        self.provider._store = FakeStore()

    def test_conflicts_tool_is_registered_and_lists_rows(self):
        names = [schema["name"] for schema in self.provider.get_tool_schemas()]
        self.assertIn("lancedb_conflicts", names)

        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_conflicts", {"status": "open", "limit": 10}
        ))

        self.assertEqual(1, payload["count"])
        self.assertEqual("conflict-1", payload["conflicts"][0]["id"])

    def test_add_returns_new_conflicts_for_immediate_visibility(self):
        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_add",
            {
                "domain": "Project",
                "subject": "Alpha",
                "tier": "2",
                "content": "port=7778",
                "category": "project",
            },
        ))

        self.assertTrue(payload["success"])
        self.assertEqual(1, len(payload["potential_conflicts"]))
        self.assertEqual("new-id", self.provider._store.added[0] and "new-id")

    def test_add_returns_warning_without_failing_when_conflict_listing_fails(self):
        self.provider._store.get_conflicts = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("ledger unavailable")
        )

        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_add",
            {
                "domain": "Project",
                "subject": "Alpha",
                "tier": "2",
                "content": "port=7777",
                "category": "project",
            },
        ))

        self.assertTrue(payload["success"])
        self.assertEqual([], payload["potential_conflicts"])
        self.assertEqual(1, len(payload["warnings"]))
        self.assertIn("ledger unavailable", payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
