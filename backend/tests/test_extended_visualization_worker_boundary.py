"""V2 framing, crash-survival deadline and Docker auto-removal races."""
import json
import socket
import threading
from types import SimpleNamespace

import docker
import pytest

from app.infrastructure.external.sandbox import extended_visualization_worker as module


def frame(payload, stream=1):
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


class Wire:
    def __init__(self, response):
        self.response = response
        self.sent = bytearray()
        self.closed = self.halfclosed = self.eof = False

    def settimeout(self, value):
        assert value == .5

    def send(self, data):
        count = min(len(data), 17)
        self.sent.extend(data[:count])
        return count

    def recv(self, size):
        count = min(size, 7)
        chunk, self.response = self.response[:count], self.response[count:]
        self.eof = not chunk
        return chunk

    def shutdown(self, direction):
        assert direction == socket.SHUT_WR
        self.halfclosed = True

    def close(self):
        self.closed = True


class Container:
    def __init__(self, response, *, auto_removed=False, exit_code=0):
        self.wire = Wire(response)
        self.auto_removed, self.exit_code = auto_removed, exit_code
        self.removal_attempted = self.started = self.waited = False

    def attach_socket(self, params):
        assert params == {"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1}
        return self.wire

    def start(self):
        self.started = True

    def wait(self, timeout):
        assert timeout == 3 and self.wire.eof and not self.wire.response
        self.waited = True
        if self.auto_removed:
            raise docker.errors.NotFound("automatically removed")
        return {"StatusCode": self.exit_code}

    def remove(self, force):
        assert force
        self.removal_attempted = True
        if self.auto_removed:
            raise docker.errors.NotFound("automatically removed")


class Client:
    def __init__(self, response, **kwargs):
        self.container = Container(response, **kwargs)
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


def invoke(monkeypatch, response, *, cancel=None, **kwargs):
    client = Client(response, **kwargs)
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    return client, lambda: module.run_extended_visualization_worker(
        "trusted:test", b"body", reader="tabular", kind="table", format="csv",
        options={}, truncated=False, cancelled=cancel or threading.Event(),
    )


@pytest.mark.parametrize("auto_removed", [False, True])
@pytest.mark.parametrize("expected", [{"ok": True, "data": {"value": 1}}, {"ok": False, "error": "Safe rejection"}])
def test_complete_frame_drain_and_fixed_crash_safe_container(monkeypatch, auto_removed, expected):
    client, run = invoke(monkeypatch, frame(b"diagnostic", 2) + frame(json.dumps(expected).encode()),
                         auto_removed=auto_removed)
    assert run() == expected
    settings = client.kwargs
    assert settings["entrypoint"] == ["/usr/bin/timeout"]
    assert settings["command"] == ["--signal=KILL", "55s", "/app/.venv/bin/python", "-m", "app.services.extended_visualization_worker"]
    assert settings["auto_remove"] is True
    assert module.WORKER_TIMEOUT_SECONDS < module.CONTAINER_TIMEOUT_SECONDS == 55
    assert settings["read_only"] and settings["network_mode"] == "none"
    assert settings["cap_drop"] == ["ALL"] and settings["user"] == "65534:65534"
    assert settings["security_opt"] == ["no-new-privileges:true"]
    assert settings["pids_limit"] == 96 and settings["mem_limit"] == settings["memswap_limit"] == "1g"
    assert not {"volumes", "mounts", "ports", "privileged"} & settings.keys()
    header, body = bytes(client.container.wire.sent).split(b"\n", 1)
    assert json.loads(header) == {"contract_version": 2, "format": "csv", "size": 4, "reader": "tabular", "kind": "table", "options": {}, "truncated": False}
    assert body == b"body"
    assert client.container.removal_attempted and client.container.waited and client.closed
    assert client.container.wire.closed and client.container.wire.halfclosed


@pytest.mark.parametrize("response", [
    b"", b"\1\0", frame(b'{"ok":true,"data":{}') ,
    frame(b'{"ok":true,"data":{}}') + b"\1\0",
    frame(b'{"ok":true,"data":{}}') + frame(b"trailing non-json"),
    frame(b'{"ok":true,"data":{}}') + bytes([2, 0, 0, 0, 0, 0, 0, 5]) + b"ab",
    bytes([1, 0, 0, 0]) + (module.MAX_CAPTURE_BYTES + 1).to_bytes(4, "big"),
    bytes([3, 0, 0, 0, 0, 0, 0, 0]), bytes([1, 1, 0, 0, 0, 0, 0, 0]),
    frame(b"invalid json"), frame(b"[]"), frame(b'{"ok":1}'),
    frame(b'{"ok":true}'), frame(b'{"ok":true,"data":[]}'),
    frame(b'{"ok":false}'), frame(b'{"ok":false,"error":null}'),
    frame(b'{"ok":true,"data":{},"unexpected":1}'),
])
def test_auto_removal_never_accepts_incomplete_or_invalid_protocol(monkeypatch, response):
    client, run = invoke(monkeypatch, response, auto_removed=True)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert not client.container.waited
    assert client.container.removal_attempted and client.closed and client.container.wire.closed


@pytest.mark.parametrize("exit_code", [1, 124, 137])
def test_known_nonzero_exit_rejects_even_complete_json(monkeypatch, exit_code):
    client, run = invoke(monkeypatch, frame(b'{"ok":true,"data":{}}'), exit_code=exit_code)
    with pytest.raises(module.VisualizationWorkerError, match="进程异常"):
        run()
    assert client.container.removal_attempted and client.closed


def test_cancellation_after_eof_is_not_masked_by_auto_removal(monkeypatch):
    cancelled = threading.Event()
    client, run = invoke(monkeypatch, frame(b'{"ok":true,"data":{}}'), cancel=cancelled, auto_removed=True)
    original_wait = client.container.wait
    def wait(timeout):
        cancelled.set()
        return original_wait(timeout)
    client.container.wait = wait
    with pytest.raises(module.VisualizationWorkerError, match="取消或超时"):
        run()
    assert client.container.removal_attempted and client.closed


def test_pre_cancel_and_ambiguous_create_cleanup_exact_name(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    client, run = invoke(monkeypatch, b"", cancel=cancelled, auto_removed=True)
    with pytest.raises(module.VisualizationWorkerError):
        run()
    assert client.kwargs is None and client.container.removal_attempted and client.closed


def test_ambiguous_create_and_lookup_notfound_preserve_safe_error(monkeypatch):
    client, run = invoke(monkeypatch, b"")
    def create(**kwargs):
        raise RuntimeError("/private/secret/path")
    def get(name):
        assert name.startswith("ai-dataseek-visualization-")
        raise docker.errors.NotFound("automatically removed")
    client.create, client.get = create, get
    with pytest.raises(module.VisualizationWorkerError) as failure:
        run()
    assert "private" not in str(failure.value) and client.closed


def test_stdout_budget_remains_bounded_when_auto_remove_is_on(monkeypatch):
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 10)
    client, run = invoke(monkeypatch, frame(b"12345678901"), auto_removed=True)
    with pytest.raises(module.VisualizationWorkerError, match="显示上限"):
        run()
    assert client.container.removal_attempted and client.closed


def test_auto_remove_409_race_is_confirmed_by_exact_id_disappearing(monkeypatch):
    client, run = invoke(monkeypatch, frame(b'{"ok":true,"data":{}}'))
    reloaded = []
    def remove(force):
        assert force
        raise docker.errors.APIError("removal in progress", response=SimpleNamespace(status_code=409))
    def reload():
        reloaded.append(True)
        if len(reloaded) == 2:
            raise docker.errors.NotFound("automatically removed")
    client.container.remove, client.container.reload = remove, reload
    assert run() == {"ok": True, "data": {}}
    assert len(reloaded) == 2 and client.closed


def test_unrelated_cleanup_error_is_safe_not_swallowed(monkeypatch):
    client, run = invoke(monkeypatch, frame(b'{"ok":true,"data":{}}'))
    def remove(force):
        raise docker.errors.APIError("/private/host/path", response=SimpleNamespace(status_code=500))
    client.container.remove = remove
    with pytest.raises(module.VisualizationWorkerError, match="清理暂不可用") as failure:
        run()
    assert "private" not in str(failure.value) and client.closed
