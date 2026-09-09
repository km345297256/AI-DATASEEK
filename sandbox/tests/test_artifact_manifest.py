import hashlib
import os
import threading

import pytest

from app.services.artifact_manifest import fingerprint_artifacts


def test_manifest_hashes_content_and_detects_same_size_overwrite(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    artifact = output / "图.csv"
    artifact.write_bytes(b"before")
    original_time = artifact.stat().st_mtime_ns
    first = fingerprint_artifacts([str(artifact)], root=output)
    artifact.write_bytes(b"after!")
    os.utime(artifact, ns=(original_time, original_time))
    second = fingerprint_artifacts([str(artifact)], root=output)
    assert first["files"][0]["size"] == second["files"][0]["size"] == 6
    assert first["files"][0]["sha256"] == hashlib.sha256(b"before").hexdigest()
    assert first["files"][0]["sha256"] != second["files"][0]["sha256"]


def test_manifest_rejects_escape_links_devices_and_missing_files(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("must not be read")
    (output / "link").symlink_to(private)
    (output / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(output / "pipe")
    paths = [str(private), str(output / "../private.txt"), str(output / "link"),
             str(output / "dirlink/private.txt"), str(output / "pipe"), str(output / "absent")]
    result = fingerprint_artifacts(paths, root=output)
    assert result["files"] == []
    assert len(result["errors"]) == len(paths)
    assert {error["code"] for error in result["errors"]} == {"unavailable_or_changed"}


def test_manifest_streams_large_files_in_bounded_chunks(tmp_path, monkeypatch):
    from app.services import artifact_manifest as module
    artifact = tmp_path / "large.bin"
    artifact.write_bytes(b"x" * (3 * module.HASH_CHUNK_BYTES + 7))
    original_fdopen = os.fdopen
    reads = []

    class Reader:
        def __init__(self, descriptor, mode):
            self.stream = original_fdopen(descriptor, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(module.os, "fdopen", Reader)
    result = fingerprint_artifacts([str(artifact)], root=tmp_path)
    assert result["files"][0]["size"] == artifact.stat().st_size
    assert len(reads) == 5
    assert max(reads) == module.HASH_CHUNK_BYTES
    assert all(0 < size <= module.HASH_CHUNK_BYTES for size in reads)


def test_manifest_refuses_changed_file_during_hash(tmp_path, monkeypatch):
    from app.services import artifact_manifest as module
    artifact = tmp_path / "changing.bin"
    artifact.write_bytes(b"initial")
    # Some container filesystems timestamp consecutive writes in one clock
    # tick. Give the initial version a deterministic distinct timestamp.
    initial_time = artifact.stat().st_mtime_ns - 1_000_000_000
    os.utime(artifact, ns=(initial_time, initial_time))
    actual_fstat = os.fstat
    calls = 0

    def racing_stat(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            artifact.write_bytes(b"changed")
        return actual_fstat(descriptor)

    monkeypatch.setattr(module.os, "fstat", racing_stat)
    result = fingerprint_artifacts([str(artifact)], root=tmp_path)
    assert result["files"] == []
    assert result["errors"]


def test_manifest_request_size_is_bounded():
    with pytest.raises(ValueError, match="too_many_paths"):
        fingerprint_artifacts(["unused"] * 257)


def test_manifest_stops_at_initial_size_when_writer_keeps_appending(tmp_path, monkeypatch):
    from app.services import artifact_manifest as module
    artifact = tmp_path / "growing.bin"
    artifact.write_bytes(b"initial")
    original_fdopen = os.fdopen
    reads = []

    class Reader:
        def __init__(self, descriptor, mode):
            self.stream = original_fdopen(descriptor, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            reads.append(size)
            assert len(reads) <= 2, "must not chase an endlessly growing file"
            with artifact.open("ab") as writer:
                writer.write(b"appended" * 100)
            return self.stream.read(size)

    monkeypatch.setattr(module.os, "fdopen", Reader)
    result = fingerprint_artifacts([str(artifact)], root=tmp_path)
    assert result["files"] == [] and result["errors"]
    assert sum(reads) <= 8


def test_manifest_detects_replaced_parent_directory(tmp_path, monkeypatch):
    from app.services import artifact_manifest as module
    parent = tmp_path / "parent"
    parent.mkdir()
    artifact = parent / "artifact.bin"
    artifact.write_bytes(b"original")
    actual_sha = hashlib.sha256

    class Hash:
        def __init__(self):
            self.digest = actual_sha()
            self.replaced = False
        def update(self, data):
            self.digest.update(data)
            if not self.replaced:
                parent.rename(tmp_path / "previous")
                parent.mkdir()
                artifact.write_bytes(b"modified")
                self.replaced = True
        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(module.hashlib, "sha256", Hash)
    result = fingerprint_artifacts([str(artifact)], root=tmp_path)
    assert result["files"] == [] and result["errors"]


@pytest.mark.parametrize("reason", ["cancelled", "deadline"])
def test_manifest_cancellation_and_deadline_do_not_open_files(tmp_path, monkeypatch, reason):
    from app.services import artifact_manifest as module
    stop = threading.Event()
    if reason == "cancelled":
        stop.set()
    monkeypatch.setattr(module, "_open_output", lambda *args: pytest.fail("must not open after cancellation/deadline"))
    result = fingerprint_artifacts([str(tmp_path / "file")], root=tmp_path, cancelled=stop,
                                  deadline=0 if reason == "deadline" else None)
    assert result["files"] == [] and result["errors"]
