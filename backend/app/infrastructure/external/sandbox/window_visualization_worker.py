"""Private JSONL range broker for one already-authorized source.

The isolated worker sees a size and bounded bytes, never storage identities,
host mounts, paths, credentials, URLs or a general-purpose file API.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable

import docker

from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError

PROTOCOL = "dataseek-window-v1"
MAX_SOURCE_BYTES = 8 * 1024**3
MAX_READ_BYTES = 1024**2
MAX_TOTAL_BYTES = 8 * 1024**2
MAX_READS = 128
MAX_OUTPUT_BYTES = 2 * 1024**2
MAX_CONTROL_LINE_BYTES = 512
MAX_STDERR_BYTES = 64 * 1024
WORKER_TIMEOUT_SECONDS = 50
CONTAINER_TIMEOUT_SECONDS = 55
WINDOW_PROFILES = {
    "radar-window": {"adapter":"radar-window", "formats":{"h5","hdf5"}, "kinds":{"tree","image"}, "default_kind":"tree", "total":MAX_TOTAL_BYTES, "reads":MAX_READS, "output":MAX_OUTPUT_BYTES},
    "ugrid-window": {"adapter":"ugrid-window", "formats":{"nc","nc4","netcdf","h5","hdf5","hdf"}, "kinds":{"tree","geometry"}, "default_kind":"tree", "total":MAX_TOTAL_BYTES, "reads":MAX_READS, "output":MAX_OUTPUT_BYTES},
    "dicom-window": {"adapter":"dicom-window", "formats":{"dcm","dicom"}, "kinds":{"tree","image"}, "default_kind":"tree", "total":MAX_TOTAL_BYTES, "reads":MAX_READS, "output":MAX_OUTPUT_BYTES},
    "spatial-window": {"adapter":"spatial-window", "formats":{"h5ad"}, "kinds":{"tree","geometry"}, "default_kind":"tree", "total":MAX_TOTAL_BYTES, "reads":MAX_READS, "output":MAX_OUTPUT_BYTES},
    "pointcloud-window": {"adapter":"pointcloud-window", "formats":{"las"}, "kinds":{"tree","geometry"}, "default_kind":"tree", "total":MAX_TOTAL_BYTES, "reads":MAX_READS, "output":MAX_OUTPUT_BYTES},
    "fcs-window": {"adapter": "fcs-window", "formats": {"fcs"}, "kinds": {"tree", "series"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": MAX_READS, "output": MAX_OUTPUT_BYTES},
    "ripple-window": {"adapter": "ripple-window", "formats": {"rpl"}, "kinds": {"tree", "image", "series"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": 256, "output": MAX_OUTPUT_BYTES},
    "envi-window": {"adapter": "envi-window", "formats": {"hdr"}, "kinds": {"tree", "image", "series"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": 256, "output": MAX_OUTPUT_BYTES},
    "grib-window": {"adapter": "grib-window", "formats": {"grib", "grb", "grib2", "grb2"}, "kinds": {"tree", "image"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": MAX_READS, "output": MAX_OUTPUT_BYTES},
    "seismic-window": {"adapter": "seismic-window", "formats": {"mseed", "miniseed", "sac"}, "kinds": {"tree", "series"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": MAX_READS, "output": MAX_OUTPUT_BYTES},
    "columnar-window": {"adapter": "columnar-window", "formats": {"parquet", "parq", "arrow", "feather"}, "kinds": {"tree", "table"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": MAX_READS, "output": MAX_OUTPUT_BYTES},
    "nexus-window": {"adapter": "nexus-window", "formats": {"nxs", "nx", "h5", "hdf5", "hdf"}, "kinds": {"tree", "series", "image"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": MAX_READS, "output": MAX_OUTPUT_BYTES},
    "edf": {"adapter": "signal-window", "formats": {"edf", "bdf"}, "kinds": {"series"}, "default_kind": "series", "total": MAX_TOTAL_BYTES, "reads": MAX_READS, "output": MAX_OUTPUT_BYTES},
    "array-window": {"adapter": "array-window", "formats": {"h5", "hdf5", "hdf", "nc", "nc4", "netcdf", "mat"}, "kinds": {"tree", "series", "image"}, "default_kind": "tree", "total": MAX_TOTAL_BYTES, "reads": MAX_READS, "output": MAX_OUTPUT_BYTES},
    "czi-window": {"adapter": "czi-window", "formats": {"czi"}, "kinds": {"tree", "image"}, "default_kind": "tree", "total": 32 * 1024**2, "reads": 4096, "output": 8 * 1024**2},
    "instrument-window": {"adapter": "instrument-image", "formats": {"edf", "spe"}, "kinds": {"tree", "image"}, "default_kind": "tree", "total": 32 * 1024**2, "reads": 2048, "output": 8 * 1024**2},
    "ome-zarr": {"adapter": "ome-zarr", "formats": {"zarr"}, "kinds": {"tree", "image"}, "default_kind": "tree", "total": 32 * 1024**2, "reads": 256, "output": MAX_OUTPUT_BYTES},
}
RESOURCE_KEY = re.compile(r"(?:\.zattrs|\.zgroup|(?:0|[1-9][0-9]{0,2})/(?:\.zarray|(?:0|[1-9][0-9]{0,8})(?:[./](?:0|[1-9][0-9]{0,8})){1,4}))\Z")


def validate_resources(resources, size):
    """Private exact-member map; no URL, host path, identity or generic storage."""
    if not isinstance(resources, list) or not 2 <= len(resources) <= 2048:
        raise VisualizationWorkerError("分块资源清单无效。")
    end, keys = 0, set()
    for row in resources:
        if (not isinstance(row, dict) or set(row) != {"key", "offset", "size"}
                or not isinstance(row["key"], str) or len(row["key"]) > 128 or not RESOURCE_KEY.fullmatch(row["key"])
                or row["key"] in keys or type(row["offset"]) is not int or row["offset"] != end
                or type(row["size"]) is not int or not 0 < row["size"] <= 4 * 1024**2):
            raise VisualizationWorkerError("分块资源清单无效。")
        keys.add(row["key"])
        end += row["size"]
    if end != size or not {".zattrs", ".zgroup"} <= keys:
        raise VisualizationWorkerError("分块资源清单无效。")


def validate_limits(limits: dict, reader="edf") -> None:
    profile = WINDOW_PROFILES.get(reader) if isinstance(reader, str) else None
    if profile is None:
        raise VisualizationWorkerError("窗口读取器未获批准。")
    maxima = {"max_read_bytes": MAX_READ_BYTES, "max_total_bytes": profile["total"], "max_reads": profile["reads"]}
    if (not isinstance(limits, dict) or set(limits) != set(maxima)
            or any(type(limits[key]) is not int or not 0 < limits[key] <= maximum for key, maximum in maxima.items())
            or limits["max_read_bytes"] > limits["max_total_bytes"]):
        raise VisualizationWorkerError("窗口读取预算无效。")


def _decode(line: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON")

    try:
        value = json.loads(line, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise VisualizationWorkerError("窗口读取器返回了无效的协议数据。") from None
    if not isinstance(value, dict):
        raise VisualizationWorkerError("窗口读取器返回了无效的协议数据。")
    return value


def run_window_visualization_worker(image: str, *, size: int, reader: str, kind: str,
                                    format: str, options: dict, limits: dict,
                                    read_range: Callable[[int, int], bytes],
                                    cancelled: threading.Event,
                                    max_output_bytes: int = MAX_OUTPUT_BYTES, resources=None) -> dict:
    """Synchronous, single-pending-request broker; callback performs authorization.

    Both this untrusted-wire boundary and the callback independently enforce the
    range budget. Cancellation/timeout removes this exact container. The caller
    retains its admission slot until outstanding storage I/O has also completed.
    """
    validate_limits(limits, reader)
    profile = WINDOW_PROFILES[reader]
    if (type(size) is not int or not 0 < size <= MAX_SOURCE_BYTES
            or kind not in profile["kinds"] or format not in profile["formats"]
            or type(max_output_bytes) is not int or not 0 < max_output_bytes <= profile["output"]):
        raise VisualizationWorkerError("窗口读取器参数无效。")
    if reader == "ome-zarr":
        validate_resources(resources, size)
    elif reader in {"envi-window", "ripple-window"}:
        if reader == "envi-window":
            from app.application.services.envi_window_visualization import validate_envi_resources as validate_pair
        else:
            from app.application.services.ripple_window_visualization import validate_ripple_resources as validate_pair
        try:
            validate_pair(resources, size)
        except ValueError:
            raise VisualizationWorkerError("ENVI 配对资源无效。") from None
    elif resources is not None:
        raise VisualizationWorkerError("此读取器不接受多对象资源。")
    try:
        header = json.dumps({"protocol": PROTOCOL, "type": "init", "size": size,
            "reader": reader, "kind": kind, "format": format, "options": options,
            "limits": limits, **({"resources": resources} if resources is not None else {})}, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    except (ValueError, TypeError):
        raise VisualizationWorkerError("窗口读取器参数无效。") from None
    if len(header) > (256 * 1024 if reader == "ome-zarr" else 8192):
        raise VisualizationWorkerError("窗口读取器参数过长。")
    name = "ai-dataseek-window-" + uuid.uuid4().hex
    client = None
    container = attached = None
    deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS

    def check():
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise VisualizationWorkerError("窗口预览已取消或超时。")

    try:
        check()
        client = docker.from_env(timeout=5)
        container = client.containers.create(
            image=image, name=name, entrypoint=["/usr/bin/timeout"],
            command=["--signal=KILL", f"{CONTAINER_TIMEOUT_SECONDS}s", "/app/.venv/bin/python",
                     "-m", "app.services.window_visualization_worker"], working_dir="/app",
            auto_remove=True, user="65534:65534", stdin_open=True, tty=False,
            network_mode="none", read_only=True, cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"], mem_limit="512m" if reader == "edf" else "1g", memswap_limit="512m" if reader == "edf" else "1g",
            nano_cpus=1_000_000_000, pids_limit=32 if reader == "edf" else 96,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m,mode=1777" if reader == "edf" else "rw,noexec,nosuid,size=512m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1", "HDF5_PLUGIN_PRELOAD": "::"},
            labels={"ai-dataseek.component": "visualization-window"},
            log_config=docker.types.LogConfig(type="none"),
        )
        check()
        attached = container.attach_socket(params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
        wire = getattr(attached, "_sock", attached)
        wire.settimeout(.5)
        container.start()

        def send(data):
            view = memoryview(data)
            sent = 0
            while sent < len(view):
                check()
                try:
                    count = wire.send(view[sent:sent + 65536])
                except socket.timeout:
                    continue
                if not count:
                    raise VisualizationWorkerError("窗口输入传输失败。")
                sent += count

        def receive(count, *, allow_eof=False):
            data = bytearray()
            while len(data) < count:
                check()
                try:
                    chunk = wire.recv(min(count - len(data), 65536))
                except socket.timeout:
                    continue
                if not chunk:
                    if allow_eof and not data:
                        return b""
                    raise VisualizationWorkerError("窗口输出传输不完整。")
                data.extend(chunk)
            return bytes(data)

        send(header)
        stdout = bytearray()
        result = None
        reads = total = captured = stderr_bytes = control_bytes = frames = 0
        capture_limit = max_output_bytes + MAX_STDERR_BYTES + limits["max_reads"] * MAX_CONTROL_LINE_BYTES
        while True:
            frame = receive(8, allow_eof=True)
            if not frame:
                break
            stream, length = frame[0], int.from_bytes(frame[4:8], "big")
            captured += length
            frames += 1
            if frame[1:4] != b"\0\0\0" or stream not in {1, 2} or captured > capture_limit or frames > max(4096, limits["max_reads"] * 4 + 32):
                raise VisualizationWorkerError("窗口输出超过协议预算。")
            if result is not None and length:
                raise VisualizationWorkerError("窗口读取器在终态后继续输出。")
            if stream == 2:
                stderr_bytes += length
                if stderr_bytes > MAX_STDERR_BYTES:
                    raise VisualizationWorkerError("窗口诊断输出超过安全预算。")
                receive(length)  # Discard diagnostics; never log or return them.
                continue
            if len(stdout) + length > max_output_bytes:
                raise VisualizationWorkerError("窗口结果超过显示上限。")
            stdout.extend(receive(length))
            while b"\n" in stdout:
                line, _, remainder = stdout.partition(b"\n")
                stdout = bytearray(remainder)
                if result is not None:
                    raise VisualizationWorkerError("窗口读取器重复发送终态。")
                message = _decode(bytes(line))
                if message.get("type") == "read":
                    control_bytes += len(line) + 1
                    if (len(line) > MAX_CONTROL_LINE_BYTES or control_bytes > limits["max_reads"] * MAX_CONTROL_LINE_BYTES
                            or set(message) != {"type", "id", "offset", "length"}
                            or type(message["id"]) is not int or message["id"] != reads + 1
                            or type(message["offset"]) is not int or type(message["length"]) is not int
                            or not 0 <= message["offset"] < size
                            or not 0 < message["length"] <= min(limits["max_read_bytes"], size - message["offset"])
                            or (resources is not None and not any(row["offset"] <= message["offset"] and message["offset"] + message["length"] <= row["offset"] + row["size"] for row in resources))
                            or reads >= limits["max_reads"] or total + message["length"] > limits["max_total_bytes"]):
                        raise VisualizationWorkerError("窗口读取请求越界或超过预算。")
                    reads += 1
                    total += message["length"]
                    check()
                    data = read_range(message["offset"], message["length"])
                    check()
                    if type(data) is not bytes or len(data) != message["length"]:
                        raise VisualizationWorkerError("窗口范围读取不完整。")
                    send(json.dumps({"type": "bytes", "id": message["id"],
                        "data_base64": base64.b64encode(data).decode("ascii")}, separators=(",", ":")).encode() + b"\n")
                elif message.get("type") == "result":
                    if (type(message.get("ok")) is not bool
                            or (message["ok"] and (set(message) != {"type", "ok", "data"} or not isinstance(message["data"], dict)))
                            or (not message["ok"] and (set(message) != {"type", "ok", "error"} or message["error"] != "rejected"))):
                        raise VisualizationWorkerError("窗口读取器终态无效。")
                    result = {key: value for key, value in message.items() if key != "type"}
                    wire.shutdown(socket.SHUT_WR)
                else:
                    raise VisualizationWorkerError("窗口读取器消息类型无效。")
        check()
        if stdout or result is None:
            raise VisualizationWorkerError("窗口读取器缺少完整终态。")
        try:
            outcome = container.wait(timeout=3)
        except docker.errors.NotFound:
            pass
        else:
            if outcome.get("StatusCode") != 0:
                raise VisualizationWorkerError("窗口读取进程异常退出或触及资源限制。")
        check()
        return result
    except VisualizationWorkerError:
        raise
    except Exception:
        # Includes untrusted parser/transport/storage diagnostics, never exposed.
        raise VisualizationWorkerError("隔离窗口预览暂不可用。") from None
    finally:
        if attached is not None:
            try:
                attached.close()
            except Exception:
                pass
        if client is not None:
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
