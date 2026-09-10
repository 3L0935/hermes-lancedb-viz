import json
import unittest

from plugin import LanceDBMemoryProvider
from plugin.memory_contract import MemoryContractError, MemoryPatch, MemoryWrite
from plugin.store import MemoryEmbeddingError


class RecordingStore:
    def __init__(self):
        self.added = None
        self.updated = None
        self.add_error = None

    def add_memory(self, memory):
        self.added = memory
        if self.add_error:
            raise self.add_error
        return {
            "success": True,
            "status": "created",
            "memory_id": "memory-1",
            "canonical_content": "Project:Alpha port=7777 [Tier=2]",
            "warnings": [],
        }

    def update_memory(self, patch):
        self.updated = patch
        return {
            "success": True,
            "status": "updated",
            "memory_id": patch.memory_id,
            "canonical_content": "Project:Alpha port=7778 [Tier=2]",
            "replaced_content": "Project:Alpha port=7777 [Tier=2]",
            "warnings": [],
        }

    def get_conflicts(self, **_kwargs):
        return []


class ProviderFormattingTests(unittest.TestCase):
    def setUp(self):
        self.provider = LanceDBMemoryProvider()
        self.store = RecordingStore()
        self.provider._store = self.store

    def schema(self, name):
        return next(
            schema for schema in self.provider.get_tool_schemas()
            if schema["name"] == name
        )["parameters"]

    def valid_write(self, **overrides):
        request = {
            "domain": "Project",
            "subject": "Alpha",
            "facts": ["port=7777"],
            "tier": 2,
            "category": "project",
        }
        request.update(overrides)
        return request

    def test_add_and_update_schemas_are_closed_and_structured(self):
        expected = {"domain", "subject", "facts", "tier", "category"}
        add = self.schema("lancedb_add")
        update = self.schema("lancedb_update")

        self.assertEqual(expected, set(add["required"]))
        self.assertEqual(expected | {"memory_id"}, set(update["required"]))
        self.assertFalse(add["additionalProperties"])
        self.assertFalse(update["additionalProperties"])
        self.assertNotIn("content", add["properties"])
        self.assertNotIn("content", update["properties"])
        self.assertEqual("integer", add["properties"]["tier"]["type"])
        self.assertEqual(12, add["properties"]["facts"]["maxItems"])

    def test_add_handler_uses_same_contract_and_echoes_canonical_content(self):
        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_add", self.valid_write()
        ))

        self.assertIsInstance(self.store.added, MemoryWrite)
        self.assertEqual("create", self.store.added.write_mode)
        self.assertTrue(payload["success"])
        self.assertEqual("memory-1", payload["memory_id"])
        self.assertEqual(
            "Project:Alpha port=7777 [Tier=2]",
            payload["canonical_content"],
        )

    def test_invalid_category_returns_machine_readable_error_without_store_call(self):
        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_add", self.valid_write(category="made-up")
        ))

        self.assertFalse(payload["success"])
        self.assertFalse(payload["retryable"])
        self.assertEqual("invalid_category", payload["error"]["code"])
        self.assertEqual("category", payload["error"]["field"])
        self.assertIsNone(self.store.added)

    def test_missing_required_field_returns_contract_error(self):
        request = self.valid_write()
        request.pop("facts")

        payload = json.loads(self.provider.handle_tool_call("lancedb_add", request))

        self.assertEqual("required_field", payload["error"]["code"])
        self.assertEqual("facts", payload["error"]["field"])

    def test_embedding_failure_is_marked_retryable(self):
        self.store.add_error = MemoryEmbeddingError("ollama unavailable")

        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_add", self.valid_write()
        ))

        self.assertFalse(payload["success"])
        self.assertTrue(payload["retryable"])
        self.assertEqual("embedding_failed", payload["error"]["code"])

    def test_update_handler_uses_typed_patch_and_echoes_replaced_content(self):
        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_update",
            {"memory_id": "memory-1", **self.valid_write(facts=["port=7778"])},
        ))

        self.assertIsInstance(self.store.updated, MemoryPatch)
        self.assertIsNone(self.store.updated.relations)
        self.assertTrue(payload["success"])
        self.assertEqual(
            "Project:Alpha port=7777 [Tier=2]",
            payload["replaced_content"],
        )

    def test_update_handler_enforces_complete_structured_shape(self):
        payload = json.loads(self.provider.handle_tool_call(
            "lancedb_update", {"memory_id": "memory-1", "facts": ["port=7778"]}
        ))

        self.assertFalse(payload["success"])
        self.assertEqual("required_field", payload["error"]["code"])
        self.assertEqual("domain", payload["error"]["field"])
        self.assertIsNone(self.store.updated)


if __name__ == "__main__":
    unittest.main()
