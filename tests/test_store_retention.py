import tempfile
import unittest
from pathlib import Path

import numpy as np

from plugin.store import LanceDBStore, route_search_mode


def fake_embed(_self, text: str) -> np.ndarray:
    vector = np.zeros(768, dtype=np.float32)
    tokens = {token.lower().strip(".,:=") for token in text.split()}
    for token in tokens:
        vector[hash(token) % 768] += 1.0
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


class StoreRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LanceDBStore(Path(self.tmp.name))
        self.original_embed = LanceDBStore._embed
        LanceDBStore._embed = fake_embed

    def tearDown(self):
        LanceDBStore._embed = self.original_embed
        self.tmp.cleanup()

    def add(self, content: str, category: str = "project", **kwargs) -> str:
        return self.store.add(content, category=category, **kwargs)

    def test_relation_label_resolves_to_unique_target_id(self):
        target_id = self.add("Project:Alpha state=active [Tier=2]")
        source_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[{"type": "depends", "target": "Project:Alpha"}],
        )

        edges = self.store.get_typed_edges()

        self.assertEqual(1, len(edges))
        self.assertEqual(source_id, edges[0]["from"])
        self.assertEqual(target_id, edges[0]["to"])
        self.assertEqual("Project:Alpha", edges[0]["target_label"])

    def test_relation_update_replaces_old_edges(self):
        alpha_id = self.add("Project:Alpha state=active [Tier=2]")
        gamma_id = self.add("Project:Gamma state=active [Tier=2]")
        source_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[{"type": "depends", "target_id": alpha_id}],
        )

        self.assertTrue(
            self.store.update(
                source_id,
                relations=[{"type": "depends", "target_id": gamma_id}],
            )
        )

        edges = [edge for edge in self.store.get_typed_edges() if edge["from"] == source_id]
        self.assertEqual(1, len(edges))
        self.assertEqual(gamma_id, edges[0]["to"])

    def test_delete_removes_incoming_and_outgoing_edges(self):
        alpha_id = self.add("Project:Alpha state=active [Tier=2]")
        beta_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[{"type": "depends", "target_id": alpha_id}],
        )
        self.add(
            "Project:Gamma state=active [Tier=2]",
            relations=[{"type": "depends", "target_id": beta_id}],
        )

        self.assertTrue(self.store.delete(beta_id))

        edges = self.store.get_typed_edges(include_unresolved=True)
        self.assertFalse(any(edge["from"] == beta_id or edge["to"] == beta_id for edge in edges))

    def test_router_is_local_and_deterministic(self):
        self.assertEqual("lexical", route_search_mode('"exact phrase"'))
        self.assertEqual("graph", route_search_mode("Comment Project:Beta dépend de Project:Alpha ?"))
        self.assertEqual("hybrid", route_search_mode("configuration audio de mon PC"))

    def test_graph_search_expands_one_hop_with_direct_result_first(self):
        alpha_id = self.add("Project:Alpha state=active [Tier=2]")
        beta_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[{"type": "depends", "target_id": alpha_id}],
        )
        beta = self.store._get_by_id_raw(beta_id)
        beta.pop("vector", None)
        beta["score"] = 0.03
        self.store._search_hybrid = lambda query, top_k, category: [beta]

        results = self.store.search(
            "Comment Project:Beta dépend de Project:Alpha ?",
            top_k=2,
            mode="graph",
            relation_depth=1,
        )

        self.assertEqual([beta_id, alpha_id], [result["id"] for result in results])
        self.assertEqual("direct", results[0]["retrieval_source"])
        self.assertEqual("relation", results[1]["retrieval_source"])
        self.assertEqual("depends", results[1]["relation_type"])

    def test_graph_search_respects_total_result_limit(self):
        alpha_id = self.add("Project:Alpha state=active [Tier=2]")
        gamma_id = self.add("Project:Gamma state=active [Tier=2]")
        beta_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[
                {"type": "depends", "target_id": alpha_id},
                {"type": "uses", "target_id": gamma_id},
            ],
        )
        beta = self.store._get_by_id_raw(beta_id)
        beta.pop("vector", None)
        beta["score"] = 0.03
        self.store._search_hybrid = lambda query, top_k, category: [beta]

        results = self.store.search("what connects beta", top_k=2, mode="graph")

        self.assertEqual(2, len(results))
        self.assertEqual(beta_id, results[0]["id"])

    def test_legacy_edge_schema_adds_target_id_without_losing_rows(self):
        import pyarrow as pa

        edge_table = self.store._db.create_table(
            "memory_edges",
            schema=pa.schema([
                pa.field("source_id", pa.string()),
                pa.field("relation_type", pa.string()),
                pa.field("target_label", pa.string()),
                pa.field("created_at", pa.float64()),
            ]),
        )
        edge_table.add([{
            "source_id": "legacy-source",
            "relation_type": "depends",
            "target_label": "Project:Alpha",
            "created_at": 1.0,
        }])

        migrated = self.store._ensure_edges_table()

        self.assertIn("target_id", migrated.schema.names)
        rows = migrated.to_arrow().to_pylist()
        self.assertEqual(1, len(rows))
        self.assertIsNone(rows[0]["target_id"])

    def test_ambiguous_label_is_retained_but_not_resolved(self):
        self.add("Project:Alpha state=active [Tier=2]")
        self.add("Project:Alpha owner=elo [Tier=2]")
        source_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[{"type": "depends", "target": "Project:Alpha"}],
        )

        edges = self.store.get_typed_edges(include_unresolved=True)

        edge = next(edge for edge in edges if edge["from"] == source_id)
        self.assertEqual("", edge["to"])
        self.assertEqual("Project:Alpha", edge["target_label"])


if __name__ == "__main__":
    unittest.main()
