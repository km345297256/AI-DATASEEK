"""Metadata-only exact inventory helper: no recursion and no pixel reads."""
import json
import os
import subprocess
import sys

import pytest
from app.infrastructure.external.sandbox import dataset_preview_reader as module


def fixture_files(tmp_path):
    directory = tmp_path.resolve() / "image.zarr"
    directory.mkdir()
    for key in (".zattrs", ".zgroup"): (directory / key).write_bytes(b"{}");
    return str(tmp_path.resolve()), ["image.zarr/.zattrs", "image.zarr/.zgroup"]


def test_bulk_stat_never_calls_pread_or_reads_large_objects(tmp_path, monkeypatch):
    root, paths = fixture_files(tmp_path)
    with (tmp_path / "large").open("wb") as stream: stream.truncate(8 * 1024**3)
    paths.append("large")
    monkeypatch.setattr(module.os, "pread", lambda *args: pytest.fail("Metadata snapshot must not read pixels"))
    result = module.stat_managed_files(root, paths)
    assert [row["size"] for row in result] == [2, 2, 8 * 1024**3]
    assert all(set(row) == {"size", "mtime_ns", "ctime_ns", "inode", "device"} for row in result)


@pytest.mark.parametrize("path", ["../secret", "/secret", "image.zarr//.zattrs", "image.zarr/../secret", "image.zarr\\secret", "image.zarr/a\0", "image.zarr/a\n"])
def test_bulk_stat_rejects_each_unsafe_member(tmp_path, path):
    root, paths = fixture_files(tmp_path)
    with pytest.raises(module.DatasetPreviewReadError, match="^Dataset file could not be read safely$"):
        module.stat_managed_files(root, paths + [path])


@pytest.mark.parametrize("kind", ["leaf", "ancestor", "fifo", "directory"])
def test_bulk_stat_never_follows_links_or_special_files(tmp_path, kind):
    root, paths = fixture_files(tmp_path)
    if kind == "leaf": (tmp_path / "bad").symlink_to(tmp_path / paths[0]); paths.append("bad")
    elif kind == "ancestor": (tmp_path / "bad").symlink_to(tmp_path / "image.zarr", target_is_directory=True); paths.append("bad/.zattrs")
    elif kind == "fifo": os.mkfifo(tmp_path / "bad"); paths.append("bad")
    else: paths.append("image.zarr")
    with pytest.raises(module.DatasetPreviewReadError): module.stat_managed_files(root, paths)


@pytest.mark.parametrize("paths", [None, [], ["one"], ["same", "same"], ["a", 1], [str(i) for i in range(2049)]])
def test_bulk_stat_rejects_invalid_inventory(tmp_path, paths):
    with pytest.raises(module.DatasetPreviewReadError): module.stat_managed_files(str(tmp_path.resolve()), paths)


@pytest.mark.parametrize("paths", [None, [], ["one"], ["same", "same"], ["a", 1], [str(i) for i in range(2049)]])
def test_host_bulk_stat_rejects_inventory_before_helper_or_file_read(monkeypatch, paths):
    monkeypatch.setattr(module, "read_dataset_host_file", lambda *args, **kwargs: pytest.fail("Invalid inventory must not start ordinary reads or a helper"))
    with pytest.raises(module.DatasetPreviewReadError): module.stat_dataset_host_files("/allowed", paths, configured_roots="/allowed")


def test_exact_helper_enforces_allowlist_and_all_members_in_bulk_mode(tmp_path):
    root, paths = fixture_files(tmp_path)
    program = module._reader_program().replace('prefix="/host"', 'prefix="/"')
    request = dict(source=root, roots=[root], path=paths[0], offset=0, length=None, max_bytes=1024**2, scope_paths=paths)
    def run(): return subprocess.run([sys.executable, "-c", program], input=json.dumps(request).encode() + b"\n", capture_output=True, timeout=5, check=True)
    response = run(); head, payload = response.stdout.split(b"\n", 1)
    assert json.loads(head)["ok"] is True and [r["size"] for r in json.loads(payload)] == [2, 2]
    assert not response.stderr and root not in payload.decode()
    request["roots"] = [root + "/different"]
    assert run().stdout == b'{"ok":false}\n'
    request["roots"] = [root]; request["scope_paths"] = [paths[0], "../private"]
    assert run().stdout == b'{"ok":false}\n'
