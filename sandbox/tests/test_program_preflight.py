"""Read-only startup recovery evidence; never execution or source disclosure."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pytest

from app.services import program
from conftest import BASE_URL


@pytest.fixture
def inputs(tmp_path):
    # Resolve macOS's /var alias so the fixture itself obeys no-follow policy.
    root = tmp_path.resolve() / "output"
    root.mkdir()
    path = root / "analysis.py"
    path.write_text("print('evidence, not executed')\n")
    return root, path


def probe(root, path, cwd=None, **kwargs):
    return program.probe_program_prerequisites(str(cwd or root), str(path), root=root, **kwargs)


def test_ready_probe_is_read_only_and_does_not_disclose_source_or_paths(inputs):
    root, path = inputs
    marker = root / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    path.write_text(source)
    result = probe(root, path)
    assert result["status"] == "ready" and result["ready"] is True
    assert result["source"]["source_digest"] == hashlib.sha256(source.encode()).hexdigest()
    assert len(result["prerequisite_digest"]) == 64
    assert not marker.exists()
    serialized = json.dumps(result)
    assert str(root) not in serialized and source not in serialized


def test_existing_source_with_missing_cwd_recovers_without_source_changes(inputs):
    root, path = inputs
    cwd = root / "work"
    before = probe(root, path, cwd)
    assert before["status"] == "blocked"
    assert before["cwd"]["state"] == "missing"
    assert before["prerequisite_digest"] is not None
    cwd.mkdir()
    after = probe(root, path, cwd)
    assert after["status"] == "ready"
    assert before["source"] == after["source"]
    assert before["prerequisite_digest"] != after["prerequisite_digest"]


def test_missing_output_and_program_are_proven_before_creation(inputs):
    original_root, _ = inputs
    root = original_root / "not-created"
    path = root / "analysis.py"
    before = probe(root, path)
    assert before["status"] == "blocked"
    assert before["cwd"]["state"] == before["source"]["state"] == "missing"
    root.mkdir()
    path.write_text("print(42)\n")
    assert probe(root, path)["status"] == "ready"


def test_timestamps_inode_and_same_byte_rewrite_are_not_prerequisite_progress(inputs):
    root, path = inputs
    before = probe(root, path)
    source = path.read_bytes()
    os.utime(path, (10, 10))
    os.utime(root, (10, 10))
    assert probe(root, path)["prerequisite_digest"] == before["prerequisite_digest"]
    replacement = root / "replacement"
    replacement.write_bytes(source)
    replacement.replace(path)
    assert probe(root, path)["prerequisite_digest"] == before["prerequisite_digest"]
    path.write_text("print('changed')\n")
    assert probe(root, path)["prerequisite_digest"] != before["prerequisite_digest"]


@pytest.mark.parametrize("target", ["cwd", "source"])
def test_permission_restoration_changes_only_accessibility_prerequisite(inputs, monkeypatch, target):
    root, path = inputs
    original_access = os.access
    denied = True

    def access(name, mode, **kwargs):
        if denied and ((target == "cwd" and name == root.name and mode == os.X_OK)
                       or (target == "source" and name == path.name and mode == os.R_OK)):
            return False
        return original_access(name, mode, **kwargs)

    monkeypatch.setattr(program.os, "access", access)
    before = probe(root, path)
    assert before["status"] == "blocked" and before["prerequisite_digest"]
    assert before[target]["state"] == ("not_accessible" if target == "cwd" else "not_readable")
    denied = False
    after = probe(root, path)
    assert after["status"] == "ready"
    assert before["prerequisite_digest"] != after["prerequisite_digest"]


@pytest.mark.parametrize("target", ["source", "cwd", "parent"])
def test_symlinks_are_unavailable_not_proven_missing(inputs, target):
    root, path = inputs
    if target == "source":
        actual = root / "real.py"
        path.rename(actual)
        path.symlink_to(actual)
        cwd = root
    elif target == "cwd":
        cwd = root / "linked"
        cwd.symlink_to(root, target_is_directory=True)
    else:
        link = root / "linked"
        link.symlink_to(root, target_is_directory=True)
        path = link / path.name
        cwd = root
    result = probe(root, path, cwd)
    assert result["status"] == "unavailable"
    assert result["prerequisite_digest"] is None


def test_fifo_is_reported_without_reading_or_blocking(inputs):
    root, path = inputs
    path.unlink()
    os.mkfifo(path)
    result = probe(root, path)
    assert result["status"] == "blocked"
    assert result["source"]["state"] == "not_regular"
    assert result["prerequisite_digest"] is not None


def test_non_directory_cwd_has_stable_blocked_identity(inputs):
    root, path = inputs
    result = probe(root, path, path)
    assert result["status"] == "blocked"
    assert result["cwd"]["state"] == "not_directory"


def test_oversized_source_is_not_read(inputs, monkeypatch):
    root, path = inputs
    monkeypatch.setattr(program, "MAX_PROGRAM_SOURCE_BYTES", 1)
    result = probe(root, path)
    assert result["status"] == "blocked"
    assert result["source"]["state"] == "too_large"


@pytest.mark.parametrize("kind", ["race", "io_error", "cancelled", "deadline"])
def test_unknown_probe_never_supplies_a_recovery_digest(inputs, monkeypatch, kind):
    root, path = inputs
    kwargs = {}
    if kind in {"race", "io_error"}:
        original = program._preflight_entry
        count = 0

        def entry(*args, **kwargs):
            nonlocal count
            count += 1
            if kind == "io_error":
                raise OSError("private host path must not leak")
            value, proof = original(*args, **kwargs)
            if count == 2:
                path.write_text("print('changed during probe')\n")
            return value, proof

        monkeypatch.setattr(program, "_preflight_entry", entry)
    elif kind == "cancelled":
        cancelled = threading.Event()
        cancelled.set()
        kwargs["cancelled"] = cancelled
    else:
        kwargs["deadline"] = time.monotonic() - 1
    result = probe(root, path, **kwargs)
    assert result["status"] == "unavailable"
    assert result["prerequisite_digest"] is None
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("source,cwd", [
    ("relative.py", "/tmp"),
    ("/home/ubuntu/output/../secret.py", "/tmp"),
    ("/home/ubuntu/output/file.py", "relative"),
    ("/home/ubuntu/output/file.py", "/tmp/../tmp"),
    ("/home/ubuntu/output/file.sh", "/tmp"),
])
def test_unsupported_paths_never_produce_recovery_identity(source, cwd):
    result = program.probe_program_prerequisites(cwd, source)
    assert result["status"] == "unavailable"
    assert result["prerequisite_digest"] is None


def test_preflight_api_is_read_only_and_exposes_stable_blocked_then_ready_state(client, inputs):
    root, path = inputs
    cwd = root / "work"
    request = {"exec_dir": str(cwd), "script_path": str(path)}
    before = client.post(f"{BASE_URL}/api/v1/shell/program-preflight", json=request, timeout=10)
    assert before.status_code == 200
    assert before.json()["success"] is True
    assert before.json()["data"]["status"] == "blocked"
    assert not cwd.exists()
    cwd.mkdir()
    after = client.post(f"{BASE_URL}/api/v1/shell/program-preflight", json=request, timeout=10)
    assert after.status_code == 200
    assert after.json()["data"]["status"] == "ready"
    assert before.json()["data"]["prerequisite_digest"] != after.json()["data"]["prerequisite_digest"]
    assert "execution_receipt" not in after.text and str(root) not in after.text


@pytest.mark.parametrize("directory", ["scripts", "workspace/custom-programs", "output"])
def test_default_probe_tracks_missing_then_created_script_in_any_analysis_directory(tmp_path, directory):
    parent = tmp_path.resolve()
    workspace = parent / directory
    path = workspace / "analysis.py"
    before = program.probe_program_prerequisites(str(workspace), str(path))
    assert before["status"] == "blocked"
    assert before["cwd"]["state"] == before["source"]["state"] == "missing"
    workspace.mkdir(parents=True)
    source = "raise RuntimeError('the probe must not execute this program')\n"
    path.write_text(source)
    after = program.probe_program_prerequisites(str(workspace), str(path))
    assert after["status"] == "ready"
    assert after["source"]["source_digest"] == hashlib.sha256(source.encode()).hexdigest()
    assert before["prerequisite_digest"] != after["prerequisite_digest"]
    assert program.probe_program_prerequisites(str(workspace), str(path)) == after
    assert str(parent) not in json.dumps(after) and source not in json.dumps(after)


def test_optional_narrow_probe_scope_still_rejects_outside_program(inputs):
    root, path = inputs
    result = program.probe_program_prerequisites(str(root), str(path), root=root / "narrow")
    assert result["status"] == "unavailable" and result["prerequisite_digest"] is None


@pytest.mark.parametrize("target", ["source", "cwd", "parent"])
def test_default_probe_outside_output_still_rejects_symlinks(tmp_path, target):
    workspace = tmp_path.resolve() / "scripts"
    workspace.mkdir()
    path = workspace / "analysis.py"
    path.write_text("raise RuntimeError('must not run')\n")
    cwd = workspace
    if target == "source":
        real = workspace / "real.py"
        path.rename(real)
        path.symlink_to(real)
    elif target == "cwd":
        cwd = workspace / "linked"
        cwd.symlink_to(workspace, target_is_directory=True)
    else:
        link = workspace / "linked"
        link.symlink_to(workspace, target_is_directory=True)
        path = link / path.name
    result = program.probe_program_prerequisites(str(cwd), str(path))
    assert result["status"] == "unavailable" and result["prerequisite_digest"] is None
