"""Deterministic tests for scientific assertions and denominator integrity."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.dataseek.protocol import ProtocolError, loads_json, validate_run, validate_task
from benchmarks.dataseek.scoring import aggregate_results, score_run, verify_answer


def task(task_id="task-1", source="source-1", domain="general", answer=None):
    return {
        "id": task_id, "dataset_id": "dataset-1", "domain": domain,
        "source_family_id": source, "prompt": "Calculate the specified answer.",
        "expected": {"answer": {"count": 2, "mean": 1.5} if answer is None else answer},
        "tolerance": {"atol": 1e-6, "rtol": 1e-6},
        "required_artifacts": [{"name": "answer.json", "kind": "json"}],
    }


def run(run_id="run-1", task_id="task-1", index=0, **kwargs):
    return {
        "run_id": run_id, "task_id": task_id, "method": "D",
        "backbone_version": "fixed-model-version", "run_index": index,
        "status": "completed", **kwargs,
    }


class AnswerTests(unittest.TestCase):
    def test_per_field_tolerance_and_relative_tolerance(self):
        gold = {"answer": {"large": 1000.0, "zero": 0.0, "strict": 10.0}}
        actual = {"answer": {"large": 1000.1, "zero": 0.01, "strict": 10.0}}
        result = verify_answer(gold, actual, {"atol": 0.0, "rtol": 0.001},
                               {"/answer/zero": {"atol": 0.01, "rtol": 0.0}})
        self.assertTrue(result["passed"])
        actual["answer"]["strict"] = 10.1
        self.assertFalse(verify_answer(gold, actual, {"atol": 0.0, "rtol": 0.001},
                                      {"/answer/zero": {"atol": 0.01, "rtol": 0.0}})["passed"])

    def test_integer_not_relaxed_by_float_tolerance(self):
        self.assertFalse(verify_answer({"answer": 2}, {"answer": 3}, {"atol": 100, "rtol": 100})["passed"])
        self.assertTrue(verify_answer({"answer": 2}, {"answer": 2.0})["passed"])

    def test_bool_and_strings_cannot_masquerade_as_measurements(self):
        for value in (True, False, "1", None):
            with self.subTest(value=value):
                self.assertFalse(verify_answer({"answer": 1}, {"answer": value})["passed"])

    def test_non_finite_predictions_fail_even_in_unexpected_fields(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.assertFalse(verify_answer({"answer": 1.0}, {"answer": value})["passed"])
                self.assertFalse(verify_answer({"answer": 1.0}, {"answer": 1.0, "other": value})["passed"])

    def test_gold_non_finite_and_misconfigured_tolerance_raise(self):
        with self.assertRaises(ProtocolError):
            verify_answer({"answer": float("nan")}, {"answer": 1})
        for tolerance in ({"atol": True, "rtol": 0}, {"atol": -1, "rtol": 0}, {"atol": 1}):
            with self.subTest(tolerance=tolerance), self.assertRaises(ProtocolError):
                verify_answer({"answer": 1.0}, {"answer": 1.0}, tolerance)
        with self.assertRaises(ProtocolError):
            verify_answer({"answer": 1.0}, {"answer": 1.0}, field_tolerances={"/missing": {"atol": 0, "rtol": 0}})

    def test_object_keys_array_order_and_string_case_are_exact(self):
        gold = {"answer": {"label": "Kelvin", "values": [1, 2]}}
        for actual in (
            {"answer": {"label": "kelvin", "values": [1, 2]}},
            {"answer": {"label": "Kelvin", "values": [2, 1]}},
            {"answer": {"label": "Kelvin", "values": [1]}},
            {"answer": {"label": "Kelvin"}},
            {"answer": {"label": "Kelvin", "values": [1, 2], "extra": "x"}},
        ):
            with self.subTest(actual=actual):
                self.assertFalse(verify_answer(gold, actual)["passed"])
        self.assertTrue(verify_answer(gold, gold)["passed"])

    def test_sets_ignore_order_but_not_duplicate_or_missing_members(self):
        gold = {"answer": ["A", "B", 1]}
        mode = {"/answer": "set"}
        self.assertTrue(verify_answer(gold, {"answer": [1, "B", "A"]}, comparison=mode)["passed"])
        for answer in (["A", "A", "B", 1], ["A", "B"], ["A", "B", True], ["A", "B", "1"]):
            self.assertFalse(verify_answer(gold, {"answer": answer}, comparison=mode)["passed"])
        with self.assertRaises(ProtocolError):
            verify_answer({"answer": ["A", "A"]}, {"answer": ["A"]}, comparison=mode)

    def test_float_sets_are_rejected_to_avoid_ambiguous_matching(self):
        with self.assertRaises(ProtocolError):
            verify_answer({"answer": [1.1]}, {"answer": [1.1]}, comparison={"/answer": "set"})

    def test_pointer_escaping_supports_real_json_keys(self):
        gold = {"answer": {"a/b~c": 1.0}}
        self.assertTrue(verify_answer(gold, {"answer": {"a/b~c": 1.1}},
                                     field_tolerances={"/answer/a~1b~0c": {"atol": 0.2, "rtol": 0}})["passed"])

    def test_huge_prediction_fails_without_overflowing(self):
        self.assertFalse(verify_answer({"answer": 1.0}, {"answer": 10 ** 1000})["passed"])


class ProtocolTests(unittest.TestCase):
    def test_input_files_are_safe_relative_unique_names(self):
        candidate = task()
        candidate["input_files"] = ["observations.csv", "nested/labels.txt"]
        self.assertIs(validate_task(candidate), candidate)
        for names in (["../source.csv"], ["/private/source.csv"], ["a.csv", "a.csv"], []):
            candidate["input_files"] = names
            with self.subTest(names=names), self.assertRaises(ProtocolError):
                validate_task(candidate)

    def test_strict_json_rejects_duplicate_keys_constants_and_exponent_overflow(self):
        for text in ('{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}'):
            with self.subTest(text=text), self.assertRaises(ProtocolError):
                loads_json(text)

    def test_task_rejects_absolute_traversal_and_unsupported_artifacts(self):
        for name in ("/tmp/answer.json", "../answer.json", "a/../answer.json", "a\\answer.json", "a//b"):
            candidate = task()
            candidate["required_artifacts"].append({"name": name, "kind": "code"})
            with self.subTest(name=name), self.assertRaises(ProtocolError):
                validate_task(candidate)
        candidate = task()
        candidate["required_artifacts"].append({"name": "plot.png", "kind": "image"})
        with self.assertRaises(ProtocolError):
            validate_task(candidate)

    def test_run_rejects_ambiguous_counts_costs_and_unknown_fields(self):
        for change in ({"run_index": True}, {"cost_usd": float("nan")}, {"cost_usd": -1},
                       {"actual_input_tokens": True}, {"status": "done"}, {"status": []}, {"arbitrary": 1}):
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                validate_run(run(**change))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = task()

    def tearDown(self):
        self.temp.cleanup()

    def write_answer(self, answer=None):
        value = self.task["expected"] if answer is None else answer
        (self.root / "answer.json").write_text(json.dumps(value), encoding="utf-8")

    def test_real_answer_artifact_required_self_report_not_enough(self):
        result = score_run(self.task, run(claimed_complete=True), self.root)
        self.assertFalse(result["independent_success"])
        self.write_answer()
        result = score_run(self.task, run(claimed_complete=False), self.root)
        self.assertTrue(result["independent_success"])
        self.assertEqual(len(result["artifact_hashes"]["answer.json"]), 64)

    def test_status_failure_kept_even_with_correct_partial_artifact(self):
        self.write_answer()
        result = score_run(self.task, run(status="timeout"), self.root)
        self.assertTrue(result["answer_passed"])
        self.assertFalse(result["independent_success"])

    def test_required_code_is_syntax_checked_but_not_executed(self):
        self.task["required_artifacts"].append({"name": "analysis.py", "kind": "code"})
        self.write_answer()
        self.assertFalse(score_run(self.task, run(), self.root)["independent_success"])
        code = self.root / "analysis.py"
        for text in ("# only comment", "def broken("):
            code.write_text(text, encoding="utf-8")
            self.assertFalse(score_run(self.task, run(), self.root)["independent_success"])
        code.write_text("raise RuntimeError('must never execute during scoring')\n", encoding="utf-8")
        self.assertTrue(score_run(self.task, run(), self.root)["independent_success"])

    def test_invalid_json_and_symlink_escape_fail_without_exposing_path(self):
        (self.root / "answer.json").write_text('{"answer": NaN}', encoding="utf-8")
        self.assertFalse(score_run(self.task, run(), self.root)["independent_success"])
        (self.root / "answer.json").unlink()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "private.json"
            external.write_text(json.dumps(self.task["expected"]), encoding="utf-8")
            (self.root / "answer.json").symlink_to(external)
            result = score_run(self.task, run(), self.root)
            self.assertFalse(result["independent_success"])
            self.assertNotIn(outside, json.dumps(result))


class AggregationTests(unittest.TestCase):
    @staticmethod
    def scored(row, passed):
        return {**row, "independent_success": passed}

    def test_repeat_source_domain_average_is_not_micro_average(self):
        tasks = [task("a", "s1", "d1"), task("b", "s1", "d1"),
                 task("c", "s2", "d1"), task("d", "s3", "d2")]
        rows = [self.scored(run("a0", "a", 0), True), self.scored(run("a1", "a", 1, status="failed"), False),
                self.scored(run("b0", "b"), True), self.scored(run("c0", "c", status="timeout"), False),
                self.scored(run("d0", "d"), True)]
        group = aggregate_results(tasks, rows)["groups"][0]
        self.assertEqual(group["task_scores"]["a"], 0.5)
        self.assertEqual(group["source_scores"]["d1"]["s1"], 0.75)
        self.assertEqual(group["domain_scores"]["d1"], 0.375)
        self.assertEqual(group["macro_tsr"], 0.6875)
        self.assertEqual(group["micro_tsr"], 0.6)

    def test_missing_scheduled_runs_count_as_failures(self):
        tasks = [task()]
        planned = [run(), run("run-2", index=1), run("run-3", index=2)]
        observed = [self.scored(planned[0], True)]
        summary = aggregate_results(tasks, observed, expected_runs=planned)
        group = summary["groups"][0]
        self.assertEqual(group["total_runs"], 3)
        self.assertEqual(group["missing_runs"], 2)
        self.assertEqual(group["macro_tsr"], 1 / 3)
        self.assertEqual(summary["denominator_scope"], "frozen_plan")

    def test_identical_duplicate_id_deduplicated_conflicts_rejected(self):
        one = self.scored(run(), True)
        summary = aggregate_results([task()], [one, copy.deepcopy(one)])
        self.assertEqual(summary["duplicate_run_records"], 1)
        self.assertEqual(summary["groups"][0]["total_runs"], 1)
        with self.assertRaises(ProtocolError):
            aggregate_results([task()], [one, {**one, "independent_success": False}])
        with self.assertRaises(ProtocolError):
            aggregate_results([task()], [one, {**one, "run_id": "different-id"}])

    def test_failed_attempt_costs_included_missing_cost_not_zero(self):
        rows = [self.scored(run(cost_usd=2.0), True),
                self.scored(run("run-2", index=1, status="failed", cost_usd=3.0), False)]
        group = aggregate_results([task()], rows)["groups"][0]
        self.assertEqual(group["total_cost_usd"], 5.0)
        self.assertEqual(group["success_unit_cost_usd"], 5.0)
        rows.append(self.scored(run("run-3", index=2, status="unavailable"), False))
        group = aggregate_results([task()], rows)["groups"][0]
        self.assertEqual(group["known_cost_usd"], 5.0)
        self.assertEqual(group["cost_coverage"], 2 / 3)
        self.assertIsNone(group["total_cost_usd"])
        self.assertIsNone(group["success_unit_cost_usd"])

    def test_no_success_and_no_completion_claims_are_not_zero_cost_or_zero_fcr(self):
        row = self.scored(run(status="failed", cost_usd=4.0, claimed_complete=False), False)
        group = aggregate_results([task()], [row])["groups"][0]
        self.assertIsNone(group["success_unit_cost_usd"])
        self.assertEqual(group["success_unit_cost_reason"], "no_successes")
        self.assertIsNone(group["false_completion_rate"])
        self.assertEqual(group["false_complete_all_rate"], 0.0)
        self.assertEqual(group["completion_claim_coverage"], 0.0)

    def test_unannotated_completion_claims_produce_unknown_all_run_fcr(self):
        rows = [self.scored(run(claimed_complete=None), True),
                self.scored(run("run-2", index=1, status="failed"), False)]
        group = aggregate_results([task()], rows)["groups"][0]
        self.assertIsNone(group["false_completion_rate"])
        self.assertIsNone(group["false_complete_all_rate"])
        self.assertEqual(group["claim_annotation_coverage"], 0.0)

    def test_partial_completion_annotations_preserve_count_but_not_all_run_fcr(self):
        rows = [self.scored(run(claimed_complete=True, status="failed"), False),
                self.scored(run("run-2", index=1, claimed_complete=None), True)]
        group = aggregate_results([task()], rows)["groups"][0]
        self.assertEqual(group["false_complete_runs"], 1)
        self.assertEqual(group["false_completion_rate"], 1.0)
        self.assertIsNone(group["false_complete_all_rate"])
        self.assertEqual(group["claim_annotation_coverage"], 0.5)

    def test_claims_and_usage_have_explicit_coverage(self):
        rows = [self.scored(run(claimed_complete=True, actual_input_tokens=10, actual_output_tokens=5), True),
                self.scored(run("run-2", index=1, claimed_complete=True, status="failed"), False)]
        group = aggregate_results([task()], rows)["groups"][0]
        self.assertEqual(group["false_completion_rate"], 0.5)
        self.assertEqual(group["false_complete_all_rate"], 0.5)
        self.assertEqual(group["actual_usage_coverage"], 0.5)
        self.assertEqual(group["known_actual_input_tokens"], 10)
        self.assertIsNone(group["total_actual_input_tokens"])

    def test_methods_models_and_unknown_tasks_do_not_silently_mix(self):
        rows = [self.scored(run(), True), self.scored(run("run-2", method="B2"), False)]
        self.assertEqual(len(aggregate_results([task()], rows)["groups"]), 2)
        with self.assertRaises(ProtocolError):
            aggregate_results([task()], [self.scored(run(task_id="not-in-plan"), True)])
        with self.assertRaises(ProtocolError):
            aggregate_results([task()], rows, expected_runs=[run()])


if __name__ == "__main__":
    unittest.main()
