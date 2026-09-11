import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from server.maintenance import MAINTENANCE_TABLES, compact_lancedb, compaction_plan


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
    def test_compaction_creates_atomic_backup_purges_only_old_compaction_backups_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "lancedb"
            backups_path = root / "backups"
            create_fixture_database(db_path)
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
            self.assertTrue(unrelated.is_dir())
            self.assertEqual(2, len(list(backups_path.glob("lancedb-pre-compact-*"))))
            self.assertEqual([], list(backups_path.glob(".*.tmp-*")))
            for name in MAINTENANCE_TABLES:
                self.assertEqual(1, result["rows"][name]["before"])
                self.assertEqual(1, result["rows"][name]["after"])
                self.assertTrue(result["rows"][name]["version_readable"])
            self.assertEqual(0, result["fts_num_unindexed_rows"])

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
