import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import lancedb
import pyarrow as pa


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "audit" / "repro" / "benchmark-retrieval.py"


def load_benchmark_module():
    spec = importlib.util.spec_from_file_location("benchmark_retrieval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RetrievalBenchmarkTests(unittest.TestCase):
    def test_question_splits_are_valid_disjoint_and_final_is_frozen(self):
        benchmark = load_benchmark_module()
        calibration = benchmark.load_question_set(
            ROOT / "audit" / "repro" / "retrieval-questions-calibration.json"
        )
        final = benchmark.load_question_set(
            ROOT / "audit" / "repro" / "retrieval-questions-final.json"
        )

        self.assertEqual("calibration", calibration["split"])
        self.assertFalse(calibration["frozen"])
        self.assertEqual("final", final["split"])
        self.assertTrue(final["frozen"])
        self.assertEqual(40, len(calibration["questions"]) + len(final["questions"]))
        self.assertTrue(
            {"fr", "en"}.issubset(
                {question["language"] for dataset in (calibration, final)
                 for question in dataset["questions"]}
            )
        )
        expected_categories = {
            "project_name", "id", "exact_path", "config", "paraphrase",
            "dependency", "old_critical_correction", "contradiction", "no_answer",
        }
        for dataset in (calibration, final):
            categories = {question["category"] for question in dataset["questions"]}
            self.assertTrue(expected_categories.issubset(categories))
        calibration_ids = {question["id"] for question in calibration["questions"]}
        final_ids = {question["id"] for question in final["questions"]}
        self.assertTrue(calibration_ids.isdisjoint(final_ids))

        calibration_report = json.loads(
            (ROOT / "audit" / "repro" / "retrieval-calibration.json").read_text()
        )
        self.assertEqual(
            0.3,
            calibration_report["selected_thresholds"]["maximum_cosine_distance"],
        )
        self.assertEqual(
            12.75,
            calibration_report["selected_thresholds"]["minimum_bm25_score"],
        )

    def test_metrics_are_deterministic_and_exclude_no_answer_from_recall(self):
        benchmark = load_benchmark_module()
        questions = [
            {"id": "answer", "expected_ids": ["a", "b"], "avoid_ids": [], "expect_abstain": False},
            {"id": "miss", "expected_ids": ["c"], "avoid_ids": [], "expect_abstain": False},
            {"id": "none", "expected_ids": [], "avoid_ids": [], "expect_abstain": True},
        ]
        observations = [
            {"question_id": "answer", "result_ids": ["x", "a", "b"], "context_chars": 30,
             "embedding_calls": 1, "embedding_ms": 2.0, "search_ms": 4.0},
            {"question_id": "miss", "result_ids": ["x"], "context_chars": 10,
             "embedding_calls": 1, "embedding_ms": 3.0, "search_ms": 5.0},
            {"question_id": "none", "result_ids": ["x"], "context_chars": 5,
             "embedding_calls": 0, "embedding_ms": 0.0, "search_ms": 1.0},
        ]

        metrics = benchmark.compute_metrics(questions, observations)

        self.assertEqual(0.5, metrics["recall_at_5"])
        self.assertEqual(0.25, metrics["mrr"])
        self.assertEqual(1.0, metrics["no_answer_false_result_rate"])
        self.assertEqual(15.0, metrics["mean_context_chars"])
        self.assertEqual(2, metrics["embedding_calls"])
        self.assertEqual(5.0, metrics["embedding_ms"])
        self.assertEqual(10.0, metrics["search_ms"])

    def test_fixture_copy_requires_tmp_and_preserves_source_version(self):
        benchmark = load_benchmark_module()
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("content", pa.string()),
        ])
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory(
            dir="/tmp"
        ) as fixture_parent:
            source_path = Path(source_dir)
            fixture_path = Path(fixture_parent) / "fixture"
            source_db = lancedb.connect(str(source_path))
            source_table = source_db.create_table(
                "memories",
                data=pa.Table.from_pylist(
                    [{"id": "public-id", "content": "private fixture text"}],
                    schema=schema,
                ),
            )
            source_version = source_table.version

            report = benchmark.copy_read_only_fixture(source_path, fixture_path)

            self.assertEqual(source_version, source_table.version)
            self.assertEqual(source_version, report["source_versions"]["memories"])
            copied = lancedb.connect(str(fixture_path)).open_table("memories")
            self.assertEqual(1, copied.count_rows())
            with self.assertRaises(ValueError):
                benchmark.copy_read_only_fixture(source_path, ROOT / "forbidden-fixture")

    def test_promotion_gate_requires_false_result_gain_and_no_critical_regression(self):
        benchmark = load_benchmark_module()
        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}
        results = {"splits": {"final": {
            "current_hybrid": {
                "metrics": {"recall_at_5": 0.88, "mrr": 0.74, "no_answer_false_result_rate": 1.0},
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_hybrid": {
                "metrics": {"recall_at_5": 0.85, "mrr": 0.75, "no_answer_false_result_rate": 0.0},
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_one_hop": {
                "metrics": {"recall_at_5": 0.85, "mrr": 0.75, "no_answer_false_result_rate": 0.0},
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
        }}}

        decision = benchmark.promotion_decision(results, dataset)

        self.assertTrue(decision["corrected_hybrid"]["promoted"])
        self.assertFalse(decision["corrected_one_hop_as_default"]["promoted"])


if __name__ == "__main__":
    unittest.main()
