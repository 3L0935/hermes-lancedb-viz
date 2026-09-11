import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from plugin.memory_contract import MemoryContractError, MemoryWrite
from plugin.store import LanceDBStore


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lancedb_security_server", ROOT / "server" / "server.py"
)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def fake_embed(_store, _content):
    return np.pad(np.ones(1, dtype=np.float32), (0, 767))


class RecordingDeleteStore:
    def __init__(self):
        self.deleted = []

    def delete(self, memory_id):
        self.deleted.append(memory_id)
        return True

    def bulk_delete(self, memory_ids):
        self.deleted.extend(memory_ids)
        return {"deleted": len(memory_ids), "errors": []}


class StoreSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.embed_patch = patch.object(LanceDBStore, "_embed", fake_embed)
        self.embed_patch.start()
        self.store = LanceDBStore(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.embed_patch.stop()
        self.tmp.cleanup()

    def add_memory(self, index=0):
        return self.store.add_memory(MemoryWrite.from_mapping({
            "domain": "Project",
            "subject": f"Memory{index}",
            "facts": ["state=active"],
            "tier": 2,
            "category": "project",
        }))["memory_id"]

    def test_delete_rejects_predicate_id_without_mutating_table(self):
        for index in range(5):
            self.add_memory(index)

        with self.assertRaises(MemoryContractError) as caught:
            self.store.delete("absent' OR '1'='1")

        self.assertEqual("invalid_memory_id", caught.exception.issue.code)
        self.assertEqual(5, self.store.count())

    def test_bulk_delete_validates_entire_id_list_before_deleting(self):
        memory_id = self.add_memory()

        with self.assertRaises(MemoryContractError) as caught:
            self.store.bulk_delete([memory_id, "absent' OR '1'='1"])

        self.assertEqual("invalid_memory_id", caught.exception.issue.code)
        self.assertEqual(1, self.store.count())

    def test_import_rejects_invalid_memory_and_relation_ids_before_writing(self):
        result = self.store.import_records([
            {
                "id": "absent' OR '1'='1",
                "content": "Project:Invalid state=active [Tier=2]",
                "category": "project",
            },
            {
                "id": "aaaaaaaa-aaa",
                "content": "Project:Relation state=active [Tier=2]",
                "category": "project",
                "relations": [{
                    "type": "depends",
                    "target_id": "absent' OR '1'='1",
                }],
            },
        ])

        self.assertEqual(0, result["imported"])
        self.assertEqual(2, result["skipped"])
        self.assertEqual(
            ["invalid_memory_id", "invalid_memory_id"],
            [error["error"]["code"] for error in result["errors"]],
        )
        self.assertEqual(0, self.store.count())

    def test_category_filter_treats_predicate_text_as_a_literal(self):
        self.add_memory()

        results = self.store._search_lexical(
            "state", category="project' OR '1'='1"
        )

        self.assertEqual([], results)

    def test_update_entry_rejects_predicate_id_before_lookup(self):
        self.add_memory()

        with self.assertRaises(MemoryContractError) as caught:
            self.store.update_entities("absent' OR '1'='1", ["safe"])

        self.assertEqual("invalid_memory_id", caught.exception.issue.code)
        self.assertEqual(1, self.store.count())

    def test_uuid_import_and_generated_id_are_accepted(self):
        generated_id = self.add_memory()
        uuid_id = "12345678-1234-1234-1234-123456789abc"

        result = self.store.import_records([{
            "id": uuid_id,
            "content": "Project:Uuid state=active [Tier=2]",
            "category": "project",
        }])

        self.assertRegex(generated_id, r"^[0-9a-f]{8}-[0-9a-f]{3}$")
        self.assertEqual(1, result["imported"])
        self.assertEqual(uuid_id, self.store.get_by_id(uuid_id)["id"])

    def test_delete_aborts_when_id_is_not_unique(self):
        memory_id = self.add_memory()
        stored_row = self.store._table.search().where(
            f"id = '{memory_id}'"
        ).limit(1).to_list()[0]
        self.store._table.add([stored_row])

        with self.assertRaises(MemoryContractError) as caught:
            self.store.delete(memory_id)

        self.assertEqual("non_unique_memory_id", caught.exception.issue.code)
        self.assertEqual(2, self.store.count())


class ServerIdBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.previous_store = server._store_instance
        self.store = RecordingDeleteStore()
        server._store_instance = self.store

    def tearDown(self):
        server._store_instance = self.previous_store

    def test_delete_route_rejects_predicate_id_before_store_call(self):
        result = server.delete_memory("absent' OR '1'='1")

        self.assertEqual("invalid_memory_id", result["code"])
        self.assertEqual([], self.store.deleted)

    def test_bulk_delete_route_rejects_whole_invalid_list(self):
        result = server.api_bulk_delete({
            "memory_ids": ["aaaaaaaa-aaa", "absent' OR '1'='1"]
        })

        self.assertEqual("invalid_memory_id", result["code"])
        self.assertEqual([], self.store.deleted)

    def test_path_routes_accept_only_canonical_ids(self):
        uuid_id = "12345678-1234-1234-1234-123456789abc"

        self.assertEqual(
            ("aaaaaaaa-aaa", ""),
            server._parse_memories_id_path("/api/memories/aaaaaaaa-aaa"),
        )
        self.assertEqual(
            (uuid_id, "access"),
            server._parse_memories_id_path(f"/api/memories/{uuid_id}/access"),
        )
        self.assertIsNone(server._parse_memories_id_path("/api/memories/not-an-id"))

    def test_import_route_rejects_invalid_ids_before_store_call(self):
        result = server.import_memories({"memories": [{
            "id": "absent' OR '1'='1",
            "content": "Project:Invalid state=active [Tier=2]",
        }]})

        self.assertEqual("invalid_memory_id", result["code"])
        self.assertEqual([], self.store.deleted)


if __name__ == "__main__":
    unittest.main()
