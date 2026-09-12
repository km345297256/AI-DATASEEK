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


class MissingArtifact(FileNotFoundError):
    """Absence proven beneath a stable, no-follow output ancestry."""


class _MissingOutputEntry(FileNotFoundError):
    def __init__(self, proof):
        super().__init__("output_entry_missing")
        self.proof = proof


def _open_output_once(path: str, root: Path):
    relative = Path(path).relative_to(root)
    if (not root.is_absolute() or root == Path("/") or ".." in root.parts
            or not relative.parts or any(part in {".", ".."} for part in relative.parts)
            or str(Path(path)) != path or "\\" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise ValueError("outside_output")
    # Traverse with directory handles: symlink parents, last-component links,
    # FIFOs/devices and directory-swap races must not turn hashing into a read
    # of arbitrary dataset/private files or block the worker on a pipe.
    # Root parents need the same protection as descendants. Opening the root
    # by its absolute path would follow a symlink in an earlier component.
    parts = root.parts[1:] + relative.parts
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    ancestry = []
    try:
        for index, part in enumerate(parts):
            # Keep private directory identities, not resolved path strings.
            parent = os.stat(".", dir_fd=directory, follow_symlinks=False)
            ancestry.append((_identity(parent), parent.st_mode, parent.st_uid, parent.st_gid))
            flags = os.O_RDONLY | os.O_NOFOLLOW
            flags |= os.O_DIRECTORY if index < len(parts) - 1 else os.O_NONBLOCK
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                # A missing runtime ancestor is not a missing deliverable.
                # The output root itself may not have been created yet, but
                # only its fully verified existing parent can prove that.
                if index >= len(root.parts) - 2:
                    raise _MissingOutputEntry((index, tuple(ancestry))) from None
                raise
            if index == len(parts) - 1:
                try:
                    return os.fdopen(child, "rb")
                except BaseException:
                    try:
                        os.close(child)
                    except OSError:
                        pass  # fdopen may already have closed a rejected FD.
                    raise
            os.close(directory)
            directory = child
    finally:
        os.close(directory)


def _open_output(path: str, root: Path):
    try:
        return _open_output_once(path, root)
    except _MissingOutputEntry as first:
        # Re-traverse from / so a detached directory FD or a replacement root
        # cannot turn an environment race into permission to create outputs.
        try:
            current = _open_output_once(path, root)
        except _MissingOutputEntry as second:
            if first.proof == second.proof:
                raise MissingArtifact("missing_artifact") from None
            raise FileNotFoundError("output_ancestry_changed") from None
        else:
            current.close()
            raise FileNotFoundError("output_entry_changed") from None


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
