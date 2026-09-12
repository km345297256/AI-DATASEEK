"""Real, isolated JSONL-window checks with synthetic sources only.

Use --build-test-image to copy the two new stdlib worker modules into a temporary
image derived from the already-installed sandbox. No packages are downloaded,
no service is updated, and the worker itself has no mounts or network. The
temporary image tag and every exact container created here are removed.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import io
import json
import tarfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import docker

from app.application.services.file_service import FileService
from app.application.services.window_visualization import window_visualization
from app.application.services.unified_visualization import normalize_result, check_output_budget
from app.core.config import get_settings
from app.domain.models.file import FileInfo
from app.infrastructure.external.sandbox import window_visualization_worker as gateway


class SyntheticRecording:
    def __init__(self, fmt="edf", plus=False, *, records=1000000, discontinuous=False):
        self.fmt, self.samples = fmt, [1000, 250]
        labels = ["EEG C3", "Status" if fmt == "bdf" else "ECG I"]
        if plus:
            self.samples.append(32)
            labels.append(fmt.upper() + " Annotations")
        width = 2 if fmt == "edf" else 3
        low, high = -(2**(width * 8 - 1)), 2**(width * 8 - 1) - 1
        count = len(labels)

        def field(value, size):
            value = str(value).encode("ascii")
            assert len(value) <= size
            return value.ljust(size, b" ")

        variant = fmt.upper() + ("+D" if discontinuous else "+C") if plus or discontinuous else ""
        header = b"0       " if fmt == "edf" else b"\xffBIOSEMI"
        header += field("PRIVATE PATIENT", 80) + field("PRIVATE RECORD /Users/private", 80)
        header += b"01.01.24" + b"12.34.56" + field((count + 1) * 256, 8)
        header += field(variant, 44) + field(records, 8) + field(1, 8) + field(count, 4)
        for values, size in ((labels, 16), (["PRIVATE SENSOR"] * count, 80), (["uV"] * count, 8),
                ([-100] * count, 8), ([100] * count, 8), ([low] * count, 8), ([high] * count, 8),
                (["PRIVATE FILTER"] * count, 80), (self.samples, 8), ([""] * count, 32)):
            header += b"".join(field(value, size) for value in values)
        self.header, self.record_bytes = header, sum(self.samples) * width
        self.info = FileInfo(file_id="synthetic-opaque", user_id="synthetic-owner", filename="synthetic." + fmt,
            size=len(header) + self.record_bytes * records, metadata={"sha256": "synthetic-revision"})
        self.reads = []

    async def get_file_info(self, file_id, user_id):
        return self.info.model_copy(deep=True) if file_id == self.info.file_id and user_id == self.info.user_id else None

    async def download_file_range(self, file_id, user_id, *, offset, length):
        assert file_id == self.info.file_id and user_id == self.info.user_id
        data = self.read(offset, length)
        return data, self.info.model_copy(deep=True)

    def read(self, offset, length):
        assert 0 <= offset < self.info.size and 0 < length <= min(1024**2, self.info.size - offset)
        self.reads.append((offset, length))
        if offset < len(self.header):
            assert offset + length <= len(self.header)
            return self.header[offset:offset + length]
        return b"\0" * length

    async def download_file(self, *_):
        raise AssertionError("Unbounded source materialization forbidden")


class Plugin(SimpleNamespace):
    def matches_filename(self, value):
        return value.endswith((".edf", ".bdf"))


class Catalog:
    def __init__(self):
        self.plugin = Plugin(id="viz-edf-signals", reader="edf", adapter="signal-window", version="1.0.0",
            capabilities=SimpleNamespace(operations=["preview"], input_mode="window", shared=False),
            limits=SimpleNamespace(max_input_bytes=8 * 1024**2, max_output_bytes=2 * 1024**2))

    async def require_enabled(self, owner, plugin_id):
        assert owner == "synthetic-owner" and plugin_id == self.plugin.id
        return copy.deepcopy(self.plugin)

    async def list_for_user(self, owner):
        assert owner == "synthetic-owner"
        return SimpleNamespace(revision="a" * 64)


def inspect(container):
    container.reload()
    attrs, host = container.attrs, container.attrs["HostConfig"]
    assert attrs["Config"]["User"] == "65534:65534"
    assert host["ReadonlyRootfs"] and host["NetworkMode"] == "none" and not host["Privileged"]
    assert host["CapDrop"] == ["ALL"] and "no-new-privileges:true" in host["SecurityOpt"]
    assert not host.get("Binds") and not attrs.get("Mounts") and not host.get("PortBindings")
    assert not host.get("Devices") and not host.get("DeviceRequests") and not host.get("CapAdd")
    assert host["Memory"] == host["MemorySwap"] == 512 * 1024**2
    assert host["NanoCpus"] == 1000000000 and host["PidsLimit"] == 32
    assert host["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=16m,mode=1777"}
    assert host["AutoRemove"] and host["LogConfig"]["Type"] == "none"
    assert attrs["Config"]["Entrypoint"] == ["/usr/bin/timeout"]


@contextlib.contextmanager
def observe(owned, verified, *, body=None, on_start=None):
    """Inspect real SDK resources; never replace parsing or byte transport."""
    original = gateway.docker.from_env

    class Container:
        def __init__(self, real): self.real = real
        def __getattr__(self, name): return getattr(self.real, name)
        def start(self):
            self.real.start()
            if on_start: on_start()

    class Containers:
        def __init__(self, real): self.real = real
        def get(self, name): return self.real.get(name)
        def create(self, **kwargs):
            assert kwargs["command"] == ["--signal=KILL", "55s", "/app/.venv/bin/python", "-m", "app.services.window_visualization_worker"]
            if body is not None:
                # Controlled negative-test program only; retain production
                # PID1 deadline and every container isolation parameter.
                kwargs["command"] = ["--signal=KILL", "55s", "/app/.venv/bin/python", "-c", body]
            real = self.real.create(**kwargs)
            owned.append(real.id)
            inspect(real)
            verified.append(real.id)
            return Container(real)

    class Client:
        def __init__(self, real): self.real, self.containers = real, Containers(real.containers)
        def close(self): self.real.close()

    gateway.docker.from_env = lambda *args, **kwargs: Client(original(*args, **kwargs))
    try:
        yield
    finally:
        gateway.docker.from_env = original


def temporary_image(client, base, source):
    client.images.get(base)  # Require an installed base; no implicit pull.
    tag = "ai-dataseek-window-check:" + uuid.uuid4().hex
    files = {"Dockerfile": (f"FROM {base}\nCOPY window_visualization_worker.py bounded_signal_readers.py /app/app/services/\n").encode()}
    for name in ("window_visualization_worker.py", "bounded_signal_readers.py"):
        files[name] = (source / "sandbox/app/services" / name).read_bytes()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(content), 0o644
            archive.addfile(entry, io.BytesIO(content))
    buffer.seek(0)
    client.images.build(fileobj=buffer, custom_context=True, tag=tag, pull=False, rm=True, forcerm=True, network_mode="none")
    return tag


async def positive(image, fmt, plus):
    storage, catalog = SyntheticRecording(fmt, plus), Catalog()
    channels = [0, 1] if fmt == "edf" else [0]
    request = SimpleNamespace(plugin_id=catalog.plugin.id, version=None, kind="series",
        options={"channels": channels, "start_seconds": 900000, "duration_seconds": .2})
    result = await window_visualization(FileService(storage), catalog, image, storage.info.file_id, storage.info.user_id, request)
    normalized = normalize_result(result)
    check_output_budget(normalized, catalog.plugin)
    assert normalized.kind == "series" and [v["sample_rate"] for v in result["series"]] == ([1000, 250] if fmt == "edf" else [1000])
    assert all(v["x"][0] == 900000 for v in result["series"])
    assert storage.info.size > 1024**3 and sum(n for _, n in storage.reads) < 2048
    assert result["metadata"]["read_bytes"] == sum(n for _, n in storage.reads)
    assert result["metadata"]["read_requests"] == len(storage.reads)
    assert all(offset > 1024**3 for offset, _ in storage.reads[2:])
    assert not any(marker in json.dumps(result) for marker in ("PRIVATE", "/Users/", "01.01.24", "12.34.56"))
    return {"format": fmt, "variant": result["metadata"]["variant"], "source_bytes": storage.info.size,
        "read_bytes": result["metadata"]["read_bytes"], "read_requests": len(storage.reads),
        "samples": [len(v["y"]) for v in result["series"]], "public_output_bytes": len(normalized.model_dump_json().encode())}


def direct(image, storage, cancelled=None):
    return gateway.run_window_visualization_worker(image, size=storage.info.size, reader="edf", kind="series", format=storage.fmt,
        options={"channels": [0], "duration_seconds": 1}, read_range=storage.read, cancelled=cancelled or threading.Event(),
        limits={"max_read_bytes": 1024**2, "max_total_bytes": 8 * 1024**2, "max_reads": 128})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Existing sandbox base image")
    parser.add_argument("--build-test-image", action="store_true")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    base = args.image or get_settings().sandbox_image
    if not base:
        raise SystemExit("Configure the existing sandbox image")
    client = docker.from_env(timeout=10)
    owned, verified, successes, rejected = [], [], [], []
    test_image = None
    started = time.monotonic()
    try:
        image = test_image = temporary_image(client, base, args.source_root) if args.build_test_image else None
        image = image or base
        with observe(owned, verified):
            for fmt, plus in (("edf", False), ("edf", True), ("bdf", False), ("bdf", True)):
                successes.append(asyncio.run(positive(image, fmt, plus)))
            invalid = SyntheticRecording(discontinuous=True)
            assert direct(image, invalid) == {"ok": False, "error": "rejected"}
            rejected.append("discontinuous-EDF-D")
        terminal = 'import json,sys; sys.stdin.buffer.readline(); print(json.dumps({"type":"result","ok":True,"data":{}}),flush=True); '
        for label, extra in (("duplicate-terminal", 'print(json.dumps({"type":"result","ok":True,"data":{}}),flush=True)'),
                             ("stderr-after-terminal", 'sys.stdin.buffer.read(); print("PRIVATE DIAGNOSTIC",file=sys.stderr,flush=True)')):
            with observe(owned, verified, body=terminal + extra):
                try:
                    direct(image, SyntheticRecording())
                except gateway.VisualizationWorkerError as error:
                    assert "PRIVATE" not in str(error)
                else:
                    raise AssertionError(label + ": malformed terminal was accepted")
                rejected.append(label)
        oversized = 'import json,sys; sys.stdin.buffer.readline(); print(json.dumps({"type":"result","ok":True,"data":{"oversized":"x"*(2*1024*1024)}}),flush=True)'
        with observe(owned, verified, body=oversized):
            try:
                direct(image, SyntheticRecording())
            except gateway.VisualizationWorkerError:
                rejected.append("output-budget")
            else:
                raise AssertionError("Oversized window result was accepted")
        blocked = "import sys,time; sys.stdin.buffer.readline(); time.sleep(20)"
        cancelled = threading.Event()
        def cancel_after_start():
            timer = threading.Timer(.2, cancelled.set)
            timer.daemon = True
            timer.start()
        with observe(owned, verified, body=blocked, on_start=cancel_after_start):
            try:
                direct(image, SyntheticRecording(), cancelled)
            except gateway.VisualizationWorkerError:
                rejected.append("cancelled-worker")
            else:
                raise AssertionError("Cancelled worker returned")
        previous = gateway.WORKER_TIMEOUT_SECONDS
        try:
            gateway.WORKER_TIMEOUT_SECONDS = 3
            with observe(owned, verified, body=blocked):
                try:
                    direct(image, SyntheticRecording())
                except gateway.VisualizationWorkerError:
                    rejected.append("timed-out-worker")
                else:
                    raise AssertionError("Timed-out worker returned")
        finally:
            gateway.WORKER_TIMEOUT_SECONDS = previous
        for container_id in owned:
            try:
                client.containers.get(container_id)
            except docker.errors.NotFound:
                continue
            raise AssertionError("Owned window worker remains")
        print(json.dumps({"status": "passed", "positive_cases": successes, "safe_rejections": rejected,
            "isolation_verified": len(verified), "network": "none", "host_mounts": 0,
            "nonroot": "65534:65534", "read_only_root": True, "owned_workers_remaining": 0,
            "user_files_accessed": 0, "services_updated": 0, "public_envelope_validated": True,
            "elapsed_seconds": round(time.monotonic() - started, 3)}, ensure_ascii=False, indent=2))
    finally:
        try:
            for container_id in owned:
                try:
                    container = client.containers.get(container_id)
                except docker.errors.NotFound:
                    continue
                gateway._remove_worker(container)
            if test_image:
                client.images.remove(test_image, noprune=True)
        finally:
            client.close()


if __name__ == "__main__":
    main()
