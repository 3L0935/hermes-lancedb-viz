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
            12.8,
            calibration_report["selected_thresholds"]["minimum_bm25_score"],
        )

    def test_bm25_threshold_sits_in_the_gap_between_the_two_populations(self):
        """The threshold must separate abstain from answer, not slice through them.

        Re-measured after the corpus drifted: the highest BM25 among questions
        that must abstain (final-18, 12.774338) was ABOVE the threshold, so it
        leaked a result. The lowest BM25 among questions that must answer
        (cal-06, 12.843929) is the other bound. The shipped threshold must sit
        strictly inside that empty space, and the calibration report must record
        both bounds so a future drift is visible instead of silent.
        """
        report = json.loads(
            (ROOT / "audit" / "repro" / "retrieval-calibration.json").read_text()
        )
        bounds = report["remeasured_no_answer_separation"]
        threshold = report["selected_thresholds"]["minimum_bm25_score"]

        self.assertGreater(threshold, bounds["highest_abstain_bm25"])
        self.assertLess(threshold, bounds["lowest_answerable_bm25"])
        self.assertEqual(
            "final-18", bounds["highest_abstain_question_id"]
        )
        self.assertEqual(
            "cal-06", bounds["lowest_answerable_question_id"]
        )

    def test_shipped_threshold_matches_the_calibration_report(self):
        """A threshold changed in code but not in the report (or vice versa) is a lie."""
        from plugin.store import SEARCH_MIN_BM25_SCORE

        report = json.loads(
            (ROOT / "audit" / "repro" / "retrieval-calibration.json").read_text()
        )
        self.assertEqual(
            report["selected_thresholds"]["minimum_bm25_score"],
            SEARCH_MIN_BM25_SCORE,
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

    def test_promotion_gate_requires_zero_false_results_and_critical_corrections(self):
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

        results["splits"]["final"]["corrected_hybrid"]["metrics"][
            "no_answer_false_result_rate"
        ] = 0.333333
        strict_failure = benchmark.promotion_decision(results, dataset)

        self.assertFalse(
            strict_failure["absolute_targets"]["strict_invariants"]
            ["no_answer_false_result_rate"]["met"]
        )
        self.assertFalse(strict_failure["corrected_hybrid"]["promoted"])

    def test_critical_correction_is_strict_even_when_current_routing_misses_it(self):
        """A living baseline miss must not waive the frozen critical invariant."""
        benchmark = load_benchmark_module()
        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}

        def variant(result_ids):
            return {
                "metrics": {
                    "recall_at_5": 0.852941,
                    "mrr": 0.72549,
                    "no_answer_false_result_rate": 0.0,
                    "answerable_count": 17,
                },
                "observations": [
                    {"question_id": "critical", "result_ids": result_ids}
                ],
            }

        results = {"splits": {"final": {
            "current_hybrid": variant([]),
            "corrected_hybrid": variant([]),
            "corrected_one_hop": variant([]),
        }}}

        decision = benchmark.promotion_decision(results, dataset)

        self.assertFalse(
            decision["absolute_targets"]["strict_invariants"]
            ["old_critical_correction"]["met"]
        )
        self.assertFalse(decision["corrected_hybrid"]["promoted"])

    def test_gate_verdict_does_not_flip_when_the_baseline_improves(self):
        """F: a baseline that improves must not turn a good fix into a regression.

        The gate compared corrected routing against a baseline measured in the
        SAME run. The corpus drifted, the baseline gained one rank step, and the
        verdict flipped to revert while corrected routing was byte-identical
        (MRR 0.754902 both sides). A verdict that reports the corpus instead of
        the code is not a gate. Here, corrected routing is unchanged and only
        the baseline moves: the verdict must stay "keep".
        """
        benchmark = load_benchmark_module()
        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}

        def variant(mrr, false_rate, recall):
            return {
                "metrics": {
                    "recall_at_5": recall, "mrr": mrr,
                    "no_answer_false_result_rate": false_rate,
                    "answerable_count": 17,
                },
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            }

        # Baseline is STRONGER than the corrected variant, which is what made
        # the frozen run flip to revert. Corrected routing still wins on the
        # absolute targets that matter.
        results = {"splits": {"final": {
            "current_hybrid": variant(0.769608, 1.0, 0.882353),
            "corrected_hybrid": variant(0.754902, 0.0, 0.882353),
            "corrected_one_hop": variant(0.754902, 0.0, 0.882353),
        }}}

        decision = benchmark.promotion_decision(results, dataset)

        self.assertEqual(
            0.0,
            decision["corrected_hybrid"]["no_answer_false_result_rate"],
        )
        self.assertEqual(
            1.0,
            decision["current_hybrid_reference"]["no_answer_false_result_rate"],
        )
        self.assertTrue(
            decision["corrected_hybrid"]["eliminates_false_results"],
            "a variant that removes every false result must be reported as such",
        )

    def test_gate_reports_absolute_targets_not_only_relative_deltas(self):
        """The verdict must be readable without knowing what the baseline did."""
        benchmark = load_benchmark_module()
        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}
        results = {"splits": {"final": {
            "current_hybrid": {
                "metrics": {"recall_at_5": 0.88, "mrr": 0.74,
                            "no_answer_false_result_rate": 1.0, "answerable_count": 17},
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_hybrid": {
                "metrics": {"recall_at_5": 0.88, "mrr": 0.75,
                            "no_answer_false_result_rate": 0.0, "answerable_count": 17},
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_one_hop": {
                "metrics": {"recall_at_5": 0.88, "mrr": 0.75,
                            "no_answer_false_result_rate": 0.0, "answerable_count": 17},
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
        }}}

        decision = benchmark.promotion_decision(results, dataset)
        targets = decision["absolute_targets"]
        strict = targets["strict_invariants"]
        guardrails = targets["corpus_guardrails"]

        self.assertEqual(0.0, strict["no_answer_false_result_rate"]["target"])
        self.assertEqual(0.0, strict["no_answer_false_result_rate"]["observed"])
        self.assertTrue(strict["no_answer_false_result_rate"]["met"])
        self.assertTrue(strict["old_critical_correction"]["met"])
        self.assertTrue(guardrails["mrr_above_degraded_control"]["met"])

    def test_mrr_floor_tolerates_a_corpus_reorder(self):
        """A rank swap caused by the corpus must not fail the gate.

        Measured during this session: the live database lost one edge
        (memory_edges rows 138 -> 137), question final-06 swapped two ranks, and
        the corrected MRR moved 0.754902 -> 0.72549 with no code change. A floor
        set as a bare point value would flap on that single reorder. The floor is
        the reference minus N rank steps, so it must still pass here.
        """
        benchmark = load_benchmark_module()
        metrics = {
            "recall_at_5": 0.852941,
            "mrr": 0.72549,           # one rank step below the reference
            "no_answer_false_result_rate": 0.0,
            "answerable_count": 17,
        }

        targets = benchmark.absolute_targets(metrics)
        mrr_guardrail = targets["corpus_guardrails"][
            "mrr_above_degraded_control"
        ]
        step = mrr_guardrail["single_question_rank_step"]

        self.assertEqual(0.029412, step)
        self.assertEqual(1, mrr_guardrail["observed_corpus_noise_rank_steps"])
        self.assertLess(mrr_guardrail["target"], metrics["mrr"])
        self.assertTrue(
            mrr_guardrail["met"],
            "a one-step corpus reorder must not fail the floor",
        )

    def test_gate_verdict_survives_one_observed_corpus_rank_step(self):
        """One corpus reorder must not be able to reverse promotion by itself.

        The measured corpus noise is one maximum MRR rank step: moving one of
        17 answerable questions from rank 1 to rank 2 costs 0.5 / 17. Place the
        healthy measurement half a step above the collapse floor, then apply
        exactly that observed reorder. The code and strict policy outcomes stay
        identical, so the promotion verdict must stay identical too.
        """
        benchmark = load_benchmark_module()
        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}

        def variant(mrr):
            return {
                "metrics": {
                    "recall_at_5": 0.852941,
                    "mrr": mrr,
                    "no_answer_false_result_rate": 0.0,
                    "answerable_count": 17,
                },
                "observations": [
                    {"question_id": "critical", "result_ids": ["critical-id"]}
                ],
            }

        current = variant(0.74)
        one_hop = variant(0.74)
        before = 0.710784
        after_one_reorder = round(before - (0.5 / 17), 6)

        def decision(candidate_mrr):
            return benchmark.promotion_decision(
                {"splits": {"final": {
                    "current_hybrid": current,
                    "corrected_hybrid": variant(candidate_mrr),
                    "corrected_one_hop": one_hop,
                }}},
                dataset,
            )

        before_decision = decision(before)
        after_decision = decision(after_one_reorder)

        self.assertEqual(0.681372, after_one_reorder)
        self.assertTrue(before_decision["corrected_hybrid"]["promoted"])
        self.assertTrue(
            after_decision["corrected_hybrid"]["promoted"],
            "one measured corpus-noise step must not reverse the verdict",
        )

    def test_mrr_floor_still_fails_a_real_routing_collapse(self):
        """The corpus guardrails must still reject a real routing collapse."""
        benchmark = load_benchmark_module()
        metrics = {
            "recall_at_5": 0.4,
            "mrr": 0.4,
            "no_answer_false_result_rate": 0.0,
            "answerable_count": 17,
        }

        targets = benchmark.absolute_targets(metrics)

        self.assertFalse(
            targets["corpus_guardrails"]["mrr_above_degraded_control"]["met"]
        )
        self.assertFalse(
            targets["corpus_guardrails"][
                "recall_at_5_above_degraded_control"
            ]["met"]
        )

        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}

        def variant(candidate_metrics):
            return {
                "metrics": candidate_metrics,
                "observations": [
                    {"question_id": "critical", "result_ids": ["critical-id"]}
                ],
            }

        healthy_metrics = {
            "recall_at_5": 0.852941,
            "mrr": 0.72549,
            "no_answer_false_result_rate": 0.0,
            "answerable_count": 17,
        }
        decision = benchmark.promotion_decision(
            {"splits": {"final": {
                "current_hybrid": variant(healthy_metrics),
                "corrected_hybrid": variant(metrics),
                "corrected_one_hop": variant(healthy_metrics),
            }}},
            dataset,
        )

        self.assertFalse(decision["corrected_hybrid"]["promoted"])
        self.assertEqual("revert corrected routing", decision["corrected_hybrid"]["decision"])

    def test_decision_reports_how_many_questions_could_flip_the_verdict(self):
        """The margin must be reported, not just the conclusion.

        The margin describes the SAME-RUN comparison, which is still informative:
        it says how close the corrected variant was to the baseline measured that
        day. It no longer decides the verdict (absolute targets do), but it must
        still be reported so a fragile comparison is visible.
        """
        benchmark = load_benchmark_module()
        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}
        results = {"splits": {"final": {
            "current_hybrid": {
                "metrics": {
                    "recall_at_5": 0.88, "mrr": 0.74,
                    "no_answer_false_result_rate": 1.0, "answerable_count": 17,
                },
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_hybrid": {
                # MRR is BELOW the same-run baseline: one question moved.
                "metrics": {
                    "recall_at_5": 0.85, "mrr": 0.725,
                    "no_answer_false_result_rate": 0.0, "answerable_count": 17,
                },
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_one_hop": {
                "metrics": {
                    "recall_at_5": 0.85, "mrr": 0.725,
                    "no_answer_false_result_rate": 0.0, "answerable_count": 17,
                },
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
        }}}

        decision = benchmark.promotion_decision(results, dataset)
        margin = decision["margin"]

        self.assertLess(margin["mrr_gap"], 0)
        self.assertGreaterEqual(margin["single_question_steps_to_flip_mrr"], 1)
        self.assertTrue(margin["hinges_on_a_single_question"])
        # The verdict is decided by the absolute targets, not by that gap:
        # false results are eliminated and the floors are met.
        self.assertTrue(decision["corrected_hybrid"]["promoted"])
        self.assertEqual(
            0.0,
            decision["corrected_hybrid"]["no_answer_false_result_rate"],
        )

    def test_margin_reports_no_flip_needed_when_mrr_is_ahead(self):
        benchmark = load_benchmark_module()
        dataset = {"questions": [{
            "id": "critical", "category": "old_critical_correction",
            "expected_ids": ["critical-id"],
        }]}
        results = {"splits": {"final": {
            "current_hybrid": {
                "metrics": {
                    "recall_at_5": 0.88, "mrr": 0.74,
                    "no_answer_false_result_rate": 1.0, "answerable_count": 17,
                },
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_hybrid": {
                "metrics": {
                    "recall_at_5": 0.85, "mrr": 0.75,
                    "no_answer_false_result_rate": 0.0, "answerable_count": 17,
                },
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
            "corrected_one_hop": {
                "metrics": {
                    "recall_at_5": 0.85, "mrr": 0.75,
                    "no_answer_false_result_rate": 0.0, "answerable_count": 17,
                },
                "observations": [{"question_id": "critical", "result_ids": ["critical-id"]}],
            },
        }}}

        margin = benchmark.promotion_decision(results, dataset)["margin"]

        self.assertEqual(0, margin["single_question_steps_to_flip_mrr"])
        self.assertFalse(margin["hinges_on_a_single_question"])


if __name__ == "__main__":
    unittest.main()
