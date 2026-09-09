"""Read-only output fingerprints. This private sandbox API returns no bodies."""
from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path

ARTIFACT_ROOT = Path("/home/ubuntu/output")
HASH_CHUNK_BYTES = 1024 * 1024
MAX_FINGERPRINT_PATHS = 256


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_output(path: str, root: Path):
    relative = Path(path).relative_to(root)
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("outside_output")
    # Traverse with directory handles: symlink parents, last-component links,
    # FIFOs/devices and directory-swap races must not turn hashing into a read
    # of arbitrary dataset/private files or block the worker on a pipe.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
        )
        return os.fdopen(descriptor, "rb")
    finally:
        os.close(directory)


def _fingerprint(path: str, root: Path, cancelled: threading.Event, deadline: float) -> dict:
    with _open_output(path, root) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not_regular")
        digest = hashlib.sha256()
        size = 0
        # A writer continuously appending must not keep us chasing EOF. The
        # extra byte detects growth without reading an unbounded new tail.
        remaining = before.st_size + 1
        while remaining:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise ValueError("cancelled_or_deadline")
            chunk = stream.read(min(HASH_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            remaining -= len(chunk)
        after = os.fstat(stream.fileno())
        # Re-traverse from the root, not the original directory FD: a parent
        # directory can have been replaced while its old FD remains valid.
        with _open_output(path, root) as current_stream:
            current = os.fstat(current_stream.fileno())
        if _identity(before) != _identity(after) or _identity(after) != _identity(current) or size != before.st_size:
            raise ValueError("changed_during_read")
        return {"path": path, "size": size, "sha256": digest.hexdigest()}


def fingerprint_artifacts(paths: list[str], *, root: Path = ARTIFACT_ROOT,
                          cancelled: threading.Event | None = None, deadline: float | None = None) -> dict:
    if len(paths) > MAX_FINGERPRINT_PATHS:
        raise ValueError("too_many_paths")
    files, errors = [], []
    cancelled = cancelled or threading.Event()
    deadline = time.monotonic() + 30 if deadline is None else deadline
    for path in dict.fromkeys(paths):
        try:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise ValueError("cancelled_or_deadline")
            files.append(_fingerprint(path, root, cancelled, deadline))
        except (OSError, ValueError):
            # Internal callers already know these container paths. Never copy
            # OS exception text or resolved targets into the response/logs.
            errors.append({"path": path, "code": "unavailable_or_changed"})
    return {"version": 1, "files": files, "errors": errors}
