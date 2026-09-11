import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from plugin.memory_contract import MemoryContractError, MemoryPatch, MemoryWrite
from plugin.store import (
    LanceDBStore,
    MemoryEmbeddingError,
    extract_claims,
    route_search_mode,
)


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
        return self.store.add(content, category=category, legacy=True, **kwargs)

    def structured(self, **overrides) -> MemoryWrite:
        values = {
            "domain": "Project",
            "subject": "Alpha",
            "facts": ["state=active"],
            "tier": 2,
            "category": "project",
        }
        values.update(overrides)
        return MemoryWrite.from_mapping(values)

    def test_list_queries_select_columns_without_vector(self):
        self.add("Project:Alpha state=active [Tier=2]")
        original_search = self.store._table.search
        selected_columns = []

        def tracked_search(*args, **kwargs):
            query = original_search(*args, **kwargs)
            original_select = query.select

            def tracked_select(columns):
                selected_columns.append(tuple(columns))
                return original_select(columns)

            query.select = tracked_select
            return query

        with patch.object(self.store._table, "search", side_effect=tracked_search):
            listed = self.store.get_all()
            filtered = self.store.get_by_filters(category="project", limit=20)

        self.assertEqual(1, len(listed))
        self.assertEqual(1, filtered["total"])
        self.assertGreaterEqual(len(selected_columns), 2)
        self.assertTrue(all("vector" not in columns for columns in selected_columns))
        self.assertTrue(all("content" in columns for columns in selected_columns))

    def test_reopen_reuses_current_fts_index_without_version_commit(self):
        self.add("Project:Alpha state=active [Tier=2]")
        self.store._ensure_fts_index(self.store._table)
        indexed_version = self.store._table.version
        index = next(
            item for item in self.store._table.list_indices()
            if item.index_type == "FTS" and item.columns == ["content"]
        )
        self.assertEqual(self.store.count(), index.num_indexed_rows)
        self.assertEqual(0, index.num_unindexed_rows)

        reopened = LanceDBStore(Path(self.tmp.name))

        self.assertEqual(indexed_version, reopened._table.version)

    def test_reopen_rebuilds_fts_index_when_rows_are_unindexed(self):
        self.add("Project:Alpha state=active [Tier=2]")
        stale_version = self.store._table.version
        stale_index = next(
            item for item in self.store._table.list_indices()
            if item.index_type == "FTS" and item.columns == ["content"]
        )
        self.assertGreater(stale_index.num_unindexed_rows, 0)

        reopened = LanceDBStore(Path(self.tmp.name))
        rebuilt_index = next(
            item for item in reopened._table.list_indices()
            if item.index_type == "FTS" and item.columns == ["content"]
        )

        self.assertGreater(reopened._table.version, stale_version)
        self.assertEqual(reopened.count(), rebuilt_index.num_indexed_rows)
        self.assertEqual(0, rebuilt_index.num_unindexed_rows)

    def test_direct_raw_add_is_fenced_from_cron_style_bypass(self):
        with self.assertRaises(MemoryContractError) as caught:
            self.store.add("garbage copied from cron")

        self.assertEqual("legacy_api_disabled", caught.exception.issue.code)
        self.assertEqual(0, self.store.count())

    def test_explicit_legacy_add_still_validates_format(self):
        memory_id = self.store.add(
            "Project:Alpha state=active [Tier=2]",
            category="project",
            legacy=True,
        )
        self.assertIsNotNone(self.store._get_by_id_raw(memory_id))

        with self.assertRaises(MemoryContractError):
            self.store.add("subjectless prose", legacy=True)

    def test_direct_dataclass_construction_cannot_bypass_validation(self):
        invalid = MemoryWrite(
            domain="Project",
            subject="Alpha",
            facts=("nested [Tier=1] marker",),
            tier=2,
            category="project",
        )

        with self.assertRaises(MemoryContractError) as caught:
            self.store.add_memory(invalid)

        self.assertEqual("nested_tier_marker", caught.exception.issue.code)
        self.assertEqual(0, self.store.count())

    def test_structured_add_is_idempotent_before_embedding(self):
        calls = []
        self.store._embed = lambda content: calls.append(content) or fake_embed(self.store, content)
        memory = self.structured()

        created = self.store.add_memory(memory)
        repeated = self.store.add_memory(memory)

        self.assertEqual("created", created["status"])
        self.assertEqual("idempotent", repeated["status"])
        self.assertEqual(created["memory_id"], repeated["memory_id"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, self.store.count())

    def test_two_stores_serialize_preflight_and_commit_in_one_process(self):
        second_store = LanceDBStore(Path(self.tmp.name))
        first_checked = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        results = []
        errors = []
        original_preflight = self.store._preflight_memory_write

        def paused_preflight(memory, *, exclude_id=""):
            result = original_preflight(memory, exclude_id=exclude_id)
            first_checked.set()
            if not release_first.wait(2):
                raise TimeoutError("test did not release first writer")
            return result

        self.store._preflight_memory_write = paused_preflight

        def write(store, finished=None):
            try:
                results.append(store.add_memory(self.structured()))
            except Exception as error:
                errors.append(error)
            finally:
                if finished:
                    finished.set()

        first = threading.Thread(target=write, args=(self.store,))
        second = threading.Thread(target=write, args=(second_store, second_finished))
        first.start()
        self.assertTrue(first_checked.wait(2))
        second.start()
        second_finished.wait(0.2)
        release_first.set()
        first.join(2)
        second.join(2)

        self.assertEqual([], errors)
        self.assertEqual(["created", "idempotent"], sorted(
            (result["status"] for result in results),
            key=lambda status: status != "created",
        ))
        self.assertEqual(1, self.store.count())

    def test_same_subject_new_details_suggest_update_before_embedding(self):
        calls = []
        self.store._embed = lambda content: calls.append(content) or fake_embed(self.store, content)
        created = self.store.add_memory(self.structured())

        suggested = self.store.add_memory(self.structured(facts=["owner=elo"]))

        self.assertEqual("update_suggested", suggested["status"])
        self.assertFalse(suggested["success"])
        self.assertEqual(created["memory_id"], suggested["memory_id"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, self.store.count())

    def test_conflicting_claims_block_before_embedding(self):
        calls = []
        self.store._embed = lambda content: calls.append(content) or fake_embed(self.store, content)
        self.store.add_memory(self.structured(facts=["port=7777"]))

        with self.assertRaises(MemoryContractError) as caught:
            self.store.add_memory(self.structured(facts=["port=7778"]))

        self.assertEqual("conflicting_claims", caught.exception.issue.code)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, self.store.count())

    def test_embedding_failure_is_retryable_and_writes_no_zero_vector(self):
        self.store._embed = lambda _content: np.zeros(768, dtype=np.float32)

        with self.assertRaises(MemoryEmbeddingError) as caught:
            self.store.add_memory(self.structured())

        self.assertTrue(caught.exception.retryable)
        self.assertEqual("embedding_failed", caught.exception.issue.code)
        self.assertEqual(0, self.store.count())

    def test_explicit_upsert_echoes_replaced_content(self):
        created = self.store.add_memory(self.structured(facts=["state=active"]))
        replacement = self.structured(
            facts=["state=active owner=elo"],
            write_mode="upsert_subject",
        )

        result = self.store.add_memory(replacement)

        self.assertEqual("updated", result["status"])
        self.assertEqual(created["memory_id"], result["memory_id"])
        self.assertEqual("Project:Alpha state=active [Tier=2]", result["replaced_content"])
        self.assertEqual(1, self.store.count())

    def test_upsert_subject_does_not_override_conflicting_claims(self):
        created = self.store.add_memory(self.structured(facts=["port=7777"]))

        with self.assertRaises(MemoryContractError) as caught:
            self.store.add_memory(self.structured(
                facts=["port=7778"],
                write_mode="upsert_subject",
            ))

        self.assertEqual("conflicting_claims", caught.exception.issue.code)
        self.assertEqual(
            "Project:Alpha port=7777 [Tier=2]",
            self.store._get_by_id_raw(created["memory_id"])["content"],
        )

    def test_overly_broad_subject_is_returned_as_warning(self):
        result = self.store.add_memory(self.structured(subject="Project"))

        self.assertEqual("created", result["status"])
        self.assertEqual("overly_broad_subject", result["warnings"][0]["code"])

    def test_structured_update_reembeds_and_raw_update_is_fenced(self):
        created = self.store.add_memory(self.structured())
        memory_id = created["memory_id"]

        with self.assertRaises(MemoryContractError):
            self.store.update(memory_id, content="Project:Alpha owner=elo [Tier=2]")
        with self.assertRaises(MemoryContractError):
            self.store.update(memory_id, legacy=True, content="subjectless prose")

        result = self.store.update_memory(MemoryPatch.from_mapping({
            "memory_id": memory_id,
            "facts": ["state=active owner=elo"],
        }))

        self.assertEqual("updated", result["status"])
        self.assertEqual(
            "Project:Alpha state=active owner=elo [Tier=2]",
            self.store._get_by_id_raw(memory_id)["content"],
        )

    def test_canonically_unchanged_update_is_idempotent_before_embedding(self):
        memory = self.structured()
        created = self.store.add_memory(memory)
        memory_id = created["memory_id"]
        calls = []
        self.store._embed = lambda content: calls.append(content) or fake_embed(self.store, content)
        version_before = self.store._table.version

        result = self.store.update_memory(MemoryPatch.from_mapping({
            "memory_id": memory_id,
            "domain": memory.domain,
            "subject": memory.subject,
            "facts": list(memory.facts),
            "tier": memory.tier,
            "category": memory.category,
            "relations": [relation.to_dict() for relation in memory.relations],
        }))

        self.assertEqual("idempotent", result["status"])
        self.assertEqual(memory_id, result["memory_id"])
        self.assertEqual([], calls)
        self.assertEqual(version_before, self.store._table.version)

    def test_category_only_update_accepts_memory_with_two_long_facts(self):
        memory = self.structured(
            subject="LongFacts",
            facts=["a" * 600, "b" * 600],
        )
        memory_id = self.store.add_memory(memory)["memory_id"]

        result = self.store.update_memory(MemoryPatch.from_mapping({
            "memory_id": memory_id,
            "category": "insight",
        }))

        self.assertEqual("updated", result["status"])
        self.assertEqual("insight", self.store._get_by_id_raw(memory_id)["category"])

    def test_quality_and_access_updates_do_not_reembed(self):
        memory_id = self.store.add_memory(self.structured())["memory_id"]
        original_vector = np.array(self.store._get_by_id_raw(memory_id)["vector"])
        calls = []
        self.store._embed = lambda content: calls.append(content) or fake_embed(self.store, content)

        updated = self.store.update_memory(MemoryPatch.from_mapping({
            "memory_id": memory_id,
            "quality": 0.8,
        }))
        accessed = self.store.get_by_id(memory_id)

        self.assertEqual("updated", updated["status"])
        self.assertEqual([], calls)
        self.assertEqual(1, accessed["access_count"])
        np.testing.assert_array_equal(
            original_vector,
            np.array(self.store._get_by_id_raw(memory_id)["vector"]),
        )

    def test_stale_filter_uses_same_recalculated_quality_as_detail_reads(self):
        memory_id = self.store.add_memory(self.structured())["memory_id"]
        old = 1_600_000_000.0
        self.store._table.update(
            f"id = '{memory_id}'",
            {"created_at": old, "accessed_at": old, "quality": 0.5},
        )

        stale = self.store.get_stale(days=90, quality_max=0.3)

        self.assertEqual([memory_id], [memory["id"] for memory in stale])
        self.assertLessEqual(stale[0]["quality"], 0.3)
        self.assertEqual(0.5, stale[0]["persisted_quality"])

    def test_content_update_preserves_relations_when_patch_omits_them(self):
        target_id = self.add("Project:Target state=active [Tier=2]")
        source_id = self.add(
            "Project:Source state=active [Tier=2]",
            relations=[{"type": "depends", "target_id": target_id}],
        )

        self.store.update_memory(MemoryPatch.from_mapping({
            "memory_id": source_id,
            "facts": ["state=ready"],
        }))

        edges = [edge for edge in self.store.get_typed_edges() if edge["from"] == source_id]
        self.assertEqual([target_id], [edge["to"] for edge in edges])

    def test_portable_import_rejects_malformed_content(self):
        result = self.store.import_records([{
            "id": "aaaaaaaa-aaa",
            "content": "subjectless prose",
            "category": "fact",
        }])

        self.assertEqual(0, result["imported"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual("missing_tier_marker", result["errors"][0]["error"]["code"])
        self.assertEqual(0, self.store.count())

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
                legacy=True,
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
        self.assertEqual("lexical", route_search_mode("12345678-123"))
        self.assertEqual(
            "lexical",
            route_search_mode("12345678-1234-1234-1234-123456789abc"),
        )
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

    def test_search_rejects_zero_embedding_before_vector_query(self):
        self.add("Project:Alpha state=active [Tier=2]")
        vector_searches = []
        original_search = self.store._table.search

        def tracked_search(*args, **kwargs):
            if kwargs.get("query_type") == "hybrid":
                vector_searches.append(True)
            return original_search(*args, **kwargs)

        self.store._embed = lambda _query: np.zeros(768, dtype=np.float32)
        with patch.object(self.store._table, "search", side_effect=tracked_search):
            with self.assertRaises(MemoryEmbeddingError) as caught:
                self.store.search("semantic query", mode="hybrid")

        self.assertEqual("embedding_failed", caught.exception.issue.code)
        self.assertEqual("query", caught.exception.issue.field)
        self.assertEqual([], vector_searches)

    def test_search_normalizes_embedding_before_vector_query(self):
        vector_norms = []
        original_search = self.store._table.search

        def tracked_search(*args, **kwargs):
            query = original_search(*args, **kwargs)
            if kwargs.get("query_type") != "hybrid":
                return query
            original_vector = query.vector

            def tracked_vector(vector):
                vector_norms.append(float(np.linalg.norm(vector)))
                return original_vector(vector)

            query.vector = tracked_vector
            return query

        self.store._embed = lambda _query: np.full(768, 3.0, dtype=np.float32)
        with patch.object(self.store._table, "search", side_effect=tracked_search):
            self.store.search("semantic query", mode="hybrid")

        self.assertEqual(1, len(vector_norms))
        self.assertAlmostEqual(1.0, vector_norms[0], places=6)

    def test_generated_and_uuid_ids_use_exact_id_lookup_without_embedding(self):
        generated_id = self.add("Project:Generated state=active [Tier=2]")
        uuid_id = "12345678-1234-1234-1234-123456789abc"
        imported = self.store.import_records([{
            "id": uuid_id,
            "content": "Project:Imported state=active [Tier=2]",
            "category": "project",
        }])
        self.assertEqual(1, imported["imported"])
        self.store._embed = lambda _query: (_ for _ in ()).throw(
            AssertionError("exact ID lookup must not embed")
        )

        for memory_id in (generated_id, uuid_id):
            with self.subTest(memory_id=memory_id):
                results = self.store.search(memory_id, mode="auto")
                self.assertEqual([memory_id], [result["id"] for result in results])
                self.assertEqual("id", results[0]["match_field"])

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

        self.store.update(
            second_id,
            legacy=True,
            content="Project:Alpha port=7777 [Tier=2]",
        )

        self.assertEqual([], self.store.get_conflicts(status="open"))
        self.assertEqual(1, len(self.store.get_conflicts(status="resolved")))

    def test_category_update_closes_or_detects_conflicts(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")

        self.assertTrue(self.store.update(second_id, legacy=True, category="insight"))
        self.assertEqual([], self.store.get_conflicts(status="open"))

        self.assertTrue(self.store.update(second_id, legacy=True, category="project"))
        self.assertEqual(1, len(self.store.get_conflicts(status="open")))

    def test_update_reports_partial_commit_after_conflict_detection_failure(self):
        memory_id = self.store.add_memory(self.structured())["memory_id"]
        self.store.detect_conflicts_for = lambda _memory_id: (_ for _ in ()).throw(
            RuntimeError("ledger unavailable")
        )

        with self.assertRaises(MemoryContractError) as caught:
            self.store.update_memory(MemoryPatch.from_mapping({
                "memory_id": memory_id,
                "facts": ["state=ready"],
            }))

        issue = caught.exception.issue
        self.assertEqual("update_partially_committed", issue.code)
        self.assertEqual("update", issue.field)
        self.assertEqual("partial", issue.received["commit_state"])
        self.assertIn("memories.content", issue.received["committed"])
        self.assertEqual("memory_conflicts.detect", issue.received["failed_step"])
        self.assertEqual(
            "Project:Alpha state=ready [Tier=2]",
            self.store._get_by_id_raw(memory_id)["content"],
        )

    def test_update_reports_nothing_committed_when_precommit_step_fails(self):
        memory_id = self.store.add_memory(self.structured())["memory_id"]
        self.store._close_conflicts_for_memory = lambda _memory_id: False

        with self.assertRaises(MemoryContractError) as caught:
            self.store.update_memory(MemoryPatch.from_mapping({
                "memory_id": memory_id,
                "facts": ["state=ready"],
            }))

        issue = caught.exception.issue
        self.assertEqual("update_not_committed", issue.code)
        self.assertEqual("none", issue.received["commit_state"])
        self.assertEqual([], issue.received["committed"])
        self.assertEqual(
            "Project:Alpha state=active [Tier=2]",
            self.store._get_by_id_raw(memory_id)["content"],
        )

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
            legacy=True,
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
            legacy=True,
            relations=[{"type": "depends", "target_id": gamma_id}],
        ))

    def test_reintroduced_claim_conflict_reopens_ledger_record(self):
        self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")
        self.store.update(
            second_id,
            legacy=True,
            content="Project:Alpha port=7777 [Tier=2]",
        )
        self.store.update(
            second_id,
            legacy=True,
            content="Project:Alpha port=7779 [Tier=2]",
        )

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

    def test_claim_parser_accepts_spacing_case_and_typed_values(self):
        self.assertEqual(
            {
                "port": "7777",
                "ratio": "1",
                "enabled": "true",
                "owner": "élo user",
            },
            extract_claims(
                'Project:Alpha PORT = 07777 ratio=1.00 enabled = TRUE owner="Élo   User"'
            ),
        )

    def test_formatting_only_claim_differences_do_not_conflict(self):
        self.add('Project:Alpha PORT=07777 ratio=1.0 owner="Élo User" [Tier=2]')
        self.add('Project:Alpha port = 7777 RATIO = 1.00 owner="élo   user" [Tier=2]')

        self.assertEqual([], self.store.get_conflicts(status="open"))

    def test_claim_key_casing_still_detects_a_real_contradiction(self):
        self.add("Project:Alpha PORT=7777 [Tier=2]")
        self.add("Project:Alpha port=7778 [Tier=2]")

        conflicts = self.store.get_conflicts(status="open")
        self.assertEqual(1, len(conflicts))
        self.assertEqual("port", conflicts[0]["claim_key"])

    def test_conflict_registry_consolidates_duplicate_logical_rows(self):
        first_id = self.add("Project:Alpha port=7777 [Tier=2]")
        second_id = self.add("Project:Alpha port=7778 [Tier=2]")
        conflict = self.store.get_conflicts(status="open")[0]
        duplicate = {
            key: value for key, value in conflict.items()
            if key not in {"memory_a_content", "memory_b_content"}
        }
        duplicate["id"] = "duplicate-id"
        self.store._ensure_conflicts_table().add([duplicate])

        self.store._conflicts_schema_checked = False
        conflicts = self.store.get_conflicts()

        self.assertEqual(1, len(conflicts))
        self.assertEqual(
            {first_id, second_id},
            {conflicts[0]["memory_a_id"], conflicts[0]["memory_b_id"]},
        )

    def test_resolved_conflict_overflow_is_archived_without_losing_audit(self):
        pairs = []
        for index in range(3):
            first_id = self.add(f"Project:Archive{index} port=7777 [Tier=2]")
            second_id = self.add(f"Project:Archive{index} port=7778 [Tier=2]")
            conflict_id = self.store.get_conflicts(status="open", memory_id=second_id)[0]["id"]
            pairs.append((first_id, second_id, conflict_id))

        with patch("plugin.store.CONFLICT_REGISTRY_RESOLVED_LIMIT", 2):
            for _, _, conflict_id in pairs:
                self.assertTrue(self.store.resolve_conflict(
                    conflict_id,
                    resolution_note=f"reviewed {conflict_id}",
                    resolved_by="elo",
                ))

        self.assertEqual(2, len(self.store.get_conflicts(status="resolved")))
        self.assertEqual(1, len(self.store.get_conflicts(status="archived")))
        exported = self.store.get_all_conflict_records(include_archived=True)
        self.assertEqual(3, len(exported))
        self.assertTrue(any(float(row.get("archived_at") or 0) > 0 for row in exported))

        archived_pair = next(
            (first_id, second_id) for first_id, second_id, conflict_id in pairs
            if any(row["id"] == conflict_id and row.get("archived_at") for row in exported)
        )
        self.store.detect_conflicts_for(archived_pair[1])
        self.assertEqual([], self.store.get_conflicts(status="open", memory_id=archived_pair[1]))

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
