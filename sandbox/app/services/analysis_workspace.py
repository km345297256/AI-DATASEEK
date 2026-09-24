"""Prepare only the platform-owned analysis output directory before launch.

The model's script, argv and working directory do not select what gets created.
Input directories and dataset mounts remain untouched. In particular this is
not a generic ``mkdir -p`` recovery for arbitrary FileNotFoundError failures.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import uuid

from app.core.exceptions import BadRequestException


WORKSPACE_ROOT = Path("/home/ubuntu")


def prepare_analysis_workspace(*, workspace_root: Path | None = None) -> None:
    """Create/check ``output`` below an existing trusted workspace, no links.

    ``workspace_root`` is dependency injection for isolated callers/tests, never
    an execution-tool argument. Traverse existing parents using no-follow file
    descriptors, create exactly one child, and verify actual write capability
    without overwriting an existing result. Fail before launching any process.
    """
    root = WORKSPACE_ROOT if workspace_root is None else workspace_root
    descriptors: list[int] = []
    try:
        if (not isinstance(root, Path) or not root.is_absolute() or root == Path("/")
                or ".." in root.parts or "\x00" in str(root)):
            raise ValueError("Invalid trusted workspace")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open("/", flags)
        descriptors.append(current)
        for part in root.parts[1:]:
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        try:
            os.mkdir("output", mode=0o755, dir_fd=current)
        except FileExistsError:
            pass
        output = os.open("output", flags, dir_fd=current)
        descriptors.append(output)
        observed = os.fstat(output)
        entry = os.stat("output", dir_fd=current, follow_symlinks=False)
        if (not stat.S_ISDIR(entry.st_mode)
                or (observed.st_dev, observed.st_ino) != (entry.st_dev, entry.st_ino)):
            raise OSError("Output directory changed during preparation")
        # os.access alone can miss a read-only mount or quota/storage failure.
        # Exclusive creation never truncates a user's file, including a link.
        name = ".dataseek-write-check-" + uuid.uuid4().hex
        probe = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600, dir_fd=output)
        identity = None
        try:
            identity = os.fstat(probe)
            if os.write(probe, b"ready\n") != 6:
                raise OSError("Output write check did not complete")
        finally:
            os.close(probe)
            try:
                candidate = os.stat(name, dir_fd=output, follow_symlinks=False)
                if identity is not None and (identity.st_dev, identity.st_ino) == (candidate.st_dev, candidate.st_ino):
                    os.unlink(name, dir_fd=output)
            except FileNotFoundError:
                pass
    except (OSError, ValueError):
        # Paths/errors may contain private host state. Keep a stable public
        # prerequisite error and preserve the no-process-started receipt.
        raise BadRequestException("Analysis output workspace is not safely writable") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
