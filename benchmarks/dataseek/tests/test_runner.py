import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks.dataseek import __main__ as cli
from benchmarks.dataseek.http_client import parse_sse, TransportError, DataSeekClient
from benchmarks.dataseek.runner import public_task, build_plan, trace_usage, run_dataseek


class RunnerTests(unittest.TestCase):
    def test_public_projection_hides_gold_and_paths(self):
        task = {"id": "t", "dataset_id": "d", "domain": "general", "prompt": "Compute.",
                "required_artifacts": [], "expected": {"secret": 123}, "host_path": "/secret"}
        self.assertNotIn("expected", public_task(task))
        self.assertNotIn("host_path", public_task(task))

    def test_multiline_sse_heartbeats_and_incomplete_terminal(self):
        wire = b': keepalive\n\nevent: tool\nid: 2\ndata: {"a":\ndata: 1}\n\nevent: done\ndata: {}'
        self.assertEqual(list(parse_sse(io.BytesIO(wire))), [{"event": "tool", "id": "2", "data": {"a": 1}}])

    def test_sse_rejects_large_event(self):
        with self.assertRaises(TransportError):
            list(parse_sse([b'data: ' + b'a' * 100], max_event_bytes=10))

    def test_plan_reproducible_distinct_runs(self):
        tasks = [{"id": "a"}, {"id": "b"}]
        first = build_plan(tasks, 2, "model", "exp")
        self.assertEqual(first, build_plan(tasks, 2, "model", "exp"))
        self.assertEqual(len({r["run_id"] for r in first}), 4)

    def test_usage_missing_is_not_zero_or_complete(self):
        usage = trace_usage([{"kind": "model_request", "model_name": "m"},
                             {"kind": "model_request", "actual_input_tokens": 10, "actual_output_tokens": 2}])
        self.assertEqual(usage["traced_usage_coverage"], 0.5)
        self.assertIsNone(trace_usage([])["traced_actual_input_tokens"])
        self.assertFalse(usage["trace_scope_includes_bootstrap"])

    def test_api_credentials_in_url_rejected(self):
        with self.assertRaises(ValueError):
            DataSeekClient("https://name:secret@example.org")

    def test_broken_stream_stops_without_resubmitting(self):
        class Client:
            submissions = 0
            stops = 0
            def api(self, method, path, body=None):
                if method == "PUT": return {"session_id": "session"}
                if path.endswith("/stop"):
                    self.stops += 1
                    return None
                if path.endswith("/files"): return []
                if "model-traces" in path: return {"traces": []}
                return {"status": "completed"}
            def stream(self, path, body):
                self.submissions += 1
                yield {"event": "message", "id": "x", "data": {"role": "assistant", "content": "Partial"}}
                raise TransportError("disconnected")
        task = {"id": "t", "dataset_id": "d", "domain": "general", "prompt": "Compute", "required_artifacts": []}
        planned = build_plan([task], 1, "m", "test")[0]
        client = Client()
        with tempfile.TemporaryDirectory() as root:
            result = run_dataseek(client, task, planned, root, 1)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["metadata"]["stop_confirmed"])
        self.assertEqual(client.submissions, 1)
        self.assertEqual(client.stops, 1)

    def test_offline_report_distinguishes_method_resource_controls(self):
        task = {"id": "t", "dataset_id": "d", "domain": "general", "source_family_id": "s",
                "prompt": "Compute.", "expected": {"answer": 1},
                "required_artifacts": [{"name": "answer.json", "kind": "json"}]}
        config = {"scope": "development", "backbone_version": "m", "repeats": 1, "wall_seconds": 300}
        planned = build_plan([task], 1, "m", "test")
        with tempfile.TemporaryDirectory() as root, patch.object(cli, "_load", return_value=(config, [task], planned)):
            with patch("sys.stdout", new=io.StringIO()):
                cli.report(SimpleNamespace(experiment_dir=root))
            summary = json.loads((Path(root) / "summary.json").read_text())
            limits = summary["limitations"]
            self.assertTrue(any(line.startswith("DataSeek API:") and "no pre-request" in line for line in limits))
            self.assertTrue(any(line.startswith("Generic ReAct B1:") and "token admission" in line
                                and "physical-call cap" in line for line in limits))
            self.assertEqual(summary["started_runs"], 0)
            self.assertIsNone(summary["planned_denominator_incomplete_as_failure"]["groups"][0]["false_complete_all_rate"])


if __name__ == "__main__":
    unittest.main()
