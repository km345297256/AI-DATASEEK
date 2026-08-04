from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Sequence

import docker

from app.core.config import get_settings


DEFAULT_MAX_DIRECTORY_FILES = 10_000
DEFAULT_MAX_DIRECTORY_OUTPUT_BYTES = 4 * 1024 * 1024
_MIN_DIRECTORY_OUTPUT_BYTES = 512


class DatasetDirectoryInspectionError(RuntimeError):
    """A safe, actionable failure returned by the host directory inspector."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DatasetDirectoryFile:
    relative_path: str
    size: int


@dataclass(frozen=True, slots=True)
class DatasetDirectoryInventory:
    canonical_source_directory: str
    files: tuple[DatasetDirectoryFile, ...]

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)


def _configured_root_values(configured_roots: str | Sequence[str]) -> list[str]:
    if isinstance(configured_roots, str):
        values: Sequence[str] = configured_roots.split(",")
    else:
        values = configured_roots
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def host_path_candidates(
    source: str,
    configured_roots: str | Sequence[str],
) -> list[str]:
    """Return allowlist roots that lexically contain an absolute host path.

    Canonical containment is deliberately checked later from a Docker helper
    that sees the Docker host filesystem. The backend may itself be running in
    a container and must not inspect its own filesystem for this decision.
    """
    if not isinstance(source, str) or not source or any(ord(character) < 32 for character in source):
        return []
    source_path = PurePosixPath(source)
    if not source_path.is_absolute() or ".." in source_path.parts:
        return []

    roots: list[str] = []
    for value in _configured_root_values(configured_roots):
        if any(ord(character) < 32 for character in value):
            continue
        root = PurePosixPath(value)
        if root.is_absolute() and ".." not in root.parts and (
            source_path == root or root in source_path.parents
        ):
            roots.append(str(root))
    return roots


def _host_root_mount():
    return docker.types.Mount(
        target="/host",
        source="/",
        type="bind",
        read_only=True,
    )


def canonical_host_source(
    docker_client: Any,
    *,
    image: str,
    source: str,
    candidate_roots: list[str],
) -> str:
    """Resolve and validate a file or directory on the Docker host."""
    if not candidate_roots:
        raise RuntimeError("Dataset path is outside DATASET_HOST_PATH_ALLOWLIST")

    # Chrooting into the read-only host bind makes absolute symlinks resolve
    # with the Docker host's semantics rather than the backend container's.
    validator_script = r'''
set -eu
for candidate do
  chroot /host /bin/sh -c '
    resolved="$(readlink -e -- "$1")" || exit 40
    if [ -f "$resolved" ]; then
      kind=file
    elif [ -d "$resolved" ]; then
      kind=directory
    else
      exit 41
    fi
    printf "%s\t%s\n" "$resolved" "$kind"
  ' dataset-path-validator "$candidate"
done
'''
    output = docker_client.containers.run(
        image=image,
        entrypoint="/bin/sh",
        command=["-c", validator_script, "dataset-path-validator", source, *candidate_roots],
        mounts=[_host_root_mount()],
        network_disabled=True,
        read_only=True,
        remove=True,
        user="0:0",
    )
    lines = output.decode("utf-8", errors="strict").splitlines()
    if len(lines) != len(candidate_roots) + 1:
        raise RuntimeError("Dataset path validation returned an invalid result")

    def resolved_path(line: str) -> PurePosixPath:
        value, separator, kind = line.partition("\t")
        if not separator or kind not in {"file", "directory"}:
            raise RuntimeError("Dataset path validation returned an invalid result")
        return PurePosixPath(value)

    canonical_source_path = resolved_path(lines[0])
    canonical_roots = [resolved_path(line) for line in lines[1:]]
    if not any(
        canonical_source_path == root or root in canonical_source_path.parents
        for root in canonical_roots
    ):
        raise RuntimeError("Dataset path is outside DATASET_HOST_PATH_ALLOWLIST")
    return str(canonical_source_path)


_DIRECTORY_INVENTORY_HELPER = r"""
import json
import os
import pathlib
import stat
import subprocess
import sys


class InventoryFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def fail(code, message):
    raise InventoryFailure(code, message)


def safe_absolute_path(value):
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        return False
    path = pathlib.PurePosixPath(value)
    return path.is_absolute() and ".." not in path.parts


def safe_component(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    if "/" in value or "\\" in value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def safe_relative_path(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8", errors="strict")) > 4096:
            return False
    except UnicodeEncodeError:
        return False
    path = pathlib.PurePosixPath(value)
    return not path.is_absolute() and str(path) == value and all(
        safe_component(part) for part in path.parts
    )


def resolve_host_directory(value):
    script = r'''
resolved="$(readlink -e -- "$1")" || exit 40
[ -d "$resolved" ] || exit 41
printf "%s" "$resolved"
'''
    process = subprocess.run(
        ["chroot", "/host", "/bin/sh", "-c", script, "dataset-directory-inspector", value],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if process.returncode != 0:
        return None, process.returncode
    try:
        resolved = process.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, 42
    if not safe_absolute_path(resolved):
        return None, 42
    return resolved, 0


def is_within(path_value, root_value):
    path = pathlib.PurePosixPath(path_value)
    root = pathlib.PurePosixPath(root_value)
    return path == root or root in path.parents


def open_host_directory(canonical_source, directory_flags):
    # Open each component with O_NOFOLLOW to close ancestor symlink races.
    directory_fd = os.open("/host", directory_flags)
    try:
        for component in pathlib.PurePosixPath(canonical_source).parts[1:]:
            child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd


def inspect_directory(
    source,
    candidate_roots,
    max_files,
    max_output_bytes,
    max_directories,
    max_entries,
):
    canonical_source, source_status = resolve_host_directory(source)
    if source_status == 40:
        fail("source_not_found", "Dataset source directory does not exist on the Docker host")
    if source_status == 41:
        fail("source_not_directory", "Dataset source path is not a directory on the Docker host")
    if source_status:
        fail("invalid_canonical_path", "Dataset source directory could not be safely resolved")

    canonical_roots = []
    for candidate_root in candidate_roots:
        canonical_root, root_status = resolve_host_directory(candidate_root)
        if root_status == 0 and canonical_root not in canonical_roots:
            canonical_roots.append(canonical_root)
    if not canonical_roots or not any(
        is_within(canonical_source, canonical_root) for canonical_root in canonical_roots
    ):
        fail(
            "source_outside_allowlist",
            "Dataset source directory is outside DATASET_LOCAL_PATH_ALLOWLIST",
        )

    result = {
        "ok": True,
        "canonical_source_directory": canonical_source,
        "canonical_allowed_roots": canonical_roots,
        "files": [],
    }
    fixed_output_size = len(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if fixed_output_size > max_output_bytes:
        fail("output_too_large", "Dataset directory inventory exceeds its output size limit")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        root_fd = open_host_directory(canonical_source, directory_flags)
    except FileNotFoundError:
        fail("source_not_found", "Dataset source directory disappeared during inspection")
    except NotADirectoryError:
        fail("source_not_directory", "Dataset source path is no longer a directory")
    except PermissionError:
        fail("source_unreadable", "Dataset source directory cannot be read")
    except OSError:
        fail("inspection_failed", "Dataset source directory could not be opened safely")

    visited_directories = set()
    encoded_file_size = 0
    scanned_entries = 0

    def walk(directory_fd, relative_parts, depth):
        nonlocal encoded_file_size, scanned_entries
        if depth > 256:
            fail("directory_too_deep", "Dataset directory nesting exceeds the safe depth limit")

        directory_stat = os.fstat(directory_fd)
        directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
        if directory_identity in visited_directories:
            fail("directory_cycle", "Dataset directory contains a recursive mount cycle")
        visited_directories.add(directory_identity)
        if len(visited_directories) > max_directories:
            fail(
                "too_many_directories",
                "Dataset directory contains more directories than allowed",
            )

        def visit(entry):
            nonlocal encoded_file_size, scanned_entries
            scanned_entries += 1
            if scanned_entries > max_entries:
                fail(
                    "too_many_entries",
                    "Dataset directory contains more entries than allowed",
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            except PermissionError:
                fail("source_unreadable", "A dataset entry cannot be inspected")
            except OSError:
                fail("inspection_failed", "A dataset entry could not be inspected")

            if stat.S_ISLNK(entry_stat.st_mode):
                return
            if not stat.S_ISREG(entry_stat.st_mode) and not stat.S_ISDIR(entry_stat.st_mode):
                return
            if not safe_component(entry.name):
                fail("unsafe_relative_path", "Dataset contains a filename that cannot be mounted safely")

            child_parts = (*relative_parts, entry.name)
            if stat.S_ISREG(entry_stat.st_mode):
                relative_path = "/".join(child_parts)
                if not safe_relative_path(relative_path):
                    fail(
                        "unsafe_relative_path",
                        "Dataset contains a path that cannot be mounted safely",
                    )
                item = {"relative_path": relative_path, "size": entry_stat.st_size}
                item_size = len(
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                projected_count = len(result["files"]) + 1
                if projected_count > max_files:
                    fail("too_many_files", "Dataset directory contains more files than allowed")
                projected_size = (
                    fixed_output_size
                    + encoded_file_size
                    + item_size
                    + max(0, projected_count - 1)
                )
                if projected_size > max_output_bytes:
                    fail(
                        "output_too_large",
                        "Dataset directory inventory exceeds its output size limit",
                    )
                result["files"].append(item)
                encoded_file_size += item_size
                return

            try:
                child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return
            except NotADirectoryError:
                return
            except PermissionError:
                fail("source_unreadable", "A dataset subdirectory cannot be read")
            except OSError:
                fail("inspection_failed", "A dataset subdirectory could not be opened safely")
            try:
                walk(child_fd, child_parts, depth + 1)
            finally:
                os.close(child_fd)

        try:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    visit(entry)
        except PermissionError:
            fail("source_unreadable", "A dataset subdirectory cannot be read")
        except OSError:
            fail("inspection_failed", "A dataset subdirectory could not be enumerated")

    try:
        walk(root_fd, (), 0)
    finally:
        os.close(root_fd)
    return result


def main():
    max_output_bytes = 512
    try:
        source = sys.argv[1]
        candidate_roots = json.loads(sys.argv[2])
        max_files = int(sys.argv[3])
        max_output_bytes = int(sys.argv[4])
        max_directories = int(sys.argv[5])
        max_entries = int(sys.argv[6])
        if not isinstance(candidate_roots, list) or not all(
            isinstance(value, str) for value in candidate_roots
        ):
            fail("invalid_request", "Dataset directory allowlist is invalid")
        payload = inspect_directory(
            source,
            candidate_roots,
            max_files,
            max_output_bytes,
            max_directories,
            max_entries,
        )
    except InventoryFailure as error:
        payload = {"ok": False, "code": error.code, "message": error.message}
    except Exception:
        payload = {
            "ok": False,
            "code": "inspection_failed",
            "message": "Dataset source directory inspection failed",
        }

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_output_bytes:
        encoded = json.dumps(
            {
                "ok": False,
                "code": "output_too_large",
                "message": "Dataset directory inventory exceeds its output size limit",
            },
            separators=(",", ":"),
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)


main()
"""


def _default_local_allowlist(settings: Any) -> str | Sequence[str]:
    configured = getattr(settings, "dataset_local_path_allowlist", None)
    if configured is not None:
        return configured

    # The current deployment still names this setting
    # DATASET_HOST_PATH_ALLOWLIST. Honour the new local-specific variable when
    # present while retaining compatibility until configuration is migrated.
    environment_value = os.environ.get("DATASET_LOCAL_PATH_ALLOWLIST")
    if environment_value is not None:
        return environment_value
    return getattr(settings, "dataset_host_path_allowlist", "")


def _safe_absolute_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(ord(character) < 32 for character in value):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    path = PurePosixPath(value)
    return path.is_absolute() and ".." not in path.parts


def docker_host_path(path: str, docker_host_root: str) -> str:
    """Map a logical host path into the filesystem namespace visible to Docker."""
    docker_host_root = docker_host_root.strip()
    if not docker_host_root:
        return path
    if not _safe_absolute_path(docker_host_root):
        raise DatasetDirectoryInspectionError(
            "invalid_docker_host_root",
            "DATASET_DOCKER_HOST_ROOT must be an absolute path without '..' segments",
        )
    root = PurePosixPath(docker_host_root)
    logical_path = PurePosixPath(path)
    if logical_path == root or root in logical_path.parents:
        return str(logical_path)
    return str(root.joinpath(*logical_path.parts[1:]))


def docker_host_source_and_candidates(
    source: str,
    configured_roots: str | Sequence[str],
    docker_host_root: str,
) -> tuple[str, list[str]]:
    """Return the Docker-visible source and matching Docker-visible roots.

    Accepting an already mapped source is intentional: directory inspection
    stores its canonical Docker-host path, and sandbox creation validates that
    same path again before mounting it.
    """
    if not _safe_absolute_path(source):
        return source, []
    mapped_source = docker_host_path(source, docker_host_root)
    mapped_roots = [
        docker_host_path(root, docker_host_root)
        for root in _configured_root_values(configured_roots)
        if _safe_absolute_path(root)
    ]
    return mapped_source, host_path_candidates(mapped_source, mapped_roots)


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8", errors="strict")) > 4096:
            return False
    except UnicodeEncodeError:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or not path.parts:
        return False
    for part in path.parts:
        if part in {"", ".", ".."} or "\\" in part:
            return False
        if any(ord(character) < 32 or ord(character) == 127 for character in part):
            return False
    return True


def _directory_traversal_limits(max_files: int) -> tuple[int, int]:
    """Bound entries that do not contribute to the regular-file limit."""
    return max(1_024, 2 * max_files), max(10_000, 10 * max_files)


def _parse_directory_inventory(
    output: bytes | str,
    *,
    max_files: int,
    max_output_bytes: int,
) -> DatasetDirectoryInventory:
    if isinstance(output, str):
        encoded_output = output.encode("utf-8", errors="strict")
    elif isinstance(output, bytes):
        encoded_output = output
    else:
        raise DatasetDirectoryInspectionError(
            "invalid_helper_response",
            "Dataset directory helper returned a non-text response",
        )
    if len(encoded_output) > max_output_bytes:
        raise DatasetDirectoryInspectionError(
            "output_too_large",
            "Dataset directory inventory exceeds its output size limit",
        )

    try:
        payload = json.loads(encoded_output.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatasetDirectoryInspectionError(
            "invalid_helper_response",
            "Dataset directory helper returned invalid JSON",
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise DatasetDirectoryInspectionError(
            "invalid_helper_response",
            "Dataset directory helper returned an invalid result",
        )
    if not payload["ok"]:
        code = payload.get("code")
        message = payload.get("message")
        if not isinstance(code, str) or not code or not isinstance(message, str) or not message:
            raise DatasetDirectoryInspectionError(
                "invalid_helper_response",
                "Dataset directory helper returned an invalid error",
            )
        raise DatasetDirectoryInspectionError(code, message)

    canonical_source = payload.get("canonical_source_directory")
    canonical_roots = payload.get("canonical_allowed_roots")
    raw_files = payload.get("files")
    if (
        not _safe_absolute_path(canonical_source)
        or not isinstance(canonical_roots, list)
        or not canonical_roots
        or not all(_safe_absolute_path(root) for root in canonical_roots)
        or not isinstance(raw_files, list)
    ):
        raise DatasetDirectoryInspectionError(
            "invalid_helper_response",
            "Dataset directory helper returned an invalid manifest",
        )

    canonical_source_path = PurePosixPath(canonical_source)
    if not any(
        canonical_source_path == PurePosixPath(root)
        or PurePosixPath(root) in canonical_source_path.parents
        for root in canonical_roots
    ):
        raise DatasetDirectoryInspectionError(
            "source_outside_allowlist",
            "Dataset source directory is outside DATASET_LOCAL_PATH_ALLOWLIST",
        )
    if len(raw_files) > max_files:
        raise DatasetDirectoryInspectionError(
            "too_many_files",
            "Dataset directory contains more files than allowed",
        )

    files: list[DatasetDirectoryFile] = []
    seen_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise DatasetDirectoryInspectionError(
                "invalid_helper_response",
                "Dataset directory helper returned an invalid file entry",
            )
        relative_path = raw_file.get("relative_path")
        size = raw_file.get("size")
        if (
            not _safe_relative_path(relative_path)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or relative_path in seen_paths
        ):
            raise DatasetDirectoryInspectionError(
                "invalid_helper_response",
                "Dataset directory helper returned an unsafe file entry",
            )
        seen_paths.add(relative_path)
        files.append(DatasetDirectoryFile(relative_path=relative_path, size=size))

    files.sort(key=lambda item: item.relative_path)
    return DatasetDirectoryInventory(
        canonical_source_directory=canonical_source,
        files=tuple(files),
    )


def inspect_local_dataset_directory(
    source_directory: str,
    *,
    docker_client: Any | None = None,
    docker_host: str | None = None,
    helper_image: str | None = None,
    configured_roots: str | Sequence[str] | None = None,
    max_files: int = DEFAULT_MAX_DIRECTORY_FILES,
    max_output_bytes: int = DEFAULT_MAX_DIRECTORY_OUTPUT_BYTES,
) -> DatasetDirectoryInventory:
    """Inspect a Docker-host directory without exposing or writing host data.

    The returned paths are relative, slash-separated paths safe to place below
    a sandbox dataset mount. Symbolic links and non-regular files are omitted.
    """
    if not _safe_absolute_path(source_directory):
        raise DatasetDirectoryInspectionError(
            "invalid_source_directory",
            "Dataset source directory must be an absolute path without '..' segments",
        )
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        raise ValueError("max_files must be a positive integer")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes < _MIN_DIRECTORY_OUTPUT_BYTES
    ):
        raise ValueError(
            f"max_output_bytes must be an integer of at least {_MIN_DIRECTORY_OUTPUT_BYTES}"
        )

    settings = get_settings()
    allowed_roots = configured_roots
    if allowed_roots is None:
        allowed_roots = _default_local_allowlist(settings)
    docker_host_root = getattr(settings, "dataset_docker_host_root", "")
    docker_source_directory, docker_candidate_roots = docker_host_source_and_candidates(
        source_directory,
        allowed_roots,
        docker_host_root,
    )
    if not docker_candidate_roots:
        raise DatasetDirectoryInspectionError(
            "source_outside_allowlist",
            "Dataset source directory is outside DATASET_LOCAL_PATH_ALLOWLIST",
        )

    image = helper_image or settings.sandbox_image
    if not image:
        raise DatasetDirectoryInspectionError(
            "helper_image_not_configured",
            "SANDBOX_IMAGE is required to inspect Docker-host dataset directories",
        )

    owns_client = docker_client is None
    try:
        max_directories, max_entries = _directory_traversal_limits(max_files)
        if docker_client is None:
            timeout = settings.sandbox_docker_create_timeout_seconds
            docker_client = (
                docker.DockerClient(base_url=docker_host, timeout=timeout)
                if docker_host
                else docker.from_env(timeout=timeout)
            )
        output = docker_client.containers.run(
            image=image,
            entrypoint="python3",
            command=[
                "-c",
                _DIRECTORY_INVENTORY_HELPER,
                docker_source_directory,
                json.dumps(docker_candidate_roots, separators=(",", ":")),
                str(max_files),
                str(max_output_bytes),
                str(max_directories),
                str(max_entries),
            ],
            environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            mounts=[_host_root_mount()],
            network_disabled=True,
            read_only=True,
            remove=True,
            user="0:0",
        )
    except DatasetDirectoryInspectionError:
        raise
    except Exception as error:
        raise DatasetDirectoryInspectionError(
            "helper_failed",
            "Docker-host dataset directory helper failed",
        ) from error
    finally:
        if owns_client and docker_client is not None:
            try:
                docker_client.close()
            except Exception:
                pass

    return _parse_directory_inventory(
        output,
        max_files=max_files,
        max_output_bytes=max_output_bytes,
    )


# Private aliases retain compatibility for the original docker_sandbox helper
# names while allowing that module to import the shared implementation.
_host_path_candidates = host_path_candidates
_canonical_host_source = canonical_host_source
