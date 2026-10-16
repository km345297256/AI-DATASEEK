"""Offline preservation tests; no Docker daemon or production data required."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sandbox_archive", Path(__file__).with_name("sandbox_archive.py"))
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)

IMAGE = "sha256:" + "a" * 64
MANIFEST = {"sandbox": {"id": IMAGE}}


def target(number=1, *, paused=False, running=True):
    return {"Id": str(number) * 64, "Name": "/ai-dataseek-sandbox-" + str(number) * 8,
            "Image": IMAGE, "SizeRootFs": 4096, "HostConfig": {"AutoRemove": True},
            "State": {"Running": running, "Paused": paused, "Dead": False, "Restarting": False},
            "Mounts": [{"Type": "bind", "RW": False, "Source": "/secret/datasets/private.csv"}],
            "Config": {"Env": ["PASSWORD=do-not-save"], "Cmd": ["do-not-save"], "Labels": {
                "com.docker.compose.project": "ai-dataseek", "com.docker.compose.service": "sandbox-image"}}}


class FakeDocker:
    def __init__(self, containers):
        self.containers = {item["Id"]: copy.deepcopy(item) for item in containers}
        self.calls = []
        self.before_stop = lambda: None

    def __call__(self, args):
        self.calls.append(args)
        action = args[2]
        if action == "ls":
            identifiers = list(self.containers)
            selected = args[args.index("--filter") + 1]
            if selected.startswith("id="):
                identifiers = [item for item in identifiers if item == selected[3:]]
            return "\n".join(identifiers)
        if action == "inspect":
            return json.dumps([self.containers[item] for item in args[3:] if item != "--size"])
        identity = args[-1]
        if action == "pause":
            self.containers[identity]["State"]["Paused"] = True
        elif action == "unpause":
            self.containers[identity]["State"]["Paused"] = False
        elif action == "stop":
            self.before_stop()
            del self.containers[identity]
        else:
            raise AssertionError(args)
        return identity


class SandboxArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "backups" / "sandboxes"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as tar:
            file = tarfile.TarInfo("workspace/result.txt")
            file.size = len(b"preserved analysis")
            tar.addfile(file, io.BytesIO(b"preserved analysis"))
        self.tar_bytes = data.getvalue()
        self.export_script()
        disk = patch.object(archive.shutil, "disk_usage", return_value=type("Disk", (), {"free": 20 * 1024 ** 3})())
        disk.start()
        self.addCleanup(disk.stop)
        output = patch("sys.stdout", new=io.StringIO())
        self.output = output.start()
        self.addCleanup(output.stop)

    def export_script(self, suffix=""):
        executable = self.bin / "docker"
        executable.write_text("#!" + sys.executable + "\nimport sys,time\n"
                              + "sys.stdout.buffer.write(" + repr(self.tar_bytes) + ")\n"
                              + "sys.stdout.buffer.flush()\n" + suffix)
        executable.chmod(0o755)

    def run_archive(self, docker, manifest=MANIFEST, **kwargs):
        return archive.archive_and_stop_sandboxes(docker, manifest, cwd=self.root, env=self.env,
                                                 directory=self.directory, **kwargs)

    def test_no_dynamic_targets_does_not_require_manifest(self):
        fixed = target()
        fixed["Name"] = "/ai-dataseek-sandbox-image-1"
        docker = FakeDocker([fixed])
        self.assertEqual(self.run_archive(docker, {}), [])
        self.assertFalse(self.directory.exists())
        self.assertFalse(any(call[2] in {"pause", "stop", "unpause"} for call in docker.calls))

    def test_v2_fixed_services_are_not_archived_as_dynamic_sandboxes(self):
        fixed = []
        for index, service in enumerate(sorted(archive.FIXED_SERVICES), 1):
            item = target(index)
            item["Name"] = "/DataSeek-v2" + ("" if service == "frontend" else "-" + service)
            item["Config"]["Labels"]["com.docker.compose.service"] = service
            fixed.append(item)
        docker = FakeDocker(fixed)
        self.assertEqual(self.run_archive(docker, {}), [])
        self.assertFalse(any(call[2] in {"pause", "stop", "unpause"} for call in docker.calls))
        fixed[0]["Config"]["Labels"]["com.docker.compose.project"] = "another-project"
        with self.assertRaises(archive.SandboxArchiveError):
            self.run_archive(FakeDocker(fixed), {})

    def test_unrecognized_target_is_rejected_before_any_pause(self):
        changes = [lambda item: item["Config"]["Labels"].update({"com.docker.compose.project": "other"}),
                   lambda item: item.update(Image="sha256:" + "b" * 64),
                   lambda item: item.update(Name="/ai-dataseek-sandbox-unrecognized"),
                   lambda item: item["Config"]["Labels"].update({"com.docker.compose.service": "other"}),
                   lambda item: item["HostConfig"].update(AutoRemove=False),
                   lambda item: item["Mounts"][0].update(RW=True),
                   lambda item: item["Mounts"][0].update(Type="volume")]
        for mutate in changes:
            unknown = target(2)
            mutate(unknown)
            docker = FakeDocker([target(), unknown])
            with self.subTest(mutation=mutate), self.assertRaises(archive.SandboxArchiveError):
                self.run_archive(docker)
            self.assertFalse(any(call[2] == "pause" for call in docker.calls))
        with self.assertRaises(archive.SandboxArchiveError):
            self.run_archive(FakeDocker([target()]), {})

    def test_insufficient_space_preserves_running_container(self):
        docker = FakeDocker([target()])
        with patch.object(archive.shutil, "disk_usage", return_value=type("Disk", (), {"free": 1})()):
            with self.assertRaisesRegex(archive.SandboxArchiveError, "Insufficient"):
                self.run_archive(docker)
        self.assertFalse(any(call[2] == "pause" for call in docker.calls))
        self.assertTrue(docker.containers["1" * 64]["State"]["Running"])

    def test_success_has_complete_private_archives_before_first_stop(self):
        docker = FakeDocker([target(), target(2)])
        def inspect_archives():
            for number in (1, 2):
                path = self.directory / (str(number) * 64 + ".tar")
                self.assertEqual(path.read_bytes(), self.tar_bytes)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                metadata = path.with_suffix(".json")
                self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
                saved = json.loads(metadata.read_text())
                self.assertEqual(saved["sha256"], hashlib.sha256(self.tar_bytes).hexdigest())
                self.assertNotIn("PASSWORD", metadata.read_text())
                self.assertNotIn("/secret", metadata.read_text())
                self.assertNotIn("Cmd", metadata.read_text())
        docker.before_stop = inspect_archives
        result = self.run_archive(docker)
        self.assertEqual(len(result), 2)
        self.assertEqual(docker.containers, {})
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(list(self.directory.glob("*.partial")), [])
        self.assertFalse(any(call[2] == "rm" for call in docker.calls))

    def test_second_export_failure_stops_none_and_resumes_both(self):
        self.export_script("sys.exit(1 if sys.argv[-1] == '2' * 64 else 0)\n")
        docker = FakeDocker([target(), target(2)])
        with self.assertRaisesRegex(archive.SandboxArchiveError, "export failed"):
            self.run_archive(docker)
        self.assertFalse(any(call[2] == "stop" for call in docker.calls))
        self.assertTrue(all(not item["State"]["Paused"] for item in docker.containers.values()))
        self.assertTrue((self.directory / ("1" * 64 + ".tar")).exists())
        self.assertFalse((self.directory / ("2" * 64 + ".tar")).exists())
        self.assertEqual(list(self.directory.glob("*.partial")), [])

    def test_preexisting_pause_is_preserved_when_export_fails(self):
        self.export_script("sys.exit(1)\n")
        docker = FakeDocker([target(paused=True)])
        with self.assertRaises(archive.SandboxArchiveError):
            self.run_archive(docker)
        self.assertTrue(docker.containers["1" * 64]["State"]["Paused"])
        self.assertFalse(any(call[2] == "unpause" for call in docker.calls))

    def test_export_timeout_kills_child_removes_partial_and_resumes(self):
        self.export_script("time.sleep(5)\n")
        docker = FakeDocker([target()])
        with self.assertRaisesRegex(archive.SandboxArchiveError, "timed out"):
            self.run_archive(docker, timeout=0.1)
        self.assertFalse(docker.containers["1" * 64]["State"]["Paused"])
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(any(call[2] == "stop" for call in docker.calls))

    def test_bad_tar_is_not_published_or_stopped(self):
        self.tar_bytes = b"not a finished tar" * 1024
        self.export_script()
        docker = FakeDocker([target()])
        with self.assertRaisesRegex(archive.SandboxArchiveError, "tar terminator"):
            self.run_archive(docker)
        self.assertFalse(any(call[2] == "stop" for call in docker.calls))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_existing_archive_is_never_overwritten(self):
        self.directory.mkdir(parents=True)
        existing = self.directory / ("1" * 64 + ".tar")
        existing.write_bytes(b"old preserved data")
        docker = FakeDocker([target()])
        with self.assertRaisesRegex(archive.SandboxArchiveError, "already exists"):
            self.run_archive(docker)
        self.assertEqual(existing.read_bytes(), b"old preserved data")
        self.assertFalse(any(call[2] == "stop" for call in docker.calls))
        self.assertFalse(docker.containers["1" * 64]["State"]["Paused"])

    def test_archive_path_must_not_traverse_symlink(self):
        actual = self.root / "actual"
        actual.mkdir()
        (self.root / "backups").symlink_to(actual, target_is_directory=True)
        docker = FakeDocker([target()])
        with self.assertRaisesRegex(archive.SandboxArchiveError, "symlinks"):
            self.run_archive(docker)
        self.assertFalse(any(call[2] == "pause" for call in docker.calls))

    def test_autoremove_race_after_unpause_accepts_already_gone(self):
        docker = FakeDocker([target()])
        def run(arguments):
            if arguments[2] == "stop":
                del docker.containers[arguments[-1]]
                raise RuntimeError("already removed")
            return docker(arguments)
        self.assertEqual(len(self.run_archive(run)), 1)
        self.assertEqual(docker.containers, {})

    def test_pause_command_error_after_side_effect_still_resumes(self):
        docker = FakeDocker([target()])
        def run(arguments):
            result = docker(arguments)
            if arguments[2] == "pause":
                raise RuntimeError("caller timed out after pause completed")
            return result
        with self.assertRaises(archive.SandboxArchiveError):
            self.run_archive(run)
        self.assertFalse(docker.containers["1" * 64]["State"]["Paused"])
        self.assertFalse(any(call[2] == "stop" for call in docker.calls))

    def test_successful_stop_command_with_running_container_is_rejected(self):
        docker = FakeDocker([target()])
        def run(arguments):
            if arguments[2] == "stop":
                return arguments[-1]
            return docker(arguments)
        with self.assertRaisesRegex(archive.SandboxArchiveError, "still running"):
            self.run_archive(run)
        self.assertTrue((self.directory / ("1" * 64 + ".tar")).exists())
        self.assertFalse(docker.containers["1" * 64]["State"]["Paused"])


if __name__ == "__main__":
    unittest.main()
