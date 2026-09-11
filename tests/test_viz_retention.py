import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from plugin.store import LanceDBStore


ROOT = Path(__file__).resolve().parents[1]
MEMORY_ID = "aaaaaaaa-aaa"
CONFLICT_ID = "cccccccc-ccc"
SPEC = importlib.util.spec_from_file_location("lancedb_viz_server", ROOT / "server" / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class FakeStore:
    def __init__(self):
        self.calls = []

    def get_conflicts(self, status="", limit=100, memory_id=""):
        self.calls.append((status, limit, memory_id))
        return [{"id": CONFLICT_ID, "status": status or "open"}]

    def resolve_conflict(self, conflict_id, resolution_note, resolved_by="user"):
        self.calls.append((conflict_id, resolution_note, resolved_by))
        return True

    def search_with_diagnostics(self, query, top_k=20, diagnostics=False, **_kwargs):
        self.calls.append(("search", top_k, diagnostics))
        return {
            "success": True, "count": 0, "results": [], "route": "hybrid",
            "degraded": False, "degraded_reason": "", "abstained": True,
            "abstention_reason": "below_calibrated_evidence",
            "message": "aucun résultat fiable",
            "timings": {"embedding_ms": 0.0, "search_ms": 1.0},
        }


class FakeUpdateStore:
    def __init__(self):
        self.calls = []

    def _get_by_id_raw(self, memory_id):
        return {
            "id": memory_id,
            "content": "Project:Alpha port=7777 [Tier=2]",
            "category": "project",
            "relations": [],
            "updated_at": 10.0,
        }

    def get_by_id(self, memory_id):
        return self._get_by_id_raw(memory_id)

    def update_memory(self, memory_patch):
        self.calls.append(memory_patch)
        return {
            "success": True,
            "canonical_content": "Project:Alpha port=7778 [Tier=2]",
            "replaced_content": "Project:Alpha port=7777 [Tier=2]",
        }


class FakeQuery:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def select(self, columns):
        self.calls.append(("select", tuple(columns)))
        self.rows = [{key: row.get(key) for key in columns} for row in self.rows]
        return self

    def limit(self, limit):
        self.calls.append(("limit", limit))
        self.rows = self.rows[:limit]
        return self

    def to_list(self):
        return self.rows


class FakeReviewTable:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def search(self):
        return FakeQuery(list(self.rows), self.calls)


class FakeReviewStore:
    def __init__(self):
        self.calls = []
        self._table = FakeReviewTable([
            {"id": "aaaaaaaa-aaa", "content": "Project:Alpha state = active [Tier=2]", "category": "project", "vector": [1.0, 0.0]},
            {"id": "bbbbbbbb-bbb", "content": "Project:Beta state=active [Tier=2]", "category": "project", "vector": [0.999, 0.01]},
        ], self.calls)

    def get_conflicts(self, status="", limit=100, memory_id=""):
        return [{"id": CONFLICT_ID, "memory_a_id": MEMORY_ID, "memory_b_id": "bbbbbbbb-bbb", "claim_key": "port"}]

    def get_typed_edges(self, include_unresolved=False):
        return [{"from": MEMORY_ID, "to": "", "relation_type": "depends", "target_label": "Project:Missing"}]


class VizRetentionTests(unittest.TestCase):
    def setUp(self):
        self.previous = server._store_instance
        self.store = FakeStore()
        server._store_instance = self.store

    def tearDown(self):
        server._store_instance = self.previous

    def test_conflicts_api_returns_flat_rows_with_filters(self):
        result = server.api_get_conflicts({
            "status": "open", "limit": "25", "memory_id": MEMORY_ID
        })

        self.assertEqual([{"id": CONFLICT_ID, "status": "open"}], result)
        self.assertEqual([("open", 25, MEMORY_ID)], self.store.calls)

        archived = server.api_get_conflicts({"status": "archived", "limit": "25"})
        self.assertEqual([{"id": CONFLICT_ID, "status": "archived"}], archived)

    def test_search_api_omits_query_text_and_forwards_diagnostic_opt_in(self):
        result = server.search_memories("private query", top_k=7, diagnostics=True)

        self.assertNotIn("query", result)
        self.assertTrue(result["abstained"])
        self.assertEqual(("search", 7, True), self.store.calls[-1])

    def test_conflict_resolution_api_is_auditable(self):
        result = server.api_resolve_conflict(CONFLICT_ID, {
            "resolution_note": "approved after review",
            "resolved_by": "elo",
        })

        self.assertEqual({"success": True, "conflict_id": CONFLICT_ID}, result)
        self.assertEqual([
            (CONFLICT_ID, "approved after review", "elo")
        ], self.store.calls)
        self.assertEqual(
            CONFLICT_ID,
            server._parse_conflict_resolution_path(f"/api/conflicts/{CONFLICT_ID}/resolve"),
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

    def test_dashboard_stats_selects_columns_without_vector(self):
        original_embed = LanceDBStore._embed
        LanceDBStore._embed = lambda _self, _text: np.pad(
            np.ones(1, dtype=np.float32), (0, 767)
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = LanceDBStore(Path(tmp))
                store.add(
                    "Project:Alpha state=active [Tier=2]",
                    category="project",
                    legacy=True,
                )
                server._store_instance = store
                original_search = store._table.search
                selected_columns = []

                def tracked_search(*args, **kwargs):
                    query = original_search(*args, **kwargs)
                    original_select = query.select

                    def tracked_select(columns):
                        selected_columns.append(tuple(columns))
                        return original_select(columns)

                    query.select = tracked_select
                    return query

                with patch.object(store._table, "search", side_effect=tracked_search):
                    result = server._compute_stats_fast()

                self.assertEqual(1, result["total_memories"])
                self.assertEqual(1, len(selected_columns))
                self.assertNotIn("vector", selected_columns[0])
                self.assertIn("content", selected_columns[0])
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
                        "id": "aaaaaaaa-aaa",
                        "content": "Project:Alpha PORT = 7777 [Tier=2]",
                        "category": "project",
                        "relations": [],
                    },
                    {
                        "id": "bbbbbbbb-bbb",
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
                target_id = "aaaaaaaa-aaa"
                source_id = "bbbbbbbb-bbb"
                base_items = [
                    {"id": target_id, "content": "Project:Target state=active [Tier=2]", "category": "project"},
                    {"id": source_id, "content": "Project:Source state=active [Tier=2]", "category": "project"},
                ]
                store.import_records(base_items)
                server._store_instance = store
                payload = {
                    "memories": [
                        dict(base_items[0], relations=[]),
                        dict(base_items[1], relations=[{
                            "type": "depends",
                            "target_id": target_id,
                            "target": "Project:Target",
                        }]),
                    ],
                    "typed_edges": [{
                        "from": source_id,
                        "to": target_id,
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
            "memory_id": MEMORY_ID,
            "content": "Project:Alpha port=7778 [Tier=2]",
            "category": "project",
        })

        self.assertTrue(result["success"])
        self.assertEqual("Project:Alpha port=7778 [Tier=2]", result["canonical_content"])
        self.assertEqual(1, len(update_store.calls))
        patch = update_store.calls[0]
        self.assertEqual(MEMORY_ID, patch.memory_id)
        self.assertEqual(("port=7778",), patch.facts)

    def test_structured_preview_is_read_only_and_returns_canonical_diff(self):
        update_store = FakeUpdateStore()
        server._store_instance = update_store

        result = server.api_preview_memory_update(MEMORY_ID, {
            "domain": "Project",
            "subject": "Alpha",
            "facts": ["port = 7778", "owner=elo"],
            "tier": 1,
            "category": "project",
            "relations": [{"type": "depends", "target_id": "bbbbbbbb-bbb"}],
            "base_updated_at": 10.0,
        })

        self.assertTrue(result["success"], result)
        self.assertEqual("Project:Alpha port=7778 ::fact:: owner=elo [Tier=1]", result["canonical_content"])
        self.assertEqual("Project:Alpha port=7777 [Tier=2]", result["previous_content"])
        self.assertEqual(10.0, result["base_updated_at"])
        self.assertEqual([], update_store.calls)

    def test_structured_update_rejects_visible_version_conflict_before_write(self):
        update_store = FakeUpdateStore()
        server._store_instance = update_store

        result = server.api_update_memory(MEMORY_ID, {
            "domain": "Project", "subject": "Alpha", "facts": ["port=7778"],
            "tier": 2, "category": "project", "relations": [],
            "base_updated_at": 9.0,
        })

        self.assertEqual("version_conflict", result["code"])
        self.assertEqual(10.0, result["current_updated_at"])
        self.assertEqual([], update_store.calls)

    def test_structured_editor_contract_exposes_all_bounded_fields_and_diff(self):
        html = (ROOT / "static" / "index.html").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()

        for field in ("edit-domain", "edit-subject", "edit-facts", "edit-tier", "edit-category", "edit-relations"):
            self.assertIn(field, graph)
        self.assertIn("/preview", graph)
        self.assertIn("canonical-preview", graph)
        self.assertIn("version-conflict", graph)
        self.assertIn("MAX_EDITOR_FACTS = 12", graph)
        self.assertIn("MAX_EDITOR_RELATIONS = 20", graph)
        self.assertIn('id="graph-canvas"', html)

    def test_review_inbox_is_on_demand_projected_bounded_and_reasoned(self):
        review_store = FakeReviewStore()
        server._store_instance = review_store

        result = server.api_get_review_inbox()

        reasons = {finding["reason"] for finding in result["findings"]}
        self.assertEqual({"format", "contradiction", "broken_reference", "near_duplicate"}, reasons)
        self.assertLessEqual(len(result["findings"]), server.REVIEW_MAX_FINDINGS)
        self.assertEqual(2000, result["budgets"]["projected_rows"])
        self.assertEqual(500, result["budgets"]["vector_rows"])
        selected = [call[1] for call in review_store.calls if call[0] == "select"]
        self.assertIn(("id", "content", "category"), selected)
        self.assertIn(("id", "content", "category", "vector"), selected)
        self.assertTrue(all("created_at" not in columns for columns in selected))

    def test_review_page_requires_manual_scan_and_never_labels_age_false(self):
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()

        self.assertIn('data-page="review"', html)
        self.assertIn('id="review-scan"', html)
        self.assertIn("async function loadReviewInbox()", app)
        self.assertNotIn("name === 'review') loadReviewInbox()", app)
        self.assertIn("Older does not mean false", html)

    def test_graph_requires_selection_instead_of_building_global_matrix(self):
        with patch.object(server, "_compute_vector_data", side_effect=AssertionError("global scan")):
            result = server.get_graph_data()

        self.assertTrue(result["selection_required"])
        self.assertEqual([], result["nodes"])
        self.assertEqual([], result["edges"])

    def test_neighborhood_graph_is_bounded_and_distinguishes_edge_kinds(self):
        center = {"id": MEMORY_ID, "content": "Project:Center state=active [Tier=2]", "category": "project"}
        rows = [center] + [
            {"id": f"{index:08x}-aaa", "content": f"Project:N{index} state=active [Tier=2]", "category": "project", "_distance": 0.01 + index / 1000}
            for index in range(1, 8)
        ]
        typed = [
            {"from": MEMORY_ID, "to": rows[1]["id"], "relation_type": "depends"},
            {"from": MEMORY_ID, "to": rows[2]["id"], "relation_type": "uses"},
        ]

        result = server._build_neighborhood_graph(
            center, rows, typed, {"depends"}, threshold=0.8,
            node_budget=4, edge_budget=3,
        )

        self.assertLessEqual(len(result["nodes"]), 4)
        self.assertLessEqual(len(result["edges"]), 3)
        self.assertEqual({"declared", "semantic"}, {edge["kind"] for edge in result["edges"]})
        self.assertEqual(1, result["hidden_by_relation_filter"])
        self.assertGreater(result["hidden_neighbor_count"], 0)
        self.assertEqual({"nodes": 4, "edges": 3, "semantic_candidates": 30}, result["budgets"])

    def test_graph_ui_loads_selected_neighborhood_and_pauses_off_screen_physics(self):
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()

        self.assertIn('id="relation-filter"', html)
        self.assertIn('id="hidden-neighbor-count"', html)
        self.assertIn("memory_id=' + encodeURIComponent(selectedNodeId)", graph)
        self.assertIn("kind: 'declared'", graph)
        self.assertIn("kind: 'semantic'", graph)
        self.assertIn("new IntersectionObserver", graph)
        self.assertIn("network.stopSimulation()", graph)
        self.assertIn("pauseGraphPhysics", app)

    def test_server_has_no_direct_table_update_and_graph_uses_memory_endpoint(self):
        source = (ROOT / "server" / "server.py").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()

        self.assertNotIn("store._table.update", source)
        self.assertIn("fetch('/api/memories/' + nodeId", graph)
        self.assertNotIn("fetch('/api/update'", graph)

    def test_database_text_is_escaped_before_html_sinks(self):
        app = (ROOT / "static" / "app.js").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()

        for unsafe in (
            "+ (m.content || '').substring",
            "+ (m.content||'').substring",
            "' + (m.content || '') + '",
            "' + e + '</span>",
            "' + t + '</span>",
            "tooltip.innerHTML = (found.content",
        ):
            self.assertNotIn(unsafe, app)
        for unsafe in (
            "' + data.error + '",
            "' + e + '</span>",
            "' + item.type + '</span>",
        ):
            self.assertNotIn(unsafe, graph)
        self.assertIn("tooltip.textContent", app)
        self.assertIn("function safeCategory", graph)


if __name__ == "__main__":
    unittest.main()
