"""F17 regression: auto-merge duplicates must not lose relations or miscount.

Three defects were reported against scripts/auto-merge-duplicates.py:
  1. connected components only walked the neighbours of the group's FIRST
     element, so a chain A~B, B~C with A!~C split into separate groups and hid
     duplicates.
  2. `total_duplicates` was counted twice (initial sum, then again per group).
  3. the group's content comparison strips [Tier=N] before deleting, and nothing
     remapped relations held by OTHER memories that point at the deleted entry,
     so a delete left a dangling target.
"""
import importlib.util
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_merge_module():
    spec = importlib.util.spec_from_file_location(
        "auto_merge_duplicates", ROOT / "scripts" / "auto-merge-duplicates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unit_vector(degrees: float) -> np.ndarray:
    radians = math.radians(degrees)
    return np.array([math.cos(radians), math.sin(radians), 0.0], dtype=np.float32)


class FakeRow(dict):
    """Minimal store row; keeps dict access and attribute-free semantics."""


class GroupingTests(unittest.TestCase):
    """Defect 1: transitive grouping (pure function, no store needed)."""

    def test_grouping_is_transitive_not_first_element_only(self):
        """A~B and B~C must yield ONE group even when A!~C.

        Angles 0/20/40 degrees: cos(20)=0.94 >= 0.9 for both hops, while
        cos(40)=0.766 < 0.9 between the ends.
        """
        merge = load_merge_module()

        class FakeStore:
            def _get_all_raw(self):
                return [
                    {"id": "a" * 12, "vector": unit_vector(0.0), "content": "A"},
                    {"id": "b" * 12, "vector": unit_vector(20.0), "content": "B"},
                    {"id": "c" * 12, "vector": unit_vector(40.0), "content": "C"},
                ]

        groups = merge.find_duplicate_groups(FakeStore(), threshold=0.9)
        sizes = sorted(len(group) for group in groups)
        self.assertEqual(
            [3], sizes,
            "transitive chain A~B~C must form one group, got sizes %r" % (sizes,),
        )

    def test_unrelated_entries_stay_separate(self):
        merge = load_merge_module()

        class FakeStore:
            def _get_all_raw(self):
                return [
                    {"id": "a" * 12, "vector": unit_vector(0.0), "content": "A"},
                    {"id": "b" * 12, "vector": unit_vector(90.0), "content": "B"},
                ]

        self.assertEqual([], merge.find_duplicate_groups(FakeStore(), threshold=0.9))


class CountingTests(unittest.TestCase):
    """Defect 2: `total_duplicates` was incremented twice."""

    def _identical_group(self, count: int):
        vector = unit_vector(0.0)
        return [
            {"id": chr(ord("a") + i) * 12,
             "vector": vector,
             "content": "Project:Alpha state=active [Tier=2]",
             "tags": []}
            for i in range(count)
        ]

    def test_duplicate_count_is_counted_once(self):
        merge = load_merge_module()
        group = self._identical_group(3)

        class FakeStore:
            def _get_all_raw(self):
                return group

        with patch("builtins.print"):
            stats = merge.run_merge(FakeStore(), threshold=0.9, apply=False)

        self.assertEqual(1, stats["duplicate_groups"])
        self.assertEqual(
            2, stats["total_duplicates"],
            "3 entries with 1 keeper = 2 duplicates, got %r" % (stats["total_duplicates"],),
        )

    def test_duplicate_count_spans_multiple_groups(self):
        merge = load_merge_module()
        first_group = self._identical_group(2)
        second_group = [
            dict(row, id="x" * 12, vector=unit_vector(90.0)) for row in first_group
        ]

        class FakeStore:
            def _get_all_raw(self):
                return first_group + second_group

        with patch("builtins.print"):
            stats = merge.run_merge(FakeStore(), threshold=0.9, apply=False)

        self.assertEqual(2, stats["duplicate_groups"])
        self.assertEqual(2, stats["total_duplicates"])


class RelationSafetyTests(unittest.TestCase):
    """Defect 3: deleting a referenced duplicate must remap or refuse."""

    def _group_and_referrer(self):
        vector = unit_vector(0.0)
        content = "Project:Alpha state=active [Tier=2]"
        keeper_id = "keep" + "0" * 8
        dupe_id = "dupe" + "0" * 8
        referrer_id = "refe" + "0" * 8
        group = [
            {"id": keeper_id, "vector": vector, "content": content, "tags": []},
            {"id": dupe_id, "vector": vector, "content": content, "tags": []},
        ]
        referrer = {
            "id": referrer_id,
            "content": "Tech:Beta state=active [Tier=2]",
            "relations": [{"type": "depends", "target_id": dupe_id}],
        }
        return keeper_id, dupe_id, referrer_id, group, referrer

    def test_remap_succeeds_then_duplicate_is_deleted(self):
        merge = load_merge_module()
        keeper_id, dupe_id, referrer_id, group, referrer = self._group_and_referrer()
        deleted = []
        updates = []

        class FakeStore:
            def _get_all_raw(self):
                return group

            def get_all(self):
                return [referrer]

            def _get_by_id_raw(self, memory_id):
                return referrer if memory_id == referrer_id else None

            def update_memory(self, patch):
                updates.append(patch)
                return {"ok": True}

            def delete(self, memory_id):
                deleted.append(memory_id)
                return True

        with patch("builtins.print"):
            stats = merge.run_merge(FakeStore(), threshold=0.9, apply=True)

        self.assertEqual([dupe_id], deleted, "remapped duplicate should be deleted")
        self.assertEqual(1, len(updates), "referrer relations must be rewritten")
        self.assertEqual(0, stats["relations_blocked"])

    def test_delete_is_refused_when_remap_fails(self):
        merge = load_merge_module()
        keeper_id, dupe_id, referrer_id, group, referrer = self._group_and_referrer()
        deleted = []

        class FakeStore:
            def _get_all_raw(self):
                return group

            def get_all(self):
                return [referrer]

            def _get_by_id_raw(self, memory_id):
                return referrer

            def update_memory(self, patch):
                raise RuntimeError("update refused")

            def delete(self, memory_id):
                deleted.append(memory_id)
                return True

        with patch("builtins.print"):
            stats = merge.run_merge(FakeStore(), threshold=0.9, apply=True)

        self.assertEqual([], deleted, "delete must be refused while a relation points at it")
        self.assertGreaterEqual(stats["skipped"], 1, "the refusal must be reported")
        self.assertEqual(1, stats["relations_blocked"])
        self.assertEqual(0, stats["deleted"])

    def test_unreadable_relations_refuse_the_delete(self):
        """If relations cannot be read, do not guess: refuse."""
        merge = load_merge_module()
        keeper_id, dupe_id, referrer_id, group, _referrer = self._group_and_referrer()
        deleted = []

        class FakeStore:
            def _get_all_raw(self):
                return group

            def get_all(self):
                raise RuntimeError("cannot read memories")

            def delete(self, memory_id):
                deleted.append(memory_id)
                return True

        with patch("builtins.print"):
            stats = merge.run_merge(FakeStore(), threshold=0.9, apply=True)

        self.assertEqual([], deleted)
        self.assertEqual(1, stats["relations_blocked"])


class RealStoreRelationTests(unittest.TestCase):
    """The remap must work through the real store API, not only fakes."""

    def setUp(self):
        import numpy as np
        from plugin.store import LanceDBStore

        self.LanceDBStore = LanceDBStore

        def fake_embed(_self, text):
            vector = np.zeros(768, dtype=np.float32)
            for token in {t.lower().strip(".,:=") for t in text.split()}:
                vector[hash(token) % 768] += 1.0
            norm = np.linalg.norm(vector)
            return vector / norm if norm else vector

        self.tmp = tempfile.TemporaryDirectory()
        self.original_embed = LanceDBStore._embed
        LanceDBStore._embed = fake_embed
        self.store = LanceDBStore(Path(self.tmp.name))

    def tearDown(self):
        self.LanceDBStore._embed = self.original_embed
        self.tmp.cleanup()

    def test_remap_moves_the_edge_to_the_keeper_and_removes_dangling(self):
        merge = load_merge_module()
        content = "Project:Alpha state=active [Tier=1]"
        keeper_id = self.store.add(content, category="project", legacy=True)
        dupe_id = self.store.add(content, category="project", legacy=True)
        referrer_id = self.store.add(
            "Tech:Beta state=active [Tier=2]", category="tech", legacy=True,
            relations=[{"type": "depends", "target_id": dupe_id}],
        )

        raw = {row["id"]: row for row in self.store._get_all_raw()}
        group = [raw[keeper_id], raw[dupe_id]]

        remapped, refused = merge.remap_incoming_relations(
            self.store, [referrer_id], dupe_id, keeper_id
        )

        self.assertEqual([referrer_id], remapped)
        self.assertEqual([], refused)

        relations = self.store._get_by_id_raw(referrer_id).get("relations")
        self.assertEqual(keeper_id, relations[0]["target_id"])

        edges = self.store._ensure_edges_table().to_arrow().to_pylist()
        self.assertEqual(
            0, len([e for e in edges if e.get("target_id") == dupe_id]),
            "no edge may still point at the duplicate after a remap",
        )
        self.assertEqual(
            1, len([e for e in edges if e.get("target_id") == keeper_id]),
            "the edge must point at the keeper",
        )

    def test_deleting_the_duplicate_leaves_no_dangling_relation(self):
        merge = load_merge_module()
        content = "Project:Alpha state=active [Tier=1]"
        keeper_id = self.store.add(content, category="project", legacy=True)
        dupe_id = self.store.add(content, category="project", legacy=True)
        referrer_id = self.store.add(
            "Tech:Beta state=active [Tier=2]", category="tech", legacy=True,
            relations=[{"type": "depends", "target_id": dupe_id}],
        )

        merge.remap_incoming_relations(self.store, [referrer_id], dupe_id, keeper_id)
        self.assertTrue(self.store.delete(dupe_id))

        edges = self.store._ensure_edges_table().to_arrow().to_pylist()
        self.assertEqual([], [e for e in edges if e.get("target_id") == dupe_id])
        self.assertEqual(1, len([e for e in edges if e.get("target_id") == keeper_id]))
        self.assertIsNone(self.store._get_by_id_raw(dupe_id))


if __name__ == "__main__":
    unittest.main()
