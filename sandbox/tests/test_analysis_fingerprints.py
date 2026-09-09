import hashlib
import json
import os
import threading
import time

import pytest

from app.services import analysis_fingerprints as snapshot


def test_snapshot_hashes_sources_and_outputs_without_returning_data(tmp_path):
    datasets, output = tmp_path / "datasets", tmp_path / "output"
    datasets.mkdir()
    output.mkdir()
    source, script = datasets / "source.csv", output / "script.py"
    source.write_text("private_source,42\n")
    script.write_text("private_script = 1\n")
    result = snapshot.fingerprint_analysis_files([str(source), str(script)], roots=(datasets, output))
    assert result["errors"] == []
    assert len(result["files"]) == 2
    for item, path in zip(result["files"], (source, script)):
        assert item == {"path": str(path), "size": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert "private_source" not in json.dumps(result)
    assert "private_script" not in json.dumps(result)


def test_same_size_source_modification_changes_snapshot(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("before")
    original_time = path.stat().st_mtime_ns
    before = snapshot.fingerprint_analysis_files([str(path)], roots=(tmp_path,))
    path.write_text("after!")
    os.utime(path, ns=(original_time, original_time))
    after = snapshot.fingerprint_analysis_files([str(path)], roots=(tmp_path,))
    assert before["files"][0]["size"] == after["files"][0]["size"]
    assert before["files"][0]["sha256"] != after["files"][0]["sha256"]


def test_snapshot_rejects_arbitrary_roots_links_and_special_files(tmp_path):
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    private = tmp_path / "private.csv"
    private.write_text("secret")
    (datasets / "link.csv").symlink_to(private)
    (datasets / "parent").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(datasets / "pipe")
    paths = [str(private), str(datasets), str(datasets / "../private.csv"), str(datasets / "link.csv"),
             str(datasets / "parent/private.csv"), str(datasets / "pipe"), str(datasets / "missing")]
    result = snapshot.fingerprint_analysis_files(paths, roots=(datasets,))
    assert result["files"] == []
    assert len(result["errors"]) == len(paths)
    assert {item["code"] for item in result["errors"]} == {"unavailable_or_unsafe_path"}


def test_snapshot_batch_budget_rejects_not_samples_large_input(tmp_path, monkeypatch):
    paths = [tmp_path / "one", tmp_path / "two"]
    for path in paths:
        path.write_bytes(b"123456")
    monkeypatch.setattr(snapshot, "MAX_BATCH_BYTES", 10)
    result = snapshot.fingerprint_analysis_files([str(path) for path in paths], roots=(tmp_path,))
    assert len(result["files"]) == 1
    assert result["errors"] == [{"path": str(paths[1]), "code": "snapshot_size_limit"}]


@pytest.mark.parametrize("condition", ["cancelled", "expired"])
def test_snapshot_stops_before_open_when_cancelled(tmp_path, monkeypatch, condition):
    def unexpected_open(*args):
        raise AssertionError("must not open a cancelled snapshot")
    monkeypatch.setattr(snapshot, "_open_output", unexpected_open)
    cancelled = threading.Event()
    if condition == "cancelled":
        cancelled.set()
    result = snapshot.fingerprint_analysis_files([str(tmp_path / "source")], roots=(tmp_path,), cancelled=cancelled,
        deadline=time.monotonic() - 1 if condition == "expired" else time.monotonic() + 10)
    assert result["files"] == []
    assert result["errors"][0]["code"] == "snapshot_deadline"


def test_snapshot_detects_source_changed_during_hash(tmp_path, monkeypatch):
    path = tmp_path / "source.csv"
    path.write_text("initial")
    initial_time = path.stat().st_mtime_ns - 1_000_000_000
    os.utime(path, ns=(initial_time, initial_time))
    original_fstat = os.fstat
    calls = 0
    def racing_stat(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("changed")
        return original_fstat(descriptor)
    monkeypatch.setattr(snapshot.os, "fstat", racing_stat)
    result = snapshot.fingerprint_analysis_files([str(path)], roots=(tmp_path,))
    assert result["files"] == []
    assert result["errors"][0]["code"] == "changed_during_read"


def test_snapshot_limits_unique_requests_and_deduplicates_paths(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("a,b\n")
    result = snapshot.fingerprint_analysis_files([str(path), str(path)], roots=(tmp_path,))
    assert len(result["files"]) == 1
    with pytest.raises(ValueError, match="invalid_snapshot_path_count"):
        snapshot.fingerprint_analysis_files([str(path)] * 65, roots=(tmp_path,))


def test_snapshot_api_is_separate_from_legacy_output_fingerprints(client, tmp_path, monkeypatch):
    from conftest import BASE_URL
    from app.api.v1 import file as file_api
    path = tmp_path / "source.csv"
    path.write_text("a,b\n1,2\n")
    monkeypatch.setattr(file_api, "fingerprint_analysis_files", lambda paths, **kwargs:
                        snapshot.fingerprint_analysis_files(paths, roots=(tmp_path,), **kwargs))
    response = client.post(f"{BASE_URL}/api/v1/file/analysis-fingerprints", json={"paths": [str(path)]})
    assert response.status_code == 200
    assert response.json()["data"]["files"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert client.post(f"{BASE_URL}/api/v1/file/analysis-fingerprints", json={"paths": []}).status_code == 422
