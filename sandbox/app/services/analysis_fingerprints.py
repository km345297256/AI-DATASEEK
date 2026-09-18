"""Read-only, bounded source/output snapshots for verified analysis continuation."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
from pathlib import Path

from app.services.artifact_manifest import _identity, _open_output

ANALYSIS_ROOTS = (Path("/home/ubuntu/datasets"), Path("/home/ubuntu/output"))
UPLOAD_ROOT = Path("/home/ubuntu/inputs")
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


def _authorized_upload(path: str, approved_upload_paths: set[str], root: Path) -> bool:
    """Only a controller-authorized, canonical file in an identity namespace.

    This API is not a general hashing endpoint: upload access requires the exact
    path supplied by the backend after ownership/identity validation. Never add
    the entire upload directory to the normal snapshot roots.
    """
    if (path not in approved_upload_paths or not isinstance(path, str)
            or "\\" in path or any(ord(char) < 32 for char in path)):
        return False
    candidate = Path(path)
    if str(candidate) != path or ".." in candidate.parts or not candidate.is_relative_to(root):
        return False
    relative = candidate.relative_to(root)
    return len(relative.parts) == 2 and bool(re.fullmatch(r"[0-9a-f]{24}", relative.parts[0]))


def fingerprint_analysis_files(paths: list[str], *, roots: tuple[Path, ...] = ANALYSIS_ROOTS,
                                approved_upload_paths: list[str] | None = None,
                                upload_root: Path = UPLOAD_ROOT,
                                cancelled: threading.Event | None = None,
                                deadline: float | None = None) -> dict:
    """Hash mounted inputs/outputs or exact authorized uploads; return no body."""
    if not paths or len(paths) > MAX_PATHS:
        raise ValueError("invalid_snapshot_path_count")
    if (approved_upload_paths is not None
            and (not isinstance(approved_upload_paths, list)
                 or len(approved_upload_paths) > MAX_PATHS
                 or any(not isinstance(path, str) for path in approved_upload_paths))):
        raise ValueError("invalid_upload_authorization")
    approved_uploads = set(approved_upload_paths or [])
    cancelled = cancelled or threading.Event()
    deadline = time.monotonic() + BATCH_TIMEOUT_SECONDS if deadline is None else deadline
    byte_budget = [MAX_BATCH_BYTES]
    files, errors = [], []
    for path in dict.fromkeys(paths):
        try:
            _check(cancelled, deadline)
            candidate = Path(path)
            root = next((root for root in roots if candidate != root and candidate.is_relative_to(root)), None)
            if root is None and _authorized_upload(path, approved_uploads, upload_root):
                root = upload_root
            if root is None or ".." in candidate.parts:
                raise _SnapshotFailure("unavailable_or_unsafe_path")
            files.append(_fingerprint(path, root, cancelled, deadline, byte_budget))
        except _SnapshotFailure as exc:
            errors.append({"path": path, "code": exc.code})
        except (OSError, ValueError):
            errors.append({"path": path, "code": "unavailable_or_unsafe_path"})
    return {"version": 1, "files": files, "errors": errors}
