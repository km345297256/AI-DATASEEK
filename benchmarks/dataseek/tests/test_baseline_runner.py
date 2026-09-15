"""Offline B1 admission, gold separation and genuine artifact delivery tests."""
import asyncio
import base64
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks.dataseek import baseline_runner as b


def task():
    return {"id": "t1", "dataset_id": "dataset-1", "domain": "tabular", "prompt": "Compute and save answer.json.",
            "required_artifacts": [{"name": "answer.json", "kind": "json"}],
            "expected": {"secret_gold": 123}, "host_path": "/private/not-for-agent"}


def artifact(raw=b'{"answer": 4}'):
    return {"name": "answer.json", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "base64": base64.b64encode(raw).decode()}


def fake_docker(*, present=True, name="ai-dataseek-sandbox-owned", remove_error=False):
    class NotFound(Exception):
        pass
    state = {"present": present, "removed": False, "closed": False, "lookups": []}
    def remove(force=False):
        if remove_error:
            raise RuntimeError("private Docker error")
        assert force is True
        state.update(removed=True, present=False)
    container = SimpleNamespace(id="a" * 64, name=name, remove=remove,
                                attrs={"Image": "sha256:" + "b" * 64, "Created": "2026-09-15T01:00:00Z"})
    def get(identity):
        state["lookups"].append(identity)
        if not state["present"]:
            raise NotFound()
        return container
    # Deliberately does not implement __enter__/__exit__, like Docker SDK 7.1.0.
    client = SimpleNamespace(containers=SimpleNamespace(get=get), close=lambda: state.update(closed=True))
    module = SimpleNamespace(from_env=lambda **kwargs: client, errors=SimpleNamespace(NotFound=NotFound))
    return module, state


class BaselineTests(unittest.TestCase):
    def test_identity_capture_with_sdk_without_context_manager(self):
        docker, state = fake_docker()
        identity = b._capture_sandbox_identity("ai-dataseek-sandbox-owned", docker_module=docker)
        self.assertEqual(identity["container_id"], "a" * 64)
        self.assertTrue(state["closed"])
        self.assertFalse(state["removed"])

    def test_cleanup_removes_only_full_pinned_identity_then_confirms_absence(self):
        docker, state = fake_docker()
        b._remove_owned_sandbox({"name": "ai-dataseek-sandbox-owned", "container_id": "a" * 64}, docker_module=docker)
        self.assertEqual(state["lookups"], ["a" * 64, "a" * 64])
        self.assertTrue(state["removed"])
        self.assertTrue(state["closed"])

    def test_cleanup_already_gone_is_success_and_closes(self):
        docker, state = fake_docker(present=False)
        b._remove_owned_sandbox({"name": "ai-dataseek-sandbox-owned", "container_id": "a" * 64}, docker_module=docker)
        self.assertFalse(state["removed"])
        self.assertTrue(state["closed"])

    def test_cleanup_refuses_mismatched_or_missing_identity(self):
        docker, state = fake_docker(name="another-users-sandbox")
        with self.assertRaises(b.BaselineStopped):
            b._remove_owned_sandbox({"name": "ai-dataseek-sandbox-owned", "container_id": "a" * 64}, docker_module=docker)
        self.assertFalse(state["removed"])
        self.assertTrue(state["closed"])
        with self.assertRaises(b.BaselineStopped):
            b._remove_owned_sandbox(None, docker_module=docker)

    def test_cleanup_closes_sdk_connection_after_remove_error(self):
        docker, state = fake_docker(remove_error=True)
        with self.assertRaises(RuntimeError):
            b._remove_owned_sandbox({"name": "ai-dataseek-sandbox-owned", "container_id": "a" * 64}, docker_module=docker)
        self.assertTrue(state["closed"])
        self.assertFalse(state["removed"])

    def test_public_task_excludes_gold_even_inside_artifact_metadata(self):
        source = task()
        source["required_artifacts"][0]["expected"] = "secret_gold"
        public = b.public_baseline_task(source)
        self.assertNotIn("expected", json.dumps(public))
        self.assertNotIn("secret_gold", json.dumps(public))
        self.assertNotIn("/private/", json.dumps(public))
        self.assertIn("expected", source)

    def test_limits_default_and_invalid_values(self):
        self.assertEqual(b.validate_limits({})["max_calls"], 64)
        for limits in ({"max_calls": True}, {"wall_seconds": 0}, {"max_calls": 999}, {"unrecognised": 1}):
            with self.assertRaises(ValueError):
                b.validate_limits(limits)

    def test_admission_stops_before_extra_request_and_keeps_unknown_charge(self):
        admission = b.Admission(100, 2, time.monotonic() + 10)
        reservation = admission.reserve(30, 20)
        admission.settle(reservation, None)
        self.assertEqual(admission.charged_tokens, 50)
        with self.assertRaises(b.BaselineStopped) as exc:
            admission.reserve(40, 20)
        self.assertEqual(exc.exception.code, "estimated_token_admission_limit")
        self.assertEqual(admission.calls, 1)
        admission.settle(reservation, 10)
        admission.reserve(10, 20)
        with self.assertRaises(b.BaselineStopped) as exc:
            admission.reserve(1, 1)
        self.assertEqual(exc.exception.code, "model_call_limit")

    def test_actual_exceeding_reservation_stops_future_admission(self):
        admission = b.Admission(100, 5, time.monotonic() + 10)
        reserve = admission.reserve(1, 10)
        admission.settle(reserve, 101)
        with self.assertRaises(b.BaselineStopped):
            admission.reserve(1, 1)

    def test_expired_deadline_is_checked_before_admission(self):
        admission = b.Admission(100, 5, time.monotonic() - 1)
        with self.assertRaises(b.BaselineStopped) as exc:
            admission.reserve(1, 1)
        self.assertEqual(exc.exception.code, "wall_clock_limit")
        self.assertEqual(admission.calls, 0)

    def test_artifact_transport_rejects_paths_hashes_and_duplicate_names(self):
        for changed in ({"name": "../answer.json"}, {"name": "/tmp/answer.json"}, {"sha256": "0" * 64}, {"size": 1}):
            with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
                b._safe_downloads([{**artifact(), **changed}], root)
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
            b._safe_downloads([artifact(), artifact()], root)

    def test_host_sends_only_public_input_and_collects_real_file(self):
        def fake_run(args, **kwargs):
            self.assertEqual(args[:5], ["docker", "exec", "-i", b.DEFAULT_CONTAINER, "python"])
            self.assertNotIn("shell", kwargs)
            public = json.loads(kwargs["input"])
            self.assertNotIn("expected", public["task"])
            self.assertNotIn("host_path", public["task"])
            response = {"run_id": "r1", "status": "completed", "stop_reason": "model_final_response",
                        "claimed_complete": True, "artifacts": [artifact()], "model_calls": [
                            {"actual_input_tokens": 10, "actual_output_tokens": 4}], "tool_calls": [], "sandbox_removed": True}
            return subprocess.CompletedProcess(args, 0, b.RESULT_PREFIX + json.dumps(response), "PRIVATE_STDERR")
        with tempfile.TemporaryDirectory() as root, patch.object(subprocess, "run", side_effect=fake_run):
            result = b.run_baseline(task(), {"run_id": "r1", "task_id": "t1"}, root)
            self.assertEqual(json.loads((Path(root) / "r1/artifacts/answer.json").read_text()), {"answer": 4})
            self.assertEqual(result["actual_input_tokens"], 10)
            self.assertNotIn("PRIVATE_STDERR", (Path(root) / "r1/run.json").read_text())

    def test_chat_json_never_becomes_an_artifact(self):
        response = {"run_id": "r1", "status": "completed", "final_text": '{"answer": 123}', "artifacts": []}
        process = subprocess.CompletedProcess([], 0, b.RESULT_PREFIX + json.dumps(response), "")
        with tempfile.TemporaryDirectory() as root, patch.object(subprocess, "run", return_value=process):
            result = b.run_baseline(task(), {"run_id": "r1"}, root)
            self.assertFalse((Path(root) / "r1/artifacts/answer.json").exists())
            self.assertIsNone(result["actual_input_tokens"])

    def test_worker_rejects_gold_before_importing_backend(self):
        request = {"run_id": "r1", "task": task(), "limits": {}}
        with self.assertRaises(ValueError):
            asyncio.run(b._worker(request))

    def test_worker_failure_never_exposes_stderr(self):
        process = subprocess.CompletedProcess([], 1, "", "https://user:SECRET@model.test")
        with tempfile.TemporaryDirectory() as root, patch.object(subprocess, "run", return_value=process):
            result = b.run_baseline(task(), {"run_id": "r1"}, root)
        self.assertEqual(result["stop_reason"], "worker_failed_without_valid_result")
        self.assertNotIn("SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
