import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compact_if_needed", ROOT / "scripts" / "compact-if-needed.py"
)
compact_if_needed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compact_if_needed)


class CompactIfNeededTests(unittest.TestCase):
    def test_below_threshold_is_read_only(self):
        plan = {"recommended": False, "trigger_reasons": []}
        with patch.object(compact_if_needed, "request_json", return_value=plan) as request:
            exit_code, report = compact_if_needed.run("http://127.0.0.1:7777")

        self.assertEqual(0, exit_code)
        self.assertEqual("not_needed", report["action"])
        request.assert_called_once_with(
            "http://127.0.0.1:7777/api/maintenance/compact/plan",
            timeout=30.0,
        )

    def test_recommended_plan_calls_confirmed_backup_first_route(self):
        plan = {
            "recommended": True,
            "trigger_reasons": ["memories.fragments=65>64"],
        }
        result = {
            "success": True,
            "backup_created": "/tmp/backups/lancedb-pre-compact-test",
            "rows": {"memories": {"before": 4, "after": 4, "version": 9}},
        }
        with patch.object(
            compact_if_needed, "request_json", side_effect=[plan, result]
        ) as request:
            exit_code, report = compact_if_needed.run("http://127.0.0.1:7777/")

        self.assertEqual(0, exit_code)
        self.assertEqual("compacted", report["action"])
        self.assertEqual(
            (("http://127.0.0.1:7777/api/maintenance/compact",), {
                "data": {"confirmed": True},
                "timeout": 30.0,
            }),
            request.call_args_list[1],
        )

    def test_busy_writer_defers_to_next_timer_run(self):
        plan = {"recommended": True, "trigger_reasons": ["memories.versions=65>64"]}
        busy = {"success": False, "code": "maintenance_lock_busy"}
        with patch.object(
            compact_if_needed, "request_json", side_effect=[plan, busy]
        ):
            exit_code, report = compact_if_needed.run("http://127.0.0.1:7777")

        self.assertEqual(0, exit_code)
        self.assertEqual("deferred", report["action"])


if __name__ == "__main__":
    unittest.main()
