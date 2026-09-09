import json
import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.infrastructure.external.sandbox import dataset_preview_reader as module


def test_managed_exact_ranges_and_stat(tmp_path):
    source = tmp_path / "datasets" / "nested"
    source.mkdir(parents=True)
    (source / "source.nc").write_bytes(b"0123456789")
    data, metadata = module.read_managed_file(str(tmp_path.resolve()), "datasets/nested/source.nc", 3, 4)
    assert data == b"3456"
    assert set(metadata) == {"size", "mtime_ns", "ctime_ns", "inode", "device"}
    assert metadata["size"] == 10
    assert module.read_managed_file(str(tmp_path.resolve()), "datasets/nested/source.nc", 0, 0) == (b"", metadata)
    assert module.read_managed_file(str(tmp_path.resolve()), "datasets/nested/source.nc", 8, 8)[0] == b"89"
    assert module.read_managed_file(str(tmp_path.resolve()), "datasets/nested/source.nc", 10, 0)[0] == b""
    assert module.read_managed_file(str(tmp_path.resolve()), "datasets/nested/source.nc")[0] == b"0123456789"


@pytest.mark.parametrize("path", ["", ".", "..", "../secret", "/secret", "a/../secret", "a//b", "a/./b", "a\\b", "a\x00b", "a\nb", "a\x7fb", "./a", "a/", "a" * 4097])
def test_unsafe_relative_paths_rejected(tmp_path, path):
    with pytest.raises(module.DatasetPreviewReadError, match="^Dataset file could not be read safely$"):
        module.read_managed_file(str(tmp_path.resolve()), path, 0, 0)


@pytest.mark.parametrize("offset,length", [(-1, 0), (True, 0), (0, True), (0, -1), (0, module.MAX_READ_BYTES + 1), (2**63, 0), (1.5, 0), (0, "1")])
def test_invalid_ranges_rejected(tmp_path, offset, length):
    (tmp_path / "file").write_bytes(b"hello")
    with pytest.raises(module.DatasetPreviewReadError):
        module.read_managed_file(str(tmp_path.resolve()), "file", offset, length)


def test_symlink_leaf_ancestor_and_source_are_never_followed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "file").write_bytes(b"secret")
    (tmp_path / "linkdir").symlink_to(real, target_is_directory=True)
    (real / "link").symlink_to(real / "file")
    for root, path in [(tmp_path, "linkdir/file"), (real, "link"), (tmp_path / "linkdir", "file")]:
        with pytest.raises(module.DatasetPreviewReadError):
            module.read_managed_file(str(root.resolve()) if root == tmp_path else str(root), path)


def test_fifo_directory_and_missing_are_rejected_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "directory").mkdir()
    for path in ["pipe", "directory", "missing"]:
        with pytest.raises(module.DatasetPreviewReadError):
            module.read_managed_file(str(tmp_path.resolve()), path, 0, 0)


def test_large_file_stat_and_prefix_do_not_materialize_source(tmp_path, monkeypatch):
    source = tmp_path / "large"
    with source.open("wb") as stream:
        stream.truncate(module.MAX_READ_BYTES + 1)
    assert module.read_managed_file(str(tmp_path.resolve()), "large", 0, 0)[1]["size"] > module.MAX_READ_BYTES
    assert module.read_managed_file(str(tmp_path.resolve()), "large", 0, 3)[0] == b"\0\0\0"
    with pytest.raises(module.DatasetPreviewReadError):
        module.read_managed_file(str(tmp_path.resolve()), "large")
    with pytest.raises(module.DatasetPreviewReadError):
        module.read_managed_file(str(tmp_path.resolve()), "large", module.MAX_READ_BYTES + 2, 0)


@pytest.mark.parametrize("budget", [0, -1, True, 1.2, module.MAX_READ_BYTES + 1])
def test_invalid_plugin_budget_rejected(tmp_path, budget):
    (tmp_path / "file").write_bytes(b"abc")
    with pytest.raises(module.DatasetPreviewReadError):
        module.read_managed_file(str(tmp_path.resolve()), "file", 0, 0, max_bytes=budget)


def test_lower_plugin_budget_enforced_before_source_read(tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"abcdef")
    def no_read(*args):
        pytest.fail("Oversize source must not materialize")
    monkeypatch.setattr(module.os, "pread", no_read)
    with pytest.raises(module.DatasetPreviewReadError):
        module.read_managed_file(str(tmp_path.resolve()), "file", max_bytes=4)
    with pytest.raises(module.DatasetPreviewReadError):
        module.read_managed_file(str(tmp_path.resolve()), "file", 0, 5, max_bytes=4)
    assert module.read_managed_file(str(tmp_path.resolve()), "file", 0, 0, max_bytes=4)[1]["size"] == 6


def test_mutating_file_rejected_after_read(tmp_path, monkeypatch):
    source = tmp_path / "file"
    source.write_bytes(b"original")
    pread = os.pread
    def changed(*args):
        data = pread(*args)
        source.write_bytes(b"changed-length!")
        return data
    monkeypatch.setattr(module.os, "pread", changed)
    with pytest.raises(module.DatasetPreviewReadError):
        module.read_managed_file(str(tmp_path.resolve()), "file")


def test_fixed_worker_executes_same_reader_and_safe_error(tmp_path):
    (tmp_path / "file").write_bytes(b"abc\x00def")
    # Test the exact generated helper with only its fixed mount point remapped
    # to this test's filesystem; production still always uses /host.
    program = module._reader_program().replace('prefix="/host"', 'prefix="/"')
    request = dict(source=str(tmp_path.resolve()), roots=[str(tmp_path.resolve())], path="file", offset=1, length=4, max_bytes=module.MAX_READ_BYTES)
    result = subprocess.run([sys.executable, "-c", program], input=json.dumps(request).encode() + b"\n", capture_output=True, timeout=5, check=True)
    header, data = result.stdout.split(b"\n", 1)
    assert json.loads(header)["length"] == 4 and data == b"bc\x00d"
    request["path"] = "../private-secret"
    result = subprocess.run([sys.executable, "-c", program], input=json.dumps(request).encode() + b"\n", capture_output=True, timeout=5, check=True)
    assert result.stdout == b'{"ok":false}\n' and not result.stderr


def frame(data, stream=1):
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def output(data=b"abc", **overrides):
    header = dict(ok=True, length=len(data), stat=dict(size=len(data), mtime_ns=1, ctime_ns=2, inode=3, device=4))
    header.update(overrides)
    return frame(json.dumps(header).encode() + b"\n" + data)


class Wire:
    def __init__(self, response):
        self.response = response
        self.sent = bytearray()
        self.closed = self.halfclosed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, data):
        count = min(len(data), 7)
        self.sent.extend(data[:count])
        return count

    def recv(self, size):
        count = min(size, 13)
        result, self.response = self.response[:count], self.response[count:]
        return result

    def shutdown(self, direction):
        assert direction == socket.SHUT_WR
        self.halfclosed = True

    def close(self):
        self.closed = True


class Container:
    def __init__(self, response):
        self.wire = Wire(response)
        self.removed = self.started = False
        self.status = 0

    def attach_socket(self, params):
        assert params == dict(stdin=1, stdout=1, stderr=1, stream=1)
        return self.wire

    def start(self):
        self.started = True

    def wait(self, timeout):
        assert timeout == 3
        return {"StatusCode": self.status}

    def remove(self, force):
        assert force
        self.removed = True


class Client:
    def __init__(self, response):
        self.containers = self
        self.container = Container(response)
        self.kwargs = None
        self.closed = False
        self.lookup = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.container

    def get(self, name):
        self.lookup = name
        assert name.startswith("ai-dataseek-dataset-preview-")
        return self.container

    def close(self):
        self.closed = True


def invoke(monkeypatch, response, **kwargs):
    client = Client(response)
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    settings = SimpleNamespace(sandbox_image="trusted:sandbox", dataset_host_path_allowlist="/data", dataset_docker_host_root="")
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    return client, lambda: module.read_dataset_host_file("/data/source", "nested/file.nc", 0, 3, **kwargs)


def test_docker_protocol_resources_bounds_and_cleanup(monkeypatch):
    client, run = invoke(monkeypatch, frame(b"diagnostic", 2) + output())
    data, metadata = run()
    assert data == b"abc" and metadata["size"] == 3
    request = json.loads(client.container.wire.sent)
    assert request == dict(source="/data/source", roots=["/data"], path="nested/file.nc", offset=0, length=3, max_bytes=module.MAX_READ_BYTES)
    options = client.kwargs
    assert options["network_mode"] == "none" and options["read_only"]
    assert options["mounts"] == [{"Target": "/host", "Source": "/", "Type": "bind", "ReadOnly": True}]
    assert options["cap_drop"] == ["ALL"] and options["pids_limit"] == 16
    assert options["mem_limit"] == options["memswap_limit"] == "384m"
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in options and "volumes" not in options and "tmpfs" not in options
    assert options["log_config"]["Type"] == "none"
    assert options["entrypoint"] == ["python3"] and options["command"][0] == "-c"
    assert client.container.wire.halfclosed and client.container.wire.closed
    assert client.container.removed and client.closed


@pytest.mark.parametrize("response", [
    frame(b'{"ok":false}\n'), frame(b"not-json\nabc"), frame(b"[]\nabc"),
    output(length=2), output(ok=1), output(stat={"size": 3}), output(b"abc", length=True),
    frame(b"x" * (module.MAX_HEADER_BYTES + 4)), b"\1\0", frame(b"abc", stream=4),
    bytes([1, 0, 0, 0]) + (module.MAX_READ_BYTES + 1).to_bytes(4, "big"),
])
def test_bad_docker_output_fail_closed_and_cleanup(monkeypatch, response):
    client, run = invoke(monkeypatch, response)
    with pytest.raises(module.DatasetPreviewReadError, match="^Dataset file could not be read safely$"):
        run()
    assert client.container.removed and client.closed and client.container.wire.closed


def test_allowlist_failure_does_not_touch_docker(monkeypatch):
    client, run = invoke(monkeypatch, output(), configured_roots="/unrelated")
    with pytest.raises(module.DatasetPreviewReadError):
        run()
    assert client.kwargs is None and client.lookup is None


def test_docker_host_namespace_mapping(monkeypatch):
    client, run = invoke(monkeypatch, output())
    module.get_settings().dataset_docker_host_root = "/host_mnt"
    run()
    request = json.loads(client.container.wire.sent)
    assert request["source"] == "/host_mnt/data/source" and request["roots"] == ["/host_mnt/data"]


def test_native_error_and_ambiguous_create_cleanup(monkeypatch):
    client, run = invoke(monkeypatch, output())
    def failed(**kwargs):
        raise RuntimeError("/private/credential/docker.sock")
    client.create = failed
    with pytest.raises(module.DatasetPreviewReadError) as error:
        run()
    assert "private" not in str(error.value)
    assert client.lookup and client.container.removed and client.closed


def test_worker_nonzero_exit_is_generic_and_removed(monkeypatch):
    client, run = invoke(monkeypatch, output())
    client.container.status = 137
    with pytest.raises(module.DatasetPreviewReadError):
        run()
    assert client.container.removed and client.closed


def test_worker_timeout_is_bounded_and_removed(monkeypatch):
    client, run = invoke(monkeypatch, output())
    timer = iter([0, 0, 0, module.READ_TIMEOUT_SECONDS + 1])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(timer))
    with pytest.raises(module.DatasetPreviewReadError):
        run()
    assert client.container.removed and client.closed
