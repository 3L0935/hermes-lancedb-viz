import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


class FakeHealthTable:
    def __init__(self, *, version, rows, useful_bytes, fragments, indices=()):
        self.version = version
        self._rows = rows
        self._useful_bytes = useful_bytes
        self._fragments = fragments
        self._indices = indices

    def count_rows(self):
        return self._rows

    def list_versions(self):
        return [{}] * self.version

    def stats(self):
        return SimpleNamespace(total_bytes=self._useful_bytes)

    def to_lance(self):
        return SimpleNamespace(get_fragments=lambda: [object()] * self._fragments)

    def list_indices(self):
        return self._indices


class FakeHealthDatabase:
    def __init__(self, tables):
        self.tables = tables

    def list_tables(self):
        return SimpleNamespace(tables=list(self.tables))

    def open_table(self, name):
        return self.tables[name]


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

    def test_typed_highlight_reset_restores_the_shared_resting_style(self):
        """The reset must restore the same style the node was drawn with.

        This replaces a test that asserted resetTypedHighlights redefined ageShadow
        inline. That inline copy was the bug: it had different freshness buckets from
        renderGraph, so deselecting a node could leave it styled differently from how
        it was originally drawn. Both now call restingNodeStyle().
        """
        graph = (ROOT / "static" / "graph.js").read_text()

        resting = re.search(r"function restingNodeStyle\(n\) \{(?P<body>.*?)\n\}", graph, re.DOTALL)
        self.assertIsNotNone(resting, "restingNodeStyle must exist as the single source of style")
        self.assertRegex(resting.group("body"), r"\bageShadow\s*=")

        match = re.search(r"function resetTypedHighlights\(\) \{(?P<body>.*?)\n\}", graph, re.DOTALL)
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("restingNodeStyle(", body)
        # No second copy of the freshness ladder: divergence is the defect being fixed.
        self.assertNotRegex(body, r"\bageShadow\s*=")

        # renderGraph must use the same helper, or the two can drift apart again.
        render = re.search(r"function renderGraph\(\) \{(?P<body>.*?)\n\}", graph, re.DOTALL)
        self.assertIsNotNone(render)
        self.assertIn("restingNodeStyle", render.group("body"))

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

    def test_graph_without_selection_returns_the_full_bounded_view(self):
        """No selection means the whole graph, which is the intended default.

        This replaces a test that asserted an empty graph. The earlier guard
        existed because building a global matrix was unbounded; the protection is
        kept, but as a cost bound rather than by refusing to render: the default
        view must cover every memory and still stay within the payload budget.
        """
        nodes = [
            {"id": f"a{index:010d}", "label": f"N{index}", "title": f"Fact {index}",
             "category": "fact", "created_at": 0.0, "access_count": 0,
             "entities": [], "relations": [], "tier": "2", "color": "#06b6d4",
             "size": 20, "node_type": "leaf"}
            for index in range(120)
        ]
        edges = [
            {"from": nodes[index]["id"], "to": nodes[index + 1]["id"],
             "label": "0.90", "color": {"color": "#6366f1", "opacity": 0.5}}
            for index in range(119)
        ]
        typed = [
            {"from": nodes[0]["id"], "to": nodes[1]["id"], "relation_type": "depends"}
        ]
        store = SimpleNamespace(get_typed_edges=lambda include_unresolved=False: typed)

        with patch.object(server, "_compute_vector_data",
                          return_value=(nodes, edges, {}, None, [], 0.8)), \
             patch.object(server, "_get_store", return_value=store):
            default_view = server.get_graph_data(threshold=0.8)
            overlay_view = server.get_graph_data(threshold=0.8, show_declared=True)

        self.assertFalse(default_view["selection_required"])
        self.assertEqual(120, len(default_view["nodes"]))
        self.assertTrue(all(edge["kind"] == "semantic" for edge in default_view["edges"]))
        self.assertLess(len(json.dumps(default_view)), 200_000)

        # Declared relations are an opt-in overlay, not part of the default view.
        self.assertEqual(120, len(overlay_view["edges"]))
        self.assertEqual(1, len(overlay_view["typed_edges"]))
        self.assertEqual(["depends"], overlay_view["available_relation_types"])

    def test_graph_default_uses_embedding_links_and_hub_modes_are_opt_in(self):
        """The default layout is embedding similarity; hubs come from `cluster`.

        Typed relations used to be drawn on every graph, which buried the
        embedding structure the page exists to show.
        """
        nodes = [
            {"id": f"b{index:010d}", "label": f"N{index}", "title": f"Fact {index}",
             "category": "tech", "created_at": 0.0, "access_count": 0,
             "entities": ["alpha"], "relations": [], "tier": "2"}
            for index in range(5)
        ]
        raw = {"nodes": nodes, "edges": []}
        hub = {"nodes": nodes + [{"id": "hub:cat_tech", "node_type": "hub",
                                  "label": "Tech", "entities": ["tech"][:1]}],
               "edges": [{"from": "hub:cat_tech", "to": nodes[0]["id"], "label": "domain"}]}
        store = SimpleNamespace(get_typed_edges=lambda include_unresolved=False: [])

        with patch.object(server, "_compute_vector_data",
                          return_value=(nodes, [], {}, None, [], 0.8)), \
             patch.object(server, "_build_category_hub_graph", return_value=hub), \
             patch.object(server, "_get_store", return_value=store):
            default_view = server.get_graph_data(threshold=0.8)
            hub_view = server.get_graph_data(threshold=0.8, cluster="category_hub")

        self.assertEqual("raw", default_view["cluster"])
        self.assertEqual([], default_view["typed_edges"])
        self.assertEqual("category_hub", hub_view["cluster"])
        self.assertEqual(1, sum(1 for n in hub_view["nodes"] if n.get("node_type") == "hub"))

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

    def test_graph_ui_selects_a_memory_without_reloading_the_graph(self):
        """Selecting a memory must not swap the overview for a sub-graph.

        Reloading per selection replaced the whole graph with a handful of nodes on
        every click, and the only way out was clicking empty canvas. The graph is now
        always the full corpus; selection opens the sidebar and glows neighbours.
        """
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()

        self.assertIn('id="relation-filter"', html)
        self.assertIn('id="hidden-neighbor-count"', html)
        self.assertIn('id="cluster-mode"', html)
        self.assertIn('id="show-declared"', html)
        self.assertIn("kind: 'declared'", graph)
        self.assertIn("kind: 'semantic'", graph)
        self.assertIn("new IntersectionObserver", graph)
        self.assertIn("network.stopSimulation()", graph)
        self.assertIn("pauseGraphPhysics", app)

        # No per-memory graph query survives, and selection stays local.
        load_graph = re.search(r"async function loadGraph\(\)[\s\S]*?\n}", graph).group(0)
        self.assertNotIn("memory_id=", load_graph)
        self.assertIn("cluster=", load_graph)
        self.assertNotIn("loadNeighborhood", graph)
        self.assertIn("function selectMemory(", graph)

        # The highlight must be additive: hiding nodes on selection is the bug.
        highlight = re.search(r"function highlightTypedRelations[\s\S]*?\n}", graph).group(0)
        self.assertNotIn("hidden:", highlight)
        self.assertNotIn("hidden = true", highlight)
        # glowIds is assigned before it exists was a real crash on every click.
        self.assertLess(highlight.index("const glowIds"), highlight.index("highlightedTypedEdges = glowIds"))

        # There must be an exit from the selected state.
        close_sidebar = re.search(r"function closeSidebar\(\)[\s\S]*?\n}", graph).group(0)
        self.assertIn("selectedNodeId = null", close_sidebar)

    def test_health_diagnostics_separate_useful_history_fts_and_ollama_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lancedb"
            table_dir = db_path / "memories.lance"
            table_dir.mkdir(parents=True)
            (table_dir / "history.bin").write_bytes(b"x" * 1000)
            fts = SimpleNamespace(index_type="FTS", columns=["content"], num_unindexed_rows=3, num_indexed_rows=7)
            database = FakeHealthDatabase({
                "memories": FakeHealthTable(version=5, rows=10, useful_bytes=400, fragments=4, indices=[fts]),
            })

            result = server.collect_health_diagnostics(
                db_path, database,
                pipeline={"model": "nomic-embed-text", "dimension": 768, "version": 2},
                ollama_probe=lambda: {"state": "error", "error": "connection refused"},
            )

        self.assertEqual(1, result["budgets"]["tables"])
        self.assertEqual(5, result["tables"]["memories"]["current_version"])
        self.assertEqual(4, result["tables"]["memories"]["fragments"])
        self.assertEqual(3, result["fts"]["num_unindexed_rows"])
        self.assertEqual(400, result["storage"]["useful_bytes"])
        self.assertEqual(600, result["storage"]["history_bytes"])
        self.assertEqual("error", result["ollama"]["state"])
        self.assertEqual("nomic-embed-text", result["pipeline"]["model"])
        self.assertEqual(400, result["maintenance_estimate"]["estimated_after_bytes"])

    def test_projection_and_health_ui_distinguish_dependency_error_and_no_data(self):
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()
        store_source = (ROOT / "plugin" / "store.py").read_text()

        self.assertIn('id="health-scan"', html)
        self.assertIn('id="health-container"', html)
        self.assertIn("async function loadHealth()", app)
        self.assertIn("dependency_missing", app)
        self.assertIn("no_data", app)
        self.assertIn("projection_failed", app)
        self.assertIn("PROJECTION_MAX_POINTS = 500", store_source)

    def test_container_requirements_cover_every_runtime_import(self):
        """The deployed image must ship what the imported store actually needs.

        server.py imports LanceDBStore, whose embedding path does `import httpx`.
        The container image was built from a requirements file without httpx or
        scikit-learn, so /api/search failed with "No module named 'httpx'" in
        production while every unit test passed on the host venv.
        """
        required = {"httpx", "scikit-learn"}
        for requirements in (
            ROOT / "requirements.txt",
            Path("/home/elo/github/hermes-hub/services/lancedb-viz/requirements.txt"),
        ):
            if not requirements.exists():
                continue
            text = requirements.read_text()
            for package in required:
                self.assertIn(
                    package, text,
                    f"{requirements} must declare {package}",
                )

    def test_configured_embed_model_is_not_ignored_after_import(self):
        """F14: the configured model must reach Ollama, not a frozen constant.

        `initialize` writes LANCE_EMBED_MODEL after plugin.store may already be
        imported, so a module-level constant silently kept the default model.
        """
        import os
        from plugin.store import embedding_pipeline, current_embed_model

        original = os.environ.get("LANCE_EMBED_MODEL")
        try:
            os.environ["LANCE_EMBED_MODEL"] = "another-embed-model"
            self.assertEqual("another-embed-model", current_embed_model())
            self.assertEqual("another-embed-model", embedding_pipeline()["model"])
        finally:
            if original is None:
                os.environ.pop("LANCE_EMBED_MODEL", None)
            else:
                os.environ["LANCE_EMBED_MODEL"] = original

    def test_pipeline_metadata_is_explicit_about_task_prefixes(self):
        """F14: report the pipeline, and state that Nomic prefixes are not applied.

        The installed Ollama model's template is `{{ .Prompt }}` with no task
        instruction slot, so prefixes would be embedded as literal text.
        """
        from plugin.store import embedding_pipeline

        pipeline = embedding_pipeline()

        self.assertEqual("not_applied", pipeline["task_prefixes"])
        self.assertIsInstance(pipeline["dimension"], int)
        self.assertIsInstance(pipeline["version"], int)
        self.assertTrue(pipeline["model"])

    def test_diagnostics_tolerate_both_lancedb_stats_shapes(self):
        """0.30.2 returns dicts and hides FTS counts behind index_stats()."""
        from server.maintenance import _stat_value, _fragment_count, _fts_index_stats

        # stats(): dict (0.30.2) and attribute object (0.34.0) must both work.
        self.assertEqual(1234, _stat_value({"total_bytes": 1234}, "total_bytes"))
        self.assertEqual(1234, _stat_value(SimpleNamespace(total_bytes=1234), "total_bytes"))
        self.assertEqual(0, _stat_value({}, "total_bytes"))
        self.assertEqual(0, _stat_value(None, "total_bytes"))
        self.assertEqual(0, _stat_value({"total_bytes": "nonsense"}, "total_bytes"))

        # fragments: to_lance() first, dict fallback when pylance is unavailable.
        class ToLanceBroken:
            def to_lance(self):
                raise ImportError("The lance library is required")

            def stats(self):
                return {"fragment_stats": {"num_fragments": 7}}

        class ToLanceOk:
            def to_lance(self):
                return SimpleNamespace(get_fragments=lambda: [1, 2, 3])

        self.assertEqual(7, _fragment_count(ToLanceBroken()))
        self.assertEqual(3, _fragment_count(ToLanceOk()))
        self.assertIsNone(_fragment_count(SimpleNamespace(
            to_lance=lambda: (_ for _ in ()).throw(ImportError("x")),
            stats=lambda: {},
        )))

        # FTS counts: object attributes first (0.34), then index_stats (0.30.2).
        direct = SimpleNamespace(
            index_type="FTS", columns=["content"],
            num_indexed_rows=10, num_unindexed_rows=0,
        )

        class DirectTable:
            def list_indices(self):
                return [direct]

        self.assertEqual(
            {"num_indexed_rows": 10, "num_unindexed_rows": 0},
            _fts_index_stats(DirectTable()),
        )

        legacy_index = SimpleNamespace(index_type="FTS", columns=["content"], name="content_idx")

        class LegacyTable:
            def list_indices(self):
                return [legacy_index]

            def index_stats(self, name):
                return SimpleNamespace(num_indexed_rows=42, num_unindexed_rows=0)

        self.assertEqual(
            {"num_indexed_rows": 42, "num_unindexed_rows": 0},
            _fts_index_stats(LegacyTable()),
        )

        # Neither path available: report nothing rather than inventing counts.
        class NoCounts:
            def list_indices(self):
                return [legacy_index]

            def index_stats(self, name):
                raise AttributeError("'IndexConfig' object has no attribute 'num_unindexed_rows'")

        self.assertIsNone(_fts_index_stats(NoCounts()))

    def test_search_response_budget_is_declared_clamped_and_exposed(self):
        source = (ROOT / "server" / "server.py").read_text()
        app = (ROOT / "static" / "app.js").read_text()

        # Declared: the response budget is a named constant, not an inline number.
        self.assertIn("SEARCH_MAX_RESULTS =", source)
        self.assertIn("SEARCH_MAX_DIAGNOSTIC_ROWS =", source)

        # Enforced: the server clamps a caller-supplied top_k before the store call.
        self.assertIn("_clamp_search_top_k(", source)
        self.assertIn("min(max(", source)

        # The clamp is real, not decorative.
        self.assertEqual(server.SEARCH_MAX_RESULTS, server._clamp_search_top_k(10_000))
        self.assertEqual(1, server._clamp_search_top_k(0))
        self.assertEqual(1, server._clamp_search_top_k(-5))
        self.assertEqual(7, server._clamp_search_top_k(7))
        # Malformed input clamps instead of raising a 500.
        self.assertEqual(server.SEARCH_MAX_RESULTS, server._clamp_search_top_k("not-a-number"))
        self.assertEqual(server.SEARCH_MAX_RESULTS, server._clamp_search_top_k(None))
        # The HTTP route itself must use the clamp, not a raw int().
        self.assertIn('_clamp_search_top_k(params.get("top_k"', source)

    def test_why_this_result_panel_consumes_c_diagnostics_without_recomputing(self):
        html = (ROOT / "static" / "index.html").read_text()
        graph = (ROOT / "static" / "graph.js").read_text()
        app = (ROOT / "static" / "app.js").read_text()

        # The panel is an explicit, on-demand surface, not an automatic overlay.
        self.assertIn('id="why-panel"', html)
        self.assertIn("function explainResult(", graph)
        self.assertIn("function closeWhyPanel(", graph)

        # It must read the diagnostics C2 already returns, never invent metrics.
        self.assertIn("diagnostics=1", graph)
        self.assertIn("abstention_reason", graph)
        self.assertIn("score_semantics", graph)
        self.assertIn("max_cosine_distance", graph)
        self.assertIn("min_bm25_score", graph)
        self.assertIn("neighbor_budget", graph)
        self.assertIn("retrieval_source", graph)
        self.assertIn("match_type", graph)

        # The RRF rank must never be presented as a probability or a distance.
        self.assertIn("rrf_rank_not_probability", graph)
        # No client-side recomputation of the diagnostics contract.
        self.assertNotIn("cosine_similarity", graph)
        self.assertNotIn("bm25_score(", graph)
        # Bounded panel content, consistent with the other viz surfaces.
        self.assertIn("WHY_PANEL_MAX_ROWS", graph)
        self.assertIn("escapeHtml", app)

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
