import unittest
from unittest.mock import patch

from benchmarks.dataseek.input_check import verify_service_inputs, verify_api_origin
from benchmarks.dataseek.environment import EnvironmentInspectionError


class InputCheckTests(unittest.TestCase):
    def test_remote_service_cannot_borrow_local_fingerprints(self):
        with self.assertRaises(EnvironmentInspectionError):
            verify_api_origin("https://example.org")

    def test_local_service_must_match_inspected_port(self):
        ports = '{"80/tcp":[{"HostIp":"127.0.0.1","HostPort":"7001"}]}'
        with patch("benchmarks.dataseek.input_check._run", return_value=ports):
            self.assertTrue(verify_api_origin("http://127.0.0.1:7001")["local_frontend_verified"])
            with self.assertRaises(EnvironmentInspectionError):
                verify_api_origin("http://127.0.0.1:7000")

    def task(self):
        return {"id": "t", "dataset_id": "d", "domain": "general", "source_family_id": "d",
                "prompt": "Read table.csv", "expected": {"answer": {"count": 123456}},
                "required_artifacts": [{"name": "answer.json", "kind": "json"}],
                "input_files": ["table.csv"],
                "metadata": {"input_identities": [{"name": "table.csv", "size": 3, "sha256": "a" * 64}]}}

    def test_private_checker_only_receives_file_identities(self):
        response = {"inputs": [{"task_id": "t", "matched": True, "checked_files": 1}],
                    "all_inputs_match": True, "auto_enabled_skills": []}
        with patch("benchmarks.dataseek.input_check._docker_python", return_value=response) as call:
            self.assertTrue(verify_service_inputs([self.task()])["all_inputs_match"])
            self.assertNotIn("123456", call.call_args.args[0])
            self.assertNotIn("expected", call.call_args.args[0])

    def test_changed_bytes_stop_before_analysis(self):
        response = {"inputs": [{"task_id": "t", "matched": False}], "all_inputs_match": False}
        with patch("benchmarks.dataseek.input_check._docker_python", return_value=response):
            with self.assertRaises(EnvironmentInspectionError):
                verify_service_inputs([self.task()])

    def test_automatic_skills_are_not_silently_ignored(self):
        response = {"inputs": [{"task_id": "t", "matched": True}],
                    "all_inputs_match": True, "auto_enabled_skills": ["unexpected"]}
        with patch("benchmarks.dataseek.input_check._docker_python", return_value=response):
            with self.assertRaises(EnvironmentInspectionError):
                verify_service_inputs([self.task()])

    def test_missing_identities_fail_closed(self):
        task = self.task()
        del task["metadata"]["input_identities"]
        with self.assertRaises(ValueError):
            verify_service_inputs([task])


if __name__ == "__main__":
    unittest.main()
