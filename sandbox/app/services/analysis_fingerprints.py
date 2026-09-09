"""Read-only, bounded source/output snapshots for verified analysis continuation."""
from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path

from app.services.artifact_manifest import _identity, _open_output

ANALYSIS_ROOTS = (Path("/home/ubuntu/datasets"), Path("/home/ubuntu/output"))
MAX_PATHS = 64
MAX_BATCH_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
BATCH_TIMEOUT_SECONDS = 30


class _SnapshotFailure(Exception):
    def __init__(self, code: str):
        self.code = code


def _check(cancelled: threading.Event, deadline: float):
    if cancelled.is_set() or time.monotonic() >= deadline:
        raise _SnapshotFailure("snapshot_deadline")


def _fingerprint(path: str, root: Path, cancelled: threading.Event,
                 deadline: float, byte_budget: list[int]) -> dict:
    _check(cancelled, deadline)
    with _open_output(path, root) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise _SnapshotFailure("unavailable_or_unsafe_path")
        if before.st_size > byte_budget[0]:
            raise _SnapshotFailure("snapshot_size_limit")
        remaining = before.st_size + 1
        digest = hashlib.sha256()
        size = 0
        while remaining:
            _check(cancelled, deadline)
            chunk = stream.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                break
            byte_budget[0] -= len(chunk)
            if byte_budget[0] < 0:
                raise _SnapshotFailure("snapshot_size_limit")
            digest.update(chunk)
            size += len(chunk)
            remaining -= len(chunk)
        after = os.fstat(stream.fileno())
        with _open_output(path, root) as current_stream:
            current = os.fstat(current_stream.fileno())
        if _identity(before) != _identity(after) or _identity(after) != _identity(current) or size != before.st_size:
            raise _SnapshotFailure("changed_during_read")
        return {"path": path, "size": size, "sha256": digest.hexdigest()}


def fingerprint_analysis_files(paths: list[str], *, roots: tuple[Path, ...] = ANALYSIS_ROOTS,
                                cancelled: threading.Event | None = None,
                                deadline: float | None = None) -> dict:
    """Hash only registered-mount/output roots; no caller-selected root or body."""
    if not paths or len(paths) > MAX_PATHS:
        raise ValueError("invalid_snapshot_path_count")
    cancelled = cancelled or threading.Event()
    deadline = time.monotonic() + BATCH_TIMEOUT_SECONDS if deadline is None else deadline
    byte_budget = [MAX_BATCH_BYTES]
    files, errors = [], []
    for path in dict.fromkeys(paths):
        try:
            _check(cancelled, deadline)
            candidate = Path(path)
            root = next((root for root in roots if candidate != root and candidate.is_relative_to(root)), None)
            if root is None or ".." in candidate.parts:
                raise _SnapshotFailure("unavailable_or_unsafe_path")
            files.append(_fingerprint(path, root, cancelled, deadline, byte_budget))
        except _SnapshotFailure as exc:
            errors.append({"path": path, "code": exc.code})
        except (OSError, ValueError):
            errors.append({"path": path, "code": "unavailable_or_unsafe_path"})
    return {"version": 1, "files": files, "errors": errors}
