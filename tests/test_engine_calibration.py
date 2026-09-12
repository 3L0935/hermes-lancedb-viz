"""The BM25 gate is only valid for the engine it was calibrated on.

Measured 2026-09-12: identical store code, identical database rows and identical
query scored final-18 at 12.774 under lancedb 0.34.0 (abstains at 12.80) but
14.398 under lancedb 0.38.0 (leaks). The container had silently drifted from
0.34.0 to 0.38.0 through a floating `lancedb>=0.33.0` pin, which made the
production threshold a no-op without changing any source file.

These tests pin the guard that makes such a drift reportable.
"""
import unittest
from unittest.mock import patch

from plugin.store import (
    SEARCH_CALIBRATED_ENGINE,
    engine_calibration_status,
    engine_version,
)


class EngineCalibrationTests(unittest.TestCase):
    def test_calibrated_engine_names_the_lancedb_version(self):
        """The constant must name a version, not a floating range.

        A range like '>=0.33.0' is what allowed the drift in the first place.
        """
        self.assertRegex(SEARCH_CALIBRATED_ENGINE, r"^lancedb==\d+\.\d+\.\d+$")

    def test_engine_version_is_read_at_call_time(self):
        """A version swapped after import must still be reported.

        Freezing the version at import is the same class of bug as the frozen
        embedder: the value goes stale while the process keeps running.
        """
        with patch("lancedb.__version__", "9.9.9"):
            self.assertEqual(engine_version(), "lancedb==9.9.9")
        with patch("lancedb.__version__", "0.34.0"):
            self.assertEqual(engine_version(), "lancedb==0.34.0")

    def test_status_matches_when_engine_is_the_calibrated_one(self):
        version = SEARCH_CALIBRATED_ENGINE.split("==", 1)[1]
        with patch("lancedb.__version__", version):
            status = engine_calibration_status()
            self.assertTrue(status["matches"])
            self.assertEqual(status["running_engine"], SEARCH_CALIBRATED_ENGINE)

    def test_status_reports_a_drifted_engine(self):
        """A drifted engine must be visible, not silent.

        This is the exact production state observed on 2026-09-12.
        """
        with patch("lancedb.__version__", "0.38.0"):
            status = engine_calibration_status()
            self.assertFalse(status["matches"])
            self.assertEqual(status["running_engine"], "lancedb==0.38.0")
            self.assertEqual(status["calibrated_engine"], SEARCH_CALIBRATED_ENGINE)

    def test_unimportable_engine_does_not_raise(self):
        """Reporting must not be able to break a search."""
        import builtins
        real_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name == "lancedb":
                raise ImportError("simulated missing engine")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", failing_import):
            with patch.dict("sys.modules", {"lancedb": None}):
                status = engine_calibration_status()
        self.assertIn("running_engine", status)
        self.assertIn("matches", status)


if __name__ == "__main__":
    unittest.main()
