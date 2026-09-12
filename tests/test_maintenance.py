import importlib.util
import fcntl
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import lancedb
import pyarrow as pa
from lancedb.index import FTS
from plugin.store import LanceDBStore

from server.maintenance import (
    MAINTENANCE_MAX_FRAGMENTS,
    MAINTENANCE_TABLES,
    _active_index_uuids,
    _physical_index_uuids,
    compact_lancedb,
    compaction_plan,
    maintenance_lock_path,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lancedb_viz_server_d5", ROOT / "server" / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def create_fixture_database(path: Path) -> None:
    database = lancedb.connect(str(path))
    schema = pa.schema([pa.field("id", pa.string()), pa.field("content", pa.string())])
    for name in MAINTENANCE_TABLES:
        table = database.create_table(name, schema=schema)
        table.add([{"id": "aaaaaaaa-aaa", "content": "Project:Alpha state=active [Tier=2]"}])
        if name == "memories":
            # LanceDB 0.30.2 (the container) has no create_index(config=...).
            try:
                table.create_index("content", config=FTS(), replace=True)
            except TypeError:
                table.create_fts_index("content", replace=True)


class MaintenanceTests(unittest.TestCase):
    def test_active_index_uuid_helper_accepts_dicts_and_attributes(self):
        first = str(uuid4())
        second = str(uuid4())

        class Dataset:
            def list_indices(self):
                return [{"uuid": first}, SimpleNamespace(uuid=second)]

        table = SimpleNamespace(to_lance=lambda: Dataset())

        self.assertEqual({first, second}, _active_index_uuids(table))

    def test_active_index_uuid_helper_falls_back_when_descriptions_lack_segments(self):
        active = str(uuid4())

        class Dataset:
            def describe_indices(self):
                return [SimpleNamespace(name="content_idx")]

            def list_indices(self):
                return [{"uuid": active}]

        table = SimpleNamespace(to_lance=lambda: Dataset())

        self.assertEqual({active}, _active_index_uuids(table))

    def test_active_index_uuid_helper_works_without_pylance(self):
        """The container has no lance module: to_lance() must not be the only path.

        Production symptom: to_lance() raises ImportError there, every caller got
        None, and both orphan cleanup and the bounded trigger silently never ran
        (orphan_index_directories=null on a database with 133 orphan directories).
        """
        active = str(uuid4())

        class Table:
            def list_indices(self):
                return [SimpleNamespace(index_uuid=active, index_type="FTS")]

            def to_lance(self):
                raise ImportError("The lance library is required to use this function")

        self.assertEqual({active}, _active_index_uuids(Table()))

    def test_active_index_uuid_helper_refuses_to_guess_when_uuid_is_unreadable(self):
        class Table:
            def list_indices(self):
                return [SimpleNamespace(index_type="FTS")]

            def to_lance(self):
                raise ImportError("no lance")

        self.assertIsNone(_active_index_uuids(Table()))

    def test_compaction_creates_atomic_backup_purges_only_old_compaction_backups_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            create_fixture_database(db_path)
            abandoned_index = (
                db_path / "memories.lance" / "_indices" / str(uuid4())
            )
            abandoned_index.mkdir(parents=True)
            (abandoned_index / "marker").write_text("abandoned")
            backups_path.mkdir()
            names = [
                "lancedb-pre-compact-20260909-010101",
                "lancedb-pre-compact-20260910-010101",
                "lancedb-pre-compact-20260911-010101",
            ]
            for index, name in enumerate(names):
                folder = backups_path / name
                folder.mkdir()
                (folder / "marker").write_text(name)
                os.utime(folder, (index + 1, index + 1))
            unrelated = backups_path / "pre-v41-migration"
            unrelated.mkdir()

            result = compact_lancedb(
                db_path,
                backups_path,
                now=lambda: datetime(2026, 9, 12, 2, 3, 4),
            )

            self.assertTrue(result["success"], result)
            self.assertEqual(names[:2], [Path(path).name for path in result["backups_deleted"]])
            self.assertTrue(Path(result["backup_created"]).is_dir())
            self.assertTrue((Path(result["backup_created"]) / "memories.lance").is_dir())
            self.assertTrue(
                (Path(result["backup_created"]) / "memories.lance" / "_indices"
                 / abandoned_index.name / "marker").is_file()
            )
            self.assertFalse(abandoned_index.exists())
            self.assertTrue(unrelated.is_dir())
            self.assertEqual(2, len(list(backups_path.glob("lancedb-pre-compact-*"))))
            self.assertEqual([], list(backups_path.glob(".*.tmp-*")))
            for name in MAINTENANCE_TABLES:
                self.assertEqual(1, result["rows"][name]["before"])
                self.assertEqual(1, result["rows"][name]["after"])
                self.assertTrue(result["rows"][name]["version_readable"])
            self.assertEqual(0, result["fts_num_unindexed_rows"])
            self.assertGreaterEqual(
                result["orphan_index_directories_removed"]["memories"], 1
            )
            memories = lancedb.connect(str(db_path)).open_table("memories")
            self.assertEqual(
                _active_index_uuids(memories),
                _physical_index_uuids(db_path, "memories"),
            )

    def test_backup_failure_leaves_orphan_index_directory_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            create_fixture_database(db_path)
            orphan = db_path / "memories.lance" / "_indices" / str(uuid4())
            orphan.mkdir(parents=True)
            (orphan / "marker").write_text("must survive")

            with patch("server.maintenance.shutil.copytree", side_effect=OSError("copy failed")):
                result = compact_lancedb(db_path, backups_path)

            self.assertFalse(result["success"])
            self.assertEqual("backup", result["failed_step"])
            self.assertTrue((orphan / "marker").is_file())

    def test_orphan_cleanup_failure_reports_step_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            create_fixture_database(db_path)
            orphan = db_path / "memories.lance" / "_indices" / str(uuid4())
            orphan.mkdir(parents=True)

            with patch("server.maintenance.shutil.rmtree", side_effect=OSError("delete failed")):
                result = compact_lancedb(db_path, backups_path)

            self.assertFalse(result["success"])
            self.assertEqual("cleanup_indices.memories", result["failed_step"])
            self.assertTrue(Path(result["backup_created"]).is_dir())
            self.assertTrue(orphan.is_dir())

    def test_unknown_active_index_uuids_skip_cleanup_without_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            create_fixture_database(db_path)
            orphan = db_path / "memories.lance" / "_indices" / str(uuid4())
            orphan.mkdir(parents=True)

            with patch("server.maintenance._active_index_uuids", return_value=None):
                result = compact_lancedb(db_path, backups_path)

            self.assertTrue(result["success"], result)
            self.assertIsNone(result["orphan_index_directories_removed"]["memories"])
            self.assertTrue(orphan.is_dir())

    def test_compaction_returns_busy_before_backup_when_writer_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            create_fixture_database(db_path)
            lock_file = maintenance_lock_path(db_path).open("a+")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result = compact_lancedb(db_path, backups_path)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()

            self.assertFalse(result["success"])
            self.assertEqual("lock", result["failed_step"])
            self.assertEqual("maintenance_lock_busy", result["code"])
            self.assertFalse(backups_path.exists())

    def test_store_writer_and_maintenance_share_the_mounted_database_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            store = LanceDBStore(db_path)

            self.assertEqual(db_path / ".write.lock", store.mutation_lock_path)
            self.assertEqual(store.mutation_lock_path, maintenance_lock_path(db_path))
            with store.write_batch():
                result = compact_lancedb(db_path, backups_path)

            self.assertFalse(result["success"])
            self.assertEqual("maintenance_lock_busy", result["code"])
            self.assertFalse(backups_path.exists())

    def test_compaction_refuses_insufficient_space_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            db_path.mkdir()
            (db_path / "data.bin").write_bytes(b"x" * 64)

            result = compact_lancedb(
                db_path,
                backups_path,
                disk_usage=lambda _path: SimpleNamespace(free=63),
            )

            self.assertFalse(result["success"])
            self.assertEqual("disk_space", result["failed_step"])
            self.assertFalse(backups_path.exists())

    def test_plan_names_backup_and_exact_old_backups_to_purge_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            db_path.mkdir()
            (db_path / "data.bin").write_bytes(b"x" * 20)
            backups_path.mkdir()
            for index in range(3):
                folder = backups_path / f"lancedb-pre-compact-2026090{index + 1}-000000"
                folder.mkdir()
                os.utime(folder, (index + 1, index + 1))
            (backups_path / "lancedb-plugin-manual").mkdir()

            plan = compaction_plan(
                db_path, backups_path,
                estimated_after_bytes=5,
                now=lambda: datetime(2026, 9, 12, 2, 3, 4),
            )

            self.assertEqual("lancedb-pre-compact-20260912-020304", Path(plan["backup_to_create"]).name)
            self.assertEqual(2, len(plan["backups_to_delete"]))
            self.assertTrue(all("lancedb-pre-compact-" in path for path in plan["backups_to_delete"]))
            self.assertEqual(20, plan["size_before_bytes"])
            self.assertEqual(5, plan["estimated_after_bytes"])
            self.assertFalse(plan["recommended"])
            self.assertEqual([], plan["trigger_reasons"])

    def test_plan_recommends_compaction_from_bounded_resource_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            db_path.mkdir()

            plan = compaction_plan(
                db_path,
                backups_path,
                estimated_after_bytes=0,
                diagnostics={
                    "tables": {
                        "memories": {
                            "versions": 2,
                            "fragments": MAINTENANCE_MAX_FRAGMENTS + 1,
                        }
                    },
                    "fts": {"orphan_index_directories": None},
                },
            )

            self.assertTrue(plan["recommended"])
            self.assertEqual(1, len(plan["trigger_reasons"]))
            self.assertIn("memories.fragments", plan["trigger_reasons"][0])

    def test_server_rejects_second_compaction_and_compose_mounts_only_backup_root(self):
        self.assertTrue(server._compaction_lock.acquire(blocking=False))
        try:
            result = server.api_compact({"confirmed": True})
        finally:
            server._compaction_lock.release()

        self.assertEqual("compaction_in_progress", result["code"])
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("~/.hermes/backups:/home/hermes/.hermes/backups:rw", compose)
        source = (ROOT / "server" / "server.py").read_text()
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()
        self.assertIn('path == "/api/maintenance/compact"', source)
        self.assertIn('path == "/api/maintenance/compact/plan"', source)
        self.assertIn('id="compact-plan-button"', html)
        self.assertIn("loadCompactionPlan", app)
        self.assertIn("runCompaction", app)
        self.assertIn("Writes from another process", html)


if __name__ == "__main__":
    unittest.main()
