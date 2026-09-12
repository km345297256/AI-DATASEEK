"""Bounded, read-only dataset bytes; no Agent, upload, shell, or source-path output."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import PurePosixPath
import socket
import stat
import time
import uuid

import docker

from app.core.config import get_settings
from app.infrastructure.external.sandbox.dataset_mount_validator import (
    _default_local_allowlist,
    docker_host_source_and_candidates,
)

MAX_READ_BYTES = 64 * 1024 * 1024
READ_TIMEOUT_SECONDS = 30
MAX_HEADER_BYTES = 2048
_ERROR_MESSAGE = "Dataset file could not be read safely"


class DatasetPreviewReadError(ValueError):
    """Deliberately contains no source paths, Docker endpoint, or native error."""


def _read_fd_file(source, relative_path, offset, length, *, prefix="/", max_bytes=MAX_READ_BYTES):
    """This fixed function also runs in the helper; keep dependencies stdlib-only.

    Every ancestor and the final file is opened with O_NOFOLLOW. An already
    opened directory descriptor, rather than a second path lookup, anchors each
    subsequent operation. Nonblocking open prevents FIFOs from hanging before
    the regular-file check. The before/after identity protects bounded reads.
    """
    def valid_path(value, absolute):
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
            return False
        path = PurePosixPath(value)
        return (path.is_absolute() == absolute and str(path) == value
                and len(path.parts) <= 256 and "\\" not in value
                and all(ord(char) >= 32 and ord(char) != 127 for char in value)
                and all(part not in {".", "..", ""} for part in path.parts))

    if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_READ_BYTES
            or not valid_path(source, True) or source.startswith("//")
            or not valid_path(relative_path, False)
            or type(offset) is not int or not 0 <= offset <= 2**63 - 1
            or (length is not None and (type(length) is not int or not 0 <= length <= max_bytes))):
        raise ValueError("Invalid dataset read")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open(prefix, directory_flags)
    file_fd = None
    try:
        components = [*PurePosixPath(source).parts[1:], *PurePosixPath(relative_path).parts]
        for component in components[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        file_fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or offset > before.st_size:
            raise ValueError("Invalid dataset file")
        count = before.st_size - offset if length is None else min(length, before.st_size - offset)
        if count > max_bytes:
            raise ValueError("Dataset read exceeds byte budget")
        result = bytearray()
        while len(result) < count:
            data = os.pread(file_fd, min(65536, count - len(result)), offset + len(result))
            if not data:
                raise ValueError("Incomplete dataset file read")
            result.extend(data)
        after = os.fstat(file_fd)
        fields = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev")
        if any(getattr(before, key) != getattr(after, key) for key in fields):
            raise ValueError("Dataset file changed during preview")
        return bytes(result), dict(size=before.st_size, mtime_ns=before.st_mtime_ns,
                                  ctime_ns=before.st_ctime_ns, inode=before.st_ino, device=before.st_dev)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def read_managed_file(root: str, relative_path: str, offset: int = 0,
                      length: int | None = None, *, max_bytes: int = MAX_READ_BYTES) -> tuple[bytes, dict]:
    """Read below the backend's configured managed volume using the same walk."""
    try:
        return _read_fd_file(str(root), relative_path, offset, length, max_bytes=max_bytes)
    except Exception:
        raise DatasetPreviewReadError(_ERROR_MESSAGE) from None


def _stat_fd_files(source, paths, *, prefix="/"):
    """Inspect only an exact, bounded inventory; never recurse or read pixels."""
    if (not isinstance(paths, list) or not 2 <= len(paths) <= 2048
            or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
        raise ValueError("Invalid resource scope")
    result = []
    for path in paths:
        _, metadata = _read_fd_file(source, path, 0, 0, prefix=prefix, max_bytes=1)
        result.append(metadata)
    return result


def stat_managed_files(root, paths):
    try:
        return _stat_fd_files(str(root), paths)
    except Exception:
        raise DatasetPreviewReadError(_ERROR_MESSAGE) from None


def stat_dataset_host_files(source, paths, *, configured_roots=None):
    """One confined helper for a metadata snapshot, not N helper containers."""
    if (not isinstance(paths, list) or not 2 <= len(paths) <= 2048
            or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
        raise DatasetPreviewReadError(_ERROR_MESSAGE)
    data, _ = read_dataset_host_file(source, paths[0] if paths else "invalid", offset=0,
        length=None, configured_roots=configured_roots, max_bytes=1024**2, _scope_paths=paths)
    try:
        value = json.loads(data)
        if (not isinstance(value, list) or len(value) != len(paths)
                or any(not isinstance(item, dict) or set(item) != {"size", "mtime_ns", "ctime_ns", "inode", "device"}
                       or any(type(v) is not int for v in item.values())
                       or any(item[k] < 0 for k in ("size", "inode", "device")) for item in value)):
            raise ValueError("Invalid resource metadata")
        return value
    except Exception:
        raise DatasetPreviewReadError(_ERROR_MESSAGE) from None


def _reader_program() -> str:
    # Send reviewed source, never user code, to the existing sandbox image. This
    # keeps managed and host reads identical without requiring an image rebuild
    # to install another helper module. All variable inputs use bounded stdin.
    return (
        "import os, stat, sys, json\nfrom pathlib import PurePosixPath\n"
        + f"MAX_READ_BYTES = {MAX_READ_BYTES}\n"
        + inspect.getsource(_read_fd_file)
        + inspect.getsource(_stat_fd_files)
        + '''
try:
    raw = sys.stdin.buffer.readline(262145)
    if len(raw) > 262144 or not raw.endswith(b"\\n") or sys.stdin.buffer.read(1):
        raise ValueError("Invalid request")
    request = json.loads(raw)
    source = PurePosixPath(request["source"])
    roots = [PurePosixPath(value) for value in request["roots"]]
    if not any(source == root or root in source.parents for root in roots):
        raise ValueError("Disallowed source")
    if "scope_paths" in request:
        if request["offset"] != 0 or request["length"] is not None or request["max_bytes"] != 1048576:
            raise ValueError("Invalid scope request")
        data = json.dumps(_stat_fd_files(request["source"], request["scope_paths"], prefix="/host"), separators=(",", ":")).encode()
        if len(data) > 1048576:
            raise ValueError("Resource metadata budget")
        metadata = dict(size=len(data), mtime_ns=0, ctime_ns=0, inode=0, device=0)
    else:
        if len(raw) > 16384:
            raise ValueError("Invalid file request")
        data, metadata = _read_fd_file(request["source"], request["path"], request["offset"], request["length"], prefix="/host", max_bytes=request["max_bytes"])
    sys.stdout.buffer.write(json.dumps({"ok": True, "length": len(data), "stat": metadata}, separators=(",", ":")).encode() + b"\\n")
    sys.stdout.buffer.write(data)
except Exception:
    sys.stdout.buffer.write(b'{"ok":false}\\n')
'''
    )


def read_dataset_host_file(source: str, relative_path: str, offset: int = 0,
                           length: int | None = None, *, configured_roots=None,
                           image: str | None = None, docker_host: str | None = None,
                           docker_client=None, max_bytes: int = MAX_READ_BYTES, _scope_paths=None) -> tuple[bytes, dict]:
    """Read a registered host source with current allowlist enforcement.

    The caller must first authorize the dataset and match the exact inventory
    entry. We independently revalidate paths and bound every Docker output byte.
    """
    client = docker_client
    owns_client = client is None
    container = None
    attached = None
    name = "ai-dataseek-dataset-preview-" + uuid.uuid4().hex
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS

    def check():
        if time.monotonic() > deadline:
            raise DatasetPreviewReadError(_ERROR_MESSAGE)

    try:
        settings = get_settings()
        roots = _default_local_allowlist(settings) if configured_roots is None else configured_roots
        mapped_source, candidates = docker_host_source_and_candidates(
            source, roots, getattr(settings, "dataset_docker_host_root", ""),
        )
        path = PurePosixPath(relative_path)
        if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_READ_BYTES
                or not candidates or not isinstance(relative_path, str) or not relative_path
                or path.is_absolute() or str(path) != relative_path or ".." in path.parts
                or "\\" in relative_path or len(relative_path.encode("utf-8")) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in relative_path)
                or type(offset) is not int or not 0 <= offset <= 2**63 - 1
                or (length is not None and (type(length) is not int or not 0 <= length <= max_bytes))):
            raise DatasetPreviewReadError(_ERROR_MESSAGE)
        if _scope_paths is not None and (not isinstance(_scope_paths, list) or not 2 <= len(_scope_paths) <= 2048
                or any(not isinstance(p, str) for p in _scope_paths) or len(set(_scope_paths)) != len(_scope_paths)
                or relative_path != _scope_paths[0] or offset != 0 or length is not None or max_bytes != 1024**2):
            raise DatasetPreviewReadError(_ERROR_MESSAGE)
        request = json.dumps(dict(source=mapped_source, roots=candidates, path=relative_path,
                                  offset=offset, length=length, max_bytes=max_bytes,
                                  **({"scope_paths": _scope_paths} if _scope_paths is not None else {})), ensure_ascii=True).encode() + b"\n"
        if len(request) > (262144 if _scope_paths is not None else 16384):
            raise DatasetPreviewReadError(_ERROR_MESSAGE)
        helper_image = image or settings.sandbox_image
        if not helper_image:
            raise DatasetPreviewReadError(_ERROR_MESSAGE)
        if client is None:
            client = docker.DockerClient(base_url=docker_host, timeout=5) if docker_host else docker.from_env(timeout=5)
        check()
        container = client.containers.create(
            image=helper_image, name=name, entrypoint=["python3"], command=["-c", _reader_program()],
            user="0:0", stdin_open=True, tty=False, network_mode="none", read_only=True,
            mounts=[docker.types.Mount(target="/host", source="/", type="bind", read_only=True)],
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="384m", memswap_limit="384m", nano_cpus=1_000_000_000, pids_limit=16,
            environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            labels={"ai-dataseek.component": "dataset-preview-read"},
            log_config=docker.types.LogConfig(type="none"),
        )
        check()
        attached = container.attach_socket(params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
        wire = getattr(attached, "_sock", attached)
        wire.settimeout(0.5)
        container.start()
        sent = 0
        while sent < len(request):
            check()
            try:
                count = wire.send(request[sent:])
            except socket.timeout:
                continue
            if not count:
                raise DatasetPreviewReadError(_ERROR_MESSAGE)
            sent += count
        wire.shutdown(socket.SHUT_WR)

        def receive(count, allow_eof=False):
            result = bytearray()
            while len(result) < count:
                check()
                try:
                    chunk = wire.recv(min(count - len(result), 65536))
                except socket.timeout:
                    continue
                if not chunk:
                    if allow_eof and not result:
                        return b""
                    raise DatasetPreviewReadError(_ERROR_MESSAGE)
                result.extend(chunk)
            return bytes(result)

        max_payload = max_bytes if length is None else length
        output = bytearray()
        captured = 0
        while True:
            frame = receive(8, allow_eof=True)
            if not frame:
                break
            count = int.from_bytes(frame[4:8], "big")
            captured += count
            if frame[0] not in {1, 2} or frame[1:4] != b"\0\0\0" or captured > max_payload + MAX_HEADER_BYTES + 16384:
                raise DatasetPreviewReadError(_ERROR_MESSAGE)
            # Even a malformed single frame is drained in small pieces, not
            # duplicated in another unbounded intermediate buffer.
            while count:
                piece = receive(min(count, 65536))
                count -= len(piece)
                if frame[0] == 1:
                    output.extend(piece)
                    if len(output) > max_payload + MAX_HEADER_BYTES:
                        raise DatasetPreviewReadError(_ERROR_MESSAGE)
        check()
        if container.wait(timeout=3).get("StatusCode") != 0:
            raise DatasetPreviewReadError(_ERROR_MESSAGE)
        separator = output.find(b"\n", 0, MAX_HEADER_BYTES)
        if separator < 0:
            raise DatasetPreviewReadError(_ERROR_MESSAGE)
        header = json.loads(output[:separator])
        metadata = header.get("stat") if isinstance(header, dict) else None
        if (not isinstance(header, dict) or header.get("ok") is not True
                or type(header.get("length")) is not int
                or header["length"] != len(output) - separator - 1
                or not isinstance(metadata, dict)
                or set(metadata) != {"size", "mtime_ns", "ctime_ns", "inode", "device"}
                or any(type(value) is not int for value in metadata.values())
                or any(metadata[key] < 0 for key in ("size", "inode", "device"))
                or metadata["size"] < offset
                or header["length"] != (metadata["size"] - offset if length is None else min(length, metadata["size"] - offset))):
            raise DatasetPreviewReadError(_ERROR_MESSAGE)
        return bytes(output[separator + 1:]), metadata
    except Exception:
        raise DatasetPreviewReadError(_ERROR_MESSAGE) from None
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
                    container.remove(force=True)
            except Exception:
                # Do not expose daemon paths if cleanup itself fails.
                pass
            finally:
                if owns_client:
                    try:
                        client.close()
                    except Exception:
                        pass
