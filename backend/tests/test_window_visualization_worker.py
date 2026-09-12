"""No real Docker access: test the actual private wire broker and confinement."""
import base64
import json
import socket
import threading
from types import SimpleNamespace

import docker
import pytest

from app.infrastructure.external.sandbox import window_visualization_worker as module


def frame(payload, stream=1):
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def line(value):
    return json.dumps(value).encode() + b"\n"


def request(**kwargs):
    return {"type": "read", "id": 1, "offset": 0, "length": 4, **kwargs}


SUCCESS = {"type": "result", "ok": True, "data": {"marker": "safe"}}


class Wire:
    def __init__(self, response):
        self.response, self.sent = response, bytearray()
        self.closed = self.halfclosed = self.eof = False

    def settimeout(self, value):
        assert value == .5

    def send(self, data):
        amount = min(len(data), 17)
        self.sent.extend(data[:amount])
        return amount

    def recv(self, size):
        chunk, self.response = self.response[:min(size, 7)], self.response[min(size, 7):]
        self.eof = not chunk
        return chunk

    def shutdown(self, direction):
        assert direction == socket.SHUT_WR
        self.halfclosed = True

    def close(self):
        self.closed = True


class Client:
    def __init__(self, response, removed=False, status=0):
        self.wire = Wire(response)
        self.containers = self
        self.removed, self.status = removed, status
        self.settings = None
        self.cleaned = self.closed = self.waited = False

    def create(self, **kwargs):
        self.settings = kwargs
        return self

    def get(self, name):
        assert name.startswith("ai-dataseek-window-")
        return self

    def attach_socket(self, params):
        assert params == {"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1}
        return self.wire

    def start(self):
        pass

    def wait(self, timeout):
        assert timeout == 3 and self.wire.eof
        self.waited = True
        if self.removed:
            raise docker.errors.NotFound("reaped")
        return {"StatusCode": self.status}

    def remove(self, force):
        assert force
        self.cleaned = True
        if self.removed:
            raise docker.errors.NotFound("reaped")

    def close(self):
        self.closed = True


def invoke(monkeypatch, response, *, read=None, cancelled=None, removed=False, status=0, **kwargs):
    client = Client(response, removed, status)
    monkeypatch.setattr(module.docker, "from_env", lambda **_: client)
    reads = []

    def bounded(offset, length):
        reads.append((offset, length))
        return read(offset, length) if read else b"A" * length

    options = dict(size=1024**3, reader="edf", kind="series", format="edf", options={},
                   limits={"max_read_bytes": module.MAX_READ_BYTES, "max_total_bytes": module.MAX_TOTAL_BYTES, "max_reads": module.MAX_READS},
                   read_range=bounded, cancelled=cancelled or threading.Event())
    options.update(kwargs)
    return client, reads, lambda: module.run_window_visualization_worker("trusted:image", **options)


@pytest.mark.parametrize("removed", [False, True])
def test_bidirectional_ranges_never_transmit_source_paths_or_entire_source(monkeypatch, removed):
    content = frame(line(request())) + frame(line(request(id=2, offset=100000000, length=8))) + frame(line(SUCCESS))
    client, reads, run = invoke(monkeypatch, content, removed=removed)
    assert run() == {"ok": True, "data": {"marker": "safe"}}
    assert reads == [(0, 4), (100000000, 8)]
    messages = [json.loads(row) for row in bytes(client.wire.sent).splitlines()]
    assert messages[0] == {"protocol": module.PROTOCOL, "type": "init", "size": 1024**3,
        "reader": "edf", "kind": "series", "format": "edf", "options": {},
        "limits": {"max_read_bytes": module.MAX_READ_BYTES, "max_total_bytes": module.MAX_TOTAL_BYTES, "max_reads": module.MAX_READS}}
    for index, size in enumerate((4, 8), 1):
        assert messages[index] == {"type": "bytes", "id": index, "data_base64": base64.b64encode(b"A" * size).decode()}
    settings = client.settings
    assert settings["entrypoint"] == ["/usr/bin/timeout"]
    assert settings["command"] == ["--signal=KILL", "55s", "/app/.venv/bin/python", "-m", "app.services.window_visualization_worker"]
    assert settings["auto_remove"] and settings["read_only"] and settings["network_mode"] == "none"
    assert settings["user"] == "65534:65534" and settings["cap_drop"] == ["ALL"]
    assert settings["security_opt"] == ["no-new-privileges:true"] and settings["pids_limit"] == 32
    assert settings["mem_limit"] == settings["memswap_limit"] == "512m"
    assert not {"mounts", "volumes", "ports", "privileged"} & settings.keys()
    assert client.cleaned and client.closed and client.waited and client.wire.closed and client.wire.halfclosed


@pytest.mark.parametrize("change", [{"offset": -1}, {"offset": True}, {"offset": 1024**3}, {"offset": 1024**3 - 2},
    {"length": 0}, {"length": -1}, {"length": True}, {"length": 1.0}, {"length": module.MAX_READ_BYTES + 1},
    {"id": 0}, {"id": True}, {"id": 2}, {"path": "/private/secret"}, {"file_id": "foreign"}, {"type": "open"}])
def test_bad_range_is_rejected_before_storage(monkeypatch, change):
    client, reads, run = invoke(monkeypatch, frame(line(request(**change))) + frame(line(SUCCESS)))
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert reads == [] and client.cleaned and client.closed


@pytest.mark.parametrize("content", [b"", b"\1\0", frame(b"{}"), frame(b"[]\n"), frame(b"{}\n"),
    frame(b'{"type":"result","ok":true,"ok":true,"data":{}}\n'),
    frame(b'{"type":"result","ok":true,"data":{"value":NaN}}\n'),
    frame(line({"type": "result", "ok": 1, "data": {}})), frame(line({"type": "result", "ok": True, "data": []})),
    frame(line({"type": "result", "ok": False, "error": "/private/secret"})),
    frame(line(SUCCESS)) + frame(line(SUCCESS)), frame(line(SUCCESS)) + frame(line(request())),
    frame(line(SUCCESS)) + frame(b" \n"), frame(line(SUCCESS)) + frame(b"log", 2),
    frame(line(SUCCESS)) + b"\1\0", bytes([3, 0, 0, 0, 0, 0, 0, 0])])
def test_invalid_partial_or_duplicate_terminal_never_succeeds(monkeypatch, content):
    client, _, run = invoke(monkeypatch, content, removed=True)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.cleaned and client.closed and not client.waited


def test_rejected_terminal_and_capped_stderr_do_not_leak_logs(monkeypatch):
    client, _, run = invoke(monkeypatch, frame(b"/private/secret patient record", 2) + frame(line({"type": "result", "ok": False, "error": "rejected"})))
    assert run() == {"ok": False, "error": "rejected"}
    assert client.cleaned


@pytest.mark.parametrize("limits", [
    {"max_read_bytes": 4, "max_total_bytes": 4, "max_reads": 3},
    {"max_read_bytes": 4, "max_total_bytes": 12, "max_reads": 1},
])
def test_cumulative_bytes_and_request_count_are_independent(monkeypatch, limits):
    client, reads, run = invoke(monkeypatch, frame(line(request())) + frame(line(request(id=2))) + frame(line(SUCCESS)), limits=limits)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert reads == [(0, 4)] and client.cleaned


def test_duplicate_read_id_is_not_replayed(monkeypatch):
    _, reads, run = invoke(monkeypatch, frame(line(request())) * 2 + frame(line(SUCCESS)))
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert reads == [(0, 4)]


@pytest.mark.parametrize("result", [b"", b"AA", bytearray(b"AAAA"), b"AAAAA"])
def test_short_overlong_or_nonbytes_storage_results_fail_closed(monkeypatch, result):
    client, _, run = invoke(monkeypatch, frame(line(request())), read=lambda *_: result)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.cleaned


def test_cancel_between_range_and_reply_removes_worker_without_sending_bytes(monkeypatch):
    cancelled = threading.Event()
    def read(*_):
        cancelled.set()
        return b"AAAA"
    client, _, run = invoke(monkeypatch, frame(line(request())) + frame(line(SUCCESS)), read=read, cancelled=cancelled)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert len(bytes(client.wire.sent).splitlines()) == 1 and client.cleaned


@pytest.mark.parametrize("status", [1, 124, 137])
def test_nonzero_exit_after_valid_result_still_fails(monkeypatch, status):
    client, _, run = invoke(monkeypatch, frame(line(SUCCESS)), status=status)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.cleaned and client.closed


def test_stderr_and_declared_output_allocation_caps(monkeypatch):
    for stream, size in ((2, module.MAX_STDERR_BYTES + 1), (1, module.MAX_OUTPUT_BYTES + 1)):
        client, _, run = invoke(monkeypatch, bytes([stream, 0, 0, 0]) + size.to_bytes(4, "big"))
        with pytest.raises(module.VisualizationWorkerError):
            run()
        assert client.cleaned


def test_transport_and_storage_errors_are_safe(monkeypatch):
    def read(*_):
        raise RuntimeError("/private/secret endpoint")
    client, _, run = invoke(monkeypatch, frame(line(request())), read=read)
    with pytest.raises(module.VisualizationWorkerError) as failure:
        run()
    assert "secret" not in str(failure.value) and client.cleaned


def test_ambiguous_create_uses_exact_random_name_cleanup(monkeypatch):
    client, _, run = invoke(monkeypatch, b"")
    def create(**_):
        raise RuntimeError("secret Docker endpoint")
    client.create = create
    with pytest.raises(module.VisualizationWorkerError) as failure:
        run()
    assert "secret" not in str(failure.value) and client.cleaned and client.closed
