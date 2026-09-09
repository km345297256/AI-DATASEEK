"""One-shot scientific readers in the existing sandbox image, not the API host."""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import docker

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_BYTES = MAX_OUTPUT_BYTES + 64 * 1024
WORKER_TIMEOUT_SECONDS = 50
# PID 1 enforces this independently of the backend process/thread. The normal
# gateway deadline is shorter so cancellation still removes the reader promptly.
CONTAINER_TIMEOUT_SECONDS = 55


from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError


def _remove_worker(container) -> None:
    """Force-remove one exact worker, tolerating Docker's AutoRemove races."""
    try:
        container.remove(force=True)
    except docker.errors.NotFound:
        return
    except docker.errors.APIError as error:
        if error.status_code != 409:
            raise VisualizationWorkerError("预览容器清理暂不可用。") from error
        # Docker can respond 409 while its own removal is already in progress.
        # Do not broadly swallow conflicts: confirm this exact ID disappears.
        cleanup_deadline = time.monotonic() + 3
        while time.monotonic() < cleanup_deadline:
            try:
                container.reload()
            except docker.errors.NotFound:
                return
            time.sleep(.05)
        raise VisualizationWorkerError("预览容器清理未完成。") from error


def run_extended_visualization_worker(image: str, data: bytes, *, reader: str, kind: str,
                             options: dict, truncated: bool, format: str,
                             cancelled: threading.Event) -> dict:
    """No host mounts, ports, network, credentials, or model calls.

    Docker's framed attachment is drained with an aggregate stdout/stderr cap;
    a timed-out/cancelled reader is forcibly removed in the same worker thread.
    A fixed PID-1 deadline and Docker auto-removal also bound a reader whose
    gateway is killed before its finally block can run.
    An exact random name permits cleanup after an ambiguous create response.
    """
    name = "ai-dataseek-visualization-" + uuid.uuid4().hex
    client = docker.from_env(timeout=5)
    container = None
    attached = None
    deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS

    def check():
        if cancelled.is_set() or time.monotonic() > deadline:
            raise VisualizationWorkerError("预览已取消或超时。")

    try:
        check()
        container = client.containers.create(
            image=image, name=name, entrypoint=["/usr/bin/timeout"],
            command=["--signal=KILL", f"{CONTAINER_TIMEOUT_SECONDS}s", "/app/.venv/bin/python",
                     "-m", "app.services.extended_visualization_worker"], working_dir="/app",
            auto_remove=True,
            user="65534:65534", stdin_open=True, tty=False, network_mode="none",
            read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="1g", memswap_limit="1g", nano_cpus=1_000_000_000,
            pids_limit=96, tmpfs={"/tmp": "rw,noexec,nosuid,size=512m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            labels={"ai-dataseek.component": "visualization-preview"},
            log_config=docker.types.LogConfig(type="none"),
        )
        check()
        attached = container.attach_socket(params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
        wire = getattr(attached, "_sock", attached)
        wire.settimeout(0.5)
        container.start()
        header = json.dumps(dict(contract_version=2, format=format, size=len(data), reader=reader, kind=kind,
                                 options=options, truncated=truncated), ensure_ascii=False).encode() + b"\n"
        if len(header) > 8192:
            raise VisualizationWorkerError("预览参数过长。")
        for part in (header, data):
            view = memoryview(part)
            sent = 0
            while sent < len(view):
                check()
                try:
                    count = wire.send(view[sent:sent + 65536])
                except socket.timeout:
                    continue
                if not count:
                    raise VisualizationWorkerError("预览输入传输失败。")
                sent += count
        # The worker requires EOF after the declared payload. Half-close only
        # stdin; retain stdout/stderr for the bounded result.
        wire.shutdown(socket.SHUT_WR)

        def receive(count, *, allow_eof=False):
            pieces = bytearray()
            while len(pieces) < count:
                check()
                try:
                    chunk = wire.recv(min(count - len(pieces), 65536))
                except socket.timeout:
                    continue
                if not chunk:
                    if allow_eof and not pieces:
                        return b""
                    raise VisualizationWorkerError("预览输出传输不完整。")
                pieces.extend(chunk)
            return bytes(pieces)

        result = bytearray()
        captured = 0
        while True:
            frame = receive(8, allow_eof=True)
            if not frame:
                break
            stream_id = frame[0]
            length = int.from_bytes(frame[4:8], "big")
            captured += length
            if frame[1:4] != b"\0\0\0" or stream_id not in {1, 2} or captured > MAX_CAPTURE_BYTES:
                raise VisualizationWorkerError("预览输出超过安全上限。")
            payload = receive(length)
            if stream_id == 1:
                result.extend(payload)
                if len(result) > MAX_OUTPUT_BYTES:
                    raise VisualizationWorkerError("预览结果超过显示上限。")
        check()
        # Auto-removal may beat wait(), but it must never let us accept a
        # partial frame, an unterminated stream or a JSON prefix. Only parse
        # after a clean framing EOF, and validate the complete envelope first.
        try:
            decoded = json.loads(result)
        except (ValueError, UnicodeError) as error:
            raise VisualizationWorkerError("预览返回了无效的协议数据。") from error
        if not isinstance(decoded, dict) or type(decoded.get("ok")) is not bool:
            raise VisualizationWorkerError("预览返回了无效的协议数据。")
        if ((decoded["ok"] and (set(decoded) != {"ok", "data"} or not isinstance(decoded["data"], dict)))
                or (not decoded["ok"] and (set(decoded) != {"ok", "error"} or not isinstance(decoded["error"], str)))):
            raise VisualizationWorkerError("预览返回了无效的协议数据。")
        try:
            outcome = container.wait(timeout=3)
        except docker.errors.NotFound:
            # A successful complete EOF/envelope is authoritative when Docker
            # has already reaped this exact auto-removing one-shot container.
            pass
        else:
            if outcome.get("StatusCode") != 0:
                raise VisualizationWorkerError("预览进程异常退出或触及资源限制。")
        check()
        return decoded
    except VisualizationWorkerError:
        raise
    except Exception as error:
        # Never expose Docker endpoints, image paths, file data or parser logs.
        raise VisualizationWorkerError("隔离预览暂不可用，请确认沙箱镜像已更新。") from error
    finally:
        if attached is not None:
            try:
                attached.close()
            except Exception:
                pass
        try:
            if container is None:
                try:
                    container = client.containers.get(name)
                except docker.errors.NotFound:
                    pass
            if container is not None:
                _remove_worker(container)
        finally:
            client.close()
