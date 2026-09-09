import json
import socket
import threading

import pytest

from app.infrastructure.external.sandbox import visualization_worker as module


def frame(payload, stream=1):
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


class Wire:
    def __init__(self, response):
        self.response = response
        self.sent = bytearray()
        self.closed = False
        self.halfclosed = False

    def settimeout(self, value):
        self.timeout = value

    def send(self, data):
        count = min(len(data), 17)
        self.sent.extend(data[:count])
        return count

    def recv(self, size):
        chunk, self.response = self.response[:min(size, 7)], self.response[min(size, 7):]
        return chunk

    def shutdown(self, direction):
        assert direction == socket.SHUT_WR
        self.halfclosed = True

    def close(self):
        self.closed = True


class Container:
    def __init__(self, response):
        self.wire = Wire(response)
        self.removed = False
        self.started = False

    def attach_socket(self, params):
        assert params == {"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1}
        return self.wire

    def start(self):
        self.started = True

    def wait(self, timeout):
        return {"StatusCode": 0}

    def remove(self, force):
        assert force
        self.removed = True


class Client:
    def __init__(self, response):
        self.container = Container(response)
        self.containers = self
        self.kwargs = None
        self.closed = False

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.container

    def get(self, name):
        assert name.startswith("ai-dataseek-visualization-")
        return self.container

    def close(self):
        self.closed = True


def invoke(monkeypatch, response, *, cancel=None):
    client = Client(response)
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    return client, lambda: module.run_visualization_worker("trusted:test", b"body", reader="netcdf", kind="series",
        options={}, truncated=False, cancelled=cancel or threading.Event())


def test_frame_drain_short_writes_and_least_privilege(monkeypatch):
    expected = {"ok": True, "data": {"value": 1}}
    client, run = invoke(monkeypatch, frame(b"diagnostic", 2) + frame(json.dumps(expected).encode()))
    assert run() == expected
    settings = client.kwargs
    assert settings["read_only"] and settings["network_mode"] == "none"
    assert settings["cap_drop"] == ["ALL"] and settings["user"] == "65534:65534"
    assert settings["pids_limit"] == 32 and settings["mem_limit"] == "512m"
    assert not {"volumes", "mounts", "ports"} & settings.keys()
    assert "SECRET" not in str(settings["environment"])
    header, body = bytes(client.container.wire.sent).split(b"\n", 1)
    assert json.loads(header)["size"] == len(body) == 4
    assert body == b"body"
    assert client.container.removed and client.closed and client.container.wire.closed and client.container.wire.halfclosed


@pytest.mark.parametrize("response", [
    bytes([1, 0, 0, 0]) + (module.MAX_CAPTURE_BYTES + 1).to_bytes(4, "big"),
    frame(b"invalid json"), frame(b"[]"), frame(b'{"ok":1}'),
    bytes([3, 0, 0, 0, 0, 0, 0, 0]), b"\1\0",
])
def test_bad_frames_fail_closed_and_remove(monkeypatch, response):
    client, run = invoke(monkeypatch, response)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.container.removed and client.closed and client.container.wire.closed


def test_pre_cancel_and_ambiguous_create_cleanup_exact_worker(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    client, run = invoke(monkeypatch, b"", cancel=cancel)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.container.removed and client.closed and client.kwargs is None


def test_native_error_never_exposes_private_exception(monkeypatch):
    client, run = invoke(monkeypatch, b"")
    def create(**kwargs):
        raise RuntimeError("/Users/private/credential")
    client.create = create
    with pytest.raises(module.VisualizationWorkerError) as failure:
        run()
    assert "private" not in str(failure.value)
    assert client.container.removed and client.closed
