"""Offline tests for the benchmark's read-only, scoped environment collectors."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.dataseek import environment as env


def usage_row(session_id="run-1", count=2, tokens=(11, 7, 18)):
    row = {"_id": session_id, "record_count": count}
    for field, value in zip(env._USAGE_FIELDS, tokens):
        row[field + "_known_sum"] = value
        row[field + "_known_records"] = count
    return row


class EnvironmentTests(unittest.TestCase):
    def test_snapshot_whitelist_image_and_file_mismatch(self):
        digest = "a" * 64
        data = {"settings": {"model_name": "test-model", "api_key": "DO-NOT-RETURN", "api_base": "PRIVATE"},
                "file_sha256": {name: digest for name in env.KEY_FILES}, "private": "DO-NOT-RETURN"}
        with tempfile.TemporaryDirectory() as root, patch.object(env, "_docker_python", return_value=data), \
                patch.object(env, "_run", return_value="sha256:" + "b" * 64):
            result = env.public_runtime_snapshot(repo_root=root)
        self.assertEqual(result["settings"]["model_name"], "test-model")
        self.assertNotIn("api_key", result["settings"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("DO-NOT-RETURN", json.dumps(result))
        self.assertFalse(result["all_key_files_match"])
        self.assertTrue(all(v is None for v in result["repository_file_sha256"].values()))

    def test_exact_scoping_no_shell_and_no_trace_collection(self):
        def run(args, **kwargs):
            self.assertEqual(args, ["docker", "exec", "-i", env.DEFAULT_BACKEND_CONTAINER, "python", "-"])
            self.assertNotIn("shell", kwargs)
            script = kwargs["input"]
            self.assertIn("['token_usage'].aggregate", script)
            self.assertNotIn("model_trace", script)
            self.assertNotIn("os.environ", script)
            compile(script, "<usage-inspection>", "exec")
            return subprocess.CompletedProcess(args, 0, json.dumps([usage_row()]), "")
        with patch.object(subprocess, "run", side_effect=run):
            result = env.collect_session_usage(["run-1", "run-1"])
        self.assertEqual(result["session_ids"], ["run-1"])
        self.assertEqual(result["totals"]["total_tokens"], 18)
        self.assertEqual(env._usage_pipeline(["run-1"])[0], {"$match": {"session_id": {"$in": ["run-1"]}}})
        self.assertEqual(result["physical_request_coverage"], "unknown")
        self.assertFalse(result["complete_provider_billing"])

    def test_missing_session_is_unknown_and_partial_known_sum_is_preserved(self):
        with patch.object(env, "_docker_python", return_value=[usage_row()]):
            result = env.collect_session_usage(["run-1", "missing"])
        self.assertIsNone(result["sessions"]["missing"]["total_tokens"])
        self.assertEqual(result["sessions"]["missing"]["record_count"], 0)
        self.assertIsNone(result["totals"]["total_tokens"])
        self.assertEqual(result["totals"]["total_tokens_known_sum"], 18)

    def test_missing_field_not_zero(self):
        row = usage_row()
        row["input_tokens_known_records"] = 1
        result = env._normalise_usage(["run-1"], [row])
        self.assertIsNone(result["totals"]["input_tokens"])
        self.assertEqual(result["totals"]["input_tokens_known_sum"], 11)

    def test_no_records_and_empty_selection_do_not_claim_zero_cost(self):
        with patch.object(env, "_docker_python") as reader:
            result = env.collect_session_usage([])
        reader.assert_not_called()
        self.assertIsNone(result["totals"]["total_tokens"])
        missing = env._normalise_usage(["x"], [])
        self.assertIsNone(missing["totals"]["total_tokens_known_sum"])

    def test_recorded_zero_is_distinct_from_missing(self):
        result = env._normalise_usage(["run-1"], [usage_row(count=1, tokens=(0, 0, 0))])
        self.assertEqual(result["totals"]["total_tokens"], 0)

    def test_rejects_invalid_scope_or_unexpected_records(self):
        for value in ("not-a-list", [""], ["x;cat /secret"], ["x"] * 1001):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                env.collect_session_usage(value)
        for rows in ([usage_row("other")], [usage_row(), usage_row()], [{"_id": "run-1", "record_count": True}]):
            with self.assertRaises(env.EnvironmentInspectionError):
                env._normalise_usage(["run-1"], rows)

    def test_errors_never_echo_secret_stderr_or_timeout_output(self):
        failure = subprocess.CompletedProcess([], 1, "", "mongodb://user:SECRET@host")
        with patch.object(subprocess, "run", return_value=failure):
            with self.assertRaises(env.EnvironmentInspectionError) as exc:
                env.collect_session_usage(["run-1"])
        self.assertNotIn("SECRET", str(exc.exception))
        error = subprocess.TimeoutExpired("SECRET", 1, output="SECRET", stderr="SECRET")
        with patch.object(subprocess, "run", side_effect=error):
            with self.assertRaises(env.EnvironmentInspectionError) as exc:
                env.collect_session_usage(["run-1"])
        self.assertNotIn("SECRET", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
