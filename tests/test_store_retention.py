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

    def test_router_markers_require_word_boundaries(self):
        self.assertEqual("hybrid", route_search_mode("an inexact result"))
        self.assertEqual("hybrid", route_search_mode("an unrelated topic"))
        self.assertEqual("lexical", route_search_mode("an exact result"))
        self.assertEqual("graph", route_search_mode("a related topic"))

    def test_empty_lexical_branch_falls_back_to_hybrid(self):
        hybrid = [{"id": "memory-1", "score": 0.4}]
        self.store._search_lexical = lambda query, top_k, category: []
        self.store._search_hybrid = lambda query, top_k, category: hybrid

        results = self.store.search("exact missing phrase", top_k=3, mode="auto")

        self.assertEqual(["memory-1"], [result["id"] for result in results])
        self.assertEqual("hybrid", results[0]["search_mode"])
        self.assertEqual("lexical_empty", results[0]["routing_fallback"])

    def test_graph_depth_zero_uses_full_top_k_for_seeds(self):
        requested = []
        self.store._search_hybrid = lambda query, top_k, category: requested.append(top_k) or []

        self.store.search("related projects", top_k=10, mode="graph", relation_depth=0)

        self.assertEqual([10], requested)

    def test_relation_expansion_order_is_stable(self):
        direct = [{"id": "seed", "score": 1.0}]
        self.store.get_typed_edges = lambda: [
            {"from": "seed", "to": "z", "relation_type": "uses", "target_label": "Z", "created_at": 2.0},
            {"from": "seed", "to": "a", "relation_type": "depends", "target_label": "A", "created_at": 1.0},
        ]
        self.store._get_by_id_raw = lambda memory_id: {
            "id": memory_id, "content": memory_id, "category": "project"
        }

        first = self.store._expand_relation_context(direct, 3)
        self.store.get_typed_edges = lambda: list(reversed([
            {"from": "seed", "to": "z", "relation_type": "uses", "target_label": "Z", "created_at": 2.0},
            {"from": "seed", "to": "a", "relation_type": "depends", "target_label": "A", "created_at": 1.0},
        ]))
        second = self.store._expand_relation_context(direct, 3)

        self.assertEqual(["seed", "a", "z"], [row["id"] for row in first])
        self.assertEqual([row["id"] for row in first], [row["id"] for row in second])

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

    def test_conflicting_explicit_claims_are_recorded_once(self):
        first_id = self.add("Project:Alpha port=7777 state=active [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 state=active [Tier=2]")

        conflicts = self.store.get_conflicts(status="open")

        self.assertEqual(1, len(conflicts))
        self.assertEqual({first_id, second_id}, {conflicts[0]["memory_a_id"], conflicts[0]["memory_b_id"]})
        self.assertEqual("port", conflicts[0]["claim_key"])
        self.assertEqual({"7777", "7778"}, {conflicts[0]["value_a"], conflicts[0]["value_b"]})
        self.assertEqual("open", conflicts[0]["status"])
        self.store.detect_conflicts_for(second_id)
        self.assertEqual(1, len(self.store.get_conflicts(status="open")))

    def test_add_succeeds_and_reports_warning_when_conflict_detection_fails(self):
        warnings = []
        self.store.detect_conflicts_for = lambda _memory_id: (_ for _ in ()).throw(
            RuntimeError("ledger unavailable")
        )

        memory_id = self.add(
            "Project:Alpha port=7777 [Tier=2]",
            warnings=warnings,
        )

        self.assertIsNotNone(self.store._get_by_id_raw(memory_id))
        self.assertEqual(1, self.store.count())
        self.assertEqual(1, len(warnings))
        self.assertIn("ledger unavailable", warnings[0])

    def test_updating_claims_closes_stale_conflicts_and_rechecks(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")
        self.assertEqual(1, len(self.store.get_conflicts(status="open")))

        self.store.update(second_id, content="Project:Alpha port=7777 [Tier=2]")

        self.assertEqual([], self.store.get_conflicts(status="open"))
        self.assertEqual(1, len(self.store.get_conflicts(status="resolved")))

    def test_category_update_closes_or_detects_conflicts(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")

        self.assertTrue(self.store.update(second_id, category="insight"))
        self.assertEqual([], self.store.get_conflicts(status="open"))

        self.assertTrue(self.store.update(second_id, category="project"))
        self.assertEqual(1, len(self.store.get_conflicts(status="open")))

    def test_delete_closes_open_conflicts(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")

        self.assertTrue(self.store.delete(second_id))

        self.assertEqual([], self.store.get_conflicts(status="open"))
        resolved = self.store.get_conflicts(status="resolved")
        self.assertEqual("memory deleted", resolved[0]["resolution_note"])
        self.assertEqual("auto", resolved[0]["resolution_type"])

    def test_conflict_resolution_is_audited_and_not_reopened(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")
        conflict_id = self.store.get_conflicts(status="open")[0]["id"]

        self.assertTrue(self.store.resolve_conflict(
            conflict_id,
            resolution_note="7778 is the approved port",
            resolved_by="elo",
        ))
        self.store.detect_conflicts_for(second_id)

        self.assertEqual([], self.store.get_conflicts(status="open"))
        conflict = self.store.get_conflicts(status="resolved")[0]
        self.assertEqual("human", conflict["resolution_type"])
        self.assertEqual("7778 is the approved port", conflict["resolution_note"])
        self.assertEqual("elo", conflict["resolved_by"])

    def test_delete_aborts_when_edge_cleanup_fails(self):
        memory_id = self.add("Project:Alpha state=active [Tier=2]")
        self.store._cleanup_edges_for_memory = lambda _memory_id: False

        self.assertFalse(self.store.delete(memory_id))
        self.assertIsNotNone(self.store._get_by_id_raw(memory_id))

    def test_relation_replace_failure_preserves_previous_edge(self):
        alpha_id = self.add("Project:Alpha state=active [Tier=2]")
        gamma_id = self.add("Project:Gamma state=active [Tier=2]")
        source_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[{"type": "depends", "target_id": alpha_id}],
        )
        actual = self.store._ensure_edges_table()

        class FailingMergeTable:
            def __getattr__(self, name):
                return getattr(actual, name)

            def merge_insert(self, _keys):
                class Builder:
                    def when_matched_update_all(inner_self):
                        return inner_self

                    def when_not_matched_insert_all(inner_self):
                        return inner_self

                    def when_not_matched_by_source_delete(inner_self, _condition):
                        return inner_self

                    def execute(inner_self, _rows):
                        raise RuntimeError("interrupted merge")
                return Builder()

        self.store._ensure_edges_table = lambda: FailingMergeTable()

        self.assertFalse(self.store.update(
            source_id,
            relations=[{"type": "depends", "target_id": gamma_id}],
        ))
        edges = [edge for edge in self.store.get_typed_edges() if edge["from"] == source_id]
        self.assertEqual([alpha_id], [edge["to"] for edge in edges])

    def test_relation_replace_verifies_postcondition(self):
        alpha_id = self.add("Project:Alpha state=active [Tier=2]")
        gamma_id = self.add("Project:Gamma state=active [Tier=2]")
        source_id = self.add(
            "Project:Beta state=active [Tier=2]",
            relations=[{"type": "depends", "target_id": alpha_id}],
        )
        actual = self.store._ensure_edges_table()

        class NoopMergeTable:
            def __getattr__(self, name):
                return getattr(actual, name)

            def merge_insert(self, _keys):
                class Builder:
                    def when_matched_update_all(inner_self):
                        return inner_self

                    def when_not_matched_insert_all(inner_self):
                        return inner_self

                    def when_not_matched_by_source_delete(inner_self, _condition):
                        return inner_self

                    def execute(inner_self, _rows):
                        return None
                return Builder()

        self.store._ensure_edges_table = lambda: NoopMergeTable()

        self.assertFalse(self.store.update(
            source_id,
            relations=[{"type": "depends", "target_id": gamma_id}],
        ))

    def test_reintroduced_claim_conflict_reopens_ledger_record(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")
        self.store.update(second_id, content="Project:Alpha port=7777 [Tier=2]")
        self.store.update(second_id, content="Project:Alpha port=7779 [Tier=2]")

        self.assertEqual(1, len(self.store.get_conflicts(status="open")))
        self.assertEqual("7779", self.store.get_conflicts(status="open")[0]["value_b"])

    def test_human_resolved_conflict_is_never_reopened(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")
        conflict = self.store.get_conflicts(status="open")[0]
        table = self.store._ensure_conflicts_table()
        table.update(
            f"id = '{conflict['id']}'",
            {"status": "resolved", "resolved_at": 1.0},
        )

        self.store.detect_conflicts_for(second_id)

        self.assertEqual([], self.store.get_conflicts(status="open"))
        self.assertEqual(1, len(self.store.get_conflicts(status="resolved")))

    def test_unrelated_subjects_do_not_create_conflicts(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        self.add("Project:Beta port=7778 [Tier=2]")
        self.assertEqual([], self.store.get_conflicts())

    def test_equal_claim_values_do_not_create_conflicts(self):
        self.add("Project:Alpha port=7777 owner=elo [Tier=2]")
        self.add("Project:Alpha port=7777 state=active [Tier=2]")
        self.assertEqual([], self.store.get_conflicts())

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

    def test_unique_short_subject_label_resolves(self):
        target_id = self.add("Hermes:MemoryWriting state=active [Tier=2]")
        source_id = self.add(
            "Project:Alpha state=active [Tier=2]",
            relations=[{"type": "depends", "target": "MemoryWriting"}],
        )

        edge = self.store.get_typed_edges()[0]
        self.assertEqual(source_id, edge["from"])
        self.assertEqual(target_id, edge["to"])

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
