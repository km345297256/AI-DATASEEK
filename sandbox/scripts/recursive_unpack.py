#!/usr/bin/env python3
"""Safely unpack nested ZIP, RAR, and 7z datasets with bounded resources."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO, Iterable
import zipfile

import rarfile


CHUNK_SIZE = 1024 * 1024
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")


class UnpackError(RuntimeError):
    """Raised when an archive is unsafe, unsupported, corrupt, or over budget."""


@dataclass(frozen=True, slots=True)
class Limits:
    max_depth: int = 5
    max_archives: int = 100
    max_files: int = 2_000
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_single_file_bytes: int = 512 * 1024 * 1024
    timeout_seconds: int = 120


@dataclass(slots=True)
class ExtractedFile:
    path: Path
    size: int


@dataclass(slots=True)
class State:
    limits: Limits
    files: list[ExtractedFile] = field(default_factory=list)
    archives: list[dict] = field(default_factory=list)
    total_bytes: int = 0

    def validate_batch(self, sizes: Iterable[int]) -> None:
        sizes = list(sizes)
        if len(self.files) + len(sizes) > self.limits.max_files:
            raise UnpackError(
                f"file count exceeds limit ({self.limits.max_files})"
            )
        for size in sizes:
            if size < 0:
                raise UnpackError("archive contains a negative file size")
            if size > self.limits.max_single_file_bytes:
                raise UnpackError(
                    "single file exceeds limit "
                    f"({size} > {self.limits.max_single_file_bytes} bytes)"
                )
        if self.total_bytes + sum(sizes) > self.limits.max_total_bytes:
            raise UnpackError(
                "expanded data exceeds total limit "
                f"({self.limits.max_total_bytes} bytes)"
            )

    def commit(self, path: Path, size: int) -> None:
        self.files.append(ExtractedFile(path=path, size=size))
        self.total_bytes += size


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    relative_path: Path
    size: int
    is_directory: bool = False


def archive_kind(path: Path) -> str | None:
    """Detect supported archives by signature instead of trusting extensions."""
    try:
        with path.open("rb") as stream:
            signature = stream.read(8)
    except (OSError, ValueError):
        return None

    if signature.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if signature.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "rar"
    if signature.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    return None


def safe_relative_path(raw_name: str) -> Path:
    """Return a safe relative member path or reject path traversal."""
    if not raw_name or "\x00" in raw_name:
        raise UnpackError("archive contains an empty or NUL member path")
    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UnpackError(f"archive contains an absolute path: {raw_name!r}")

    pure_path = PurePosixPath(normalized)
    if pure_path.is_absolute() or any(part == ".." for part in pure_path.parts):
        raise UnpackError(f"archive contains path traversal: {raw_name!r}")
    parts = [part for part in pure_path.parts if part not in ("", ".")]
    if not parts:
        raise UnpackError(f"archive contains an invalid member path: {raw_name!r}")
    return Path(*parts)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _ensure_directory(root: Path, relative_path: Path) -> Path:
    current = root
    for part in relative_path.parts:
        current = current / part
        if _lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise UnpackError(f"unsafe directory collision: {relative_path}")
        else:
            current.mkdir()
    return current


def _target_for_file(root: Path, relative_path: Path) -> Path:
    _ensure_directory(root, relative_path.parent)
    target = root / relative_path
    if _lexists(target):
        raise UnpackError(f"duplicate or colliding archive member: {relative_path}")
    return target


def _copy_bounded(
    source: BinaryIO,
    target: Path,
    expected_size: int,
    state: State,
) -> None:
    written = 0
    try:
        with target.open("xb") as destination:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > state.limits.max_single_file_bytes:
                    raise UnpackError(f"single file exceeds limit while reading: {target.name}")
                if state.total_bytes + written > state.limits.max_total_bytes:
                    raise UnpackError("expanded data exceeds total limit while reading")
                destination.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    if written != expected_size:
        target.unlink(missing_ok=True)
        raise UnpackError(
            f"member size mismatch for {target.name}: expected {expected_size}, got {written}"
        )
    state.commit(target, written)


def _validate_members(members: list[Member], state: State) -> None:
    seen: set[Path] = set()
    file_sizes: list[int] = []
    for member in members:
        if member.relative_path in seen:
            raise UnpackError(f"duplicate archive member: {member.relative_path}")
        seen.add(member.relative_path)
        if not member.is_directory:
            file_sizes.append(member.size)
    state.validate_batch(file_sizes)


def extract_zip(archive_path: Path, target_root: Path, state: State) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        entries: list[tuple[zipfile.ZipInfo, Member]] = []
        for info in archive.infolist():
            relative_path = safe_relative_path(info.filename)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                raise UnpackError(f"symbolic links are not allowed: {info.filename!r}")
            if info.flag_bits & 0x1:
                raise UnpackError(f"encrypted archives are not supported: {info.filename!r}")
            entries.append(
                (
                    info,
                    Member(
                        name=info.filename,
                        relative_path=relative_path,
                        size=info.file_size,
                        is_directory=info.is_dir(),
                    ),
                )
            )

        _validate_members([member for _, member in entries], state)
        for info, member in entries:
            if member.is_directory:
                _ensure_directory(target_root, member.relative_path)
                continue
            target = _target_for_file(target_root, member.relative_path)
            with archive.open(info, "r") as source:
                _copy_bounded(source, target, member.size, state)


def extract_rar(archive_path: Path, target_root: Path, state: State) -> None:
    with rarfile.RarFile(archive_path) as archive:
        if archive.needs_password():
            raise UnpackError("encrypted archives are not supported")
        entries: list[tuple[rarfile.RarInfo, Member]] = []
        for info in archive.infolist():
            relative_path = safe_relative_path(info.filename)
            is_link = bool(
                getattr(info, "is_symlink", lambda: False)()
                or getattr(info, "is_hardlink", lambda: False)()
            )
            if is_link:
                raise UnpackError(f"links are not allowed: {info.filename!r}")
            entries.append(
                (
                    info,
                    Member(
                        name=info.filename,
                        relative_path=relative_path,
                        size=info.file_size,
                        is_directory=info.isdir(),
                    ),
                )
            )

        _validate_members([member for _, member in entries], state)
        for info, member in entries:
            if member.is_directory:
                _ensure_directory(target_root, member.relative_path)
                continue
            target = _target_for_file(target_root, member.relative_path)
            with archive.open(info, "r") as source:
                _copy_bounded(source, target, member.size, state)


def _parse_7z_listing(output: str) -> list[tuple[Member, dict[str, str]]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines() + [""]:
        line = raw_line.rstrip("\r")
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" = ")
        if separator:
            current[key] = value

    members: list[tuple[Member, dict[str, str]]] = []
    for record in records:
        raw_name = record.get("Path")
        if not raw_name:
            continue
        attributes = record.get("Attributes", "")
        attribute_parts = attributes.split()
        is_directory = record.get("Folder") == "+" or attributes.startswith("D")
        is_link = "Symbolic Link" in record or any(
            part.startswith("l") for part in attribute_parts
        )
        if is_link:
            raise UnpackError(f"links are not allowed: {raw_name!r}")
        if record.get("Encrypted") == "+":
            raise UnpackError(f"encrypted archives are not supported: {raw_name!r}")
        if record.get("Anti") == "+":
            raise UnpackError(f"anti-items are not allowed: {raw_name!r}")
        try:
            size = int(record.get("Size", "0"))
        except ValueError as exc:
            raise UnpackError(f"invalid member size for {raw_name!r}") from exc
        members.append(
            (
                Member(
                    name=raw_name,
                    relative_path=safe_relative_path(raw_name),
                    size=size,
                    is_directory=is_directory,
                ),
                record,
            )
        )
    if not members:
        raise UnpackError("7z archive contains no readable members")
    return members


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    return environment


def _file_size_limit(maximum: int):
    def apply_limit() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))

    return apply_limit


def extract_7z(archive_path: Path, target_root: Path, state: State) -> None:
    try:
        listing = subprocess.run(
            ["7z", "l", "-slt", "-ba", str(archive_path)],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=state.limits.timeout_seconds,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UnpackError(f"unable to list 7z archive: {exc}") from exc
    if listing.returncode != 0:
        raise UnpackError(f"7z listing failed: {listing.stderr[-2_000:]}")

    parsed_entries = _parse_7z_listing(listing.stdout)
    members = [member for member, _ in parsed_entries]
    _validate_members(members, state)

    with tempfile.TemporaryDirectory(prefix=".sevenzip-") as temporary_directory:
        staging = Path(temporary_directory)
        try:
            extraction = subprocess.run(
                [
                    "7z",
                    "x",
                    "-y",
                    "-bd",
                    "-bb0",
                    f"-o{staging}",
                    str(archive_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=state.limits.timeout_seconds,
                env=_subprocess_environment(),
                preexec_fn=_file_size_limit(state.limits.max_single_file_bytes),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UnpackError(f"unable to extract 7z archive: {exc}") from exc
        if extraction.returncode != 0:
            raise UnpackError(f"7z extraction failed: {extraction.stderr[-2_000:]}")

        expected_files = {
            member.relative_path for member in members if not member.is_directory
        }
        actual_files: set[Path] = set()
        for directory, directory_names, file_names in os.walk(staging, followlinks=False):
            directory_path = Path(directory)
            for name in directory_names:
                child = directory_path / name
                if child.is_symlink():
                    raise UnpackError(f"7z created a symbolic link: {child.name!r}")
            for name in file_names:
                child = directory_path / name
                relative_path = child.relative_to(staging)
                if child.is_symlink() or not child.is_file():
                    raise UnpackError(f"7z created an unsafe file type: {relative_path}")
                actual_files.add(relative_path)
        if actual_files != expected_files:
            raise UnpackError("7z extracted files do not match its validated listing")

        for member in members:
            if member.is_directory:
                _ensure_directory(target_root, member.relative_path)
                continue
            target = _target_for_file(target_root, member.relative_path)
            with (staging / member.relative_path).open("rb") as source:
                _copy_bounded(source, target, member.size, state)


def _archive_stem(filename: str) -> str:
    lowered = filename.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            filename = filename[: -len(suffix)]
            break
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return safe_name or "archive"


def _nested_target(archive_path: Path) -> Path:
    base = f"{_archive_stem(archive_path.name)}_contents"
    candidate = archive_path.parent / base
    suffix = 2
    while _lexists(candidate):
        candidate = archive_path.parent / f"{base}_{suffix}"
        suffix += 1
    return candidate


def _relative_display(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def unpack_recursive(source_archive: Path, output_directory: Path, limits: Limits) -> dict:
    source_archive = source_archive.expanduser().resolve(strict=True)
    if not source_archive.is_file():
        raise UnpackError(f"source archive is not a file: {source_archive}")
    root_kind = archive_kind(source_archive)
    if root_kind is None:
        raise UnpackError("source is not a supported ZIP, RAR, or 7z archive")

    output_directory = output_directory.expanduser().resolve(strict=False)
    output_parent = output_directory.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    if _lexists(output_directory):
        raise UnpackError(f"output directory already exists: {output_directory}")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_directory.name}.tmp-", dir=output_parent)
    )
    state = State(limits=limits)
    queue = deque([(source_archive, staging, 0, root_kind)])

    try:
        while queue:
            archive_path, target_root, depth, kind = queue.popleft()
            if depth > limits.max_depth:
                raise UnpackError(f"nested archive depth exceeds limit ({limits.max_depth})")
            if len(state.archives) >= limits.max_archives:
                raise UnpackError(f"archive count exceeds limit ({limits.max_archives})")

            target_root.mkdir(parents=True, exist_ok=False if target_root != staging else True)
            before_count = len(state.files)
            if kind == "zip":
                extract_zip(archive_path, target_root, state)
            elif kind == "rar":
                extract_rar(archive_path, target_root, state)
            elif kind == "7z":
                extract_7z(archive_path, target_root, state)
            else:  # pragma: no cover - queue is populated only after detection
                raise UnpackError(f"unsupported archive kind: {kind}")

            archive_display = (
                source_archive.name
                if archive_path == source_archive
                else _relative_display(archive_path, staging)
            )
            state.archives.append(
                {
                    "path": archive_display,
                    "format": kind,
                    "depth": depth,
                    "extracted_to": (
                        "." if target_root == staging else _relative_display(target_root, staging)
                    ),
                }
            )

            for extracted in state.files[before_count:]:
                nested_kind = archive_kind(extracted.path)
                if nested_kind is None:
                    continue
                if depth >= limits.max_depth:
                    raise UnpackError(
                        f"nested archive depth exceeds limit ({limits.max_depth})"
                    )
                queue.append(
                    (extracted.path, _nested_target(extracted.path), depth + 1, nested_kind)
                )

        processed_archives = {
            entry["path"] for entry in state.archives if entry["depth"] > 0
        }
        final_files = [
            {
                "path": _relative_display(extracted.path, staging),
                "size": extracted.size,
            }
            for extracted in state.files
            if _relative_display(extracted.path, staging) not in processed_archives
        ]
        final_files.sort(key=lambda entry: entry["path"])
        manifest = {
            "version": 1,
            "source_archive": source_archive.name,
            "output_directory": str(output_directory),
            "summary": {
                "archive_count": len(state.archives),
                "file_count": len(final_files),
                "expanded_bytes": state.total_bytes,
            },
            "limits": asdict(limits),
            "archives": state.archives,
            "files": final_files,
        }
        (staging / "unpack_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output_directory)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively unpack nested ZIP, RAR, and 7z archives and print a JSON "
            "manifest. Extraction is transactional and rejects traversal, links, "
            "encrypted members, and configured resource limits."
        )
    )
    parser.add_argument("archive", type=Path, help="source ZIP, RAR, or 7z file")
    parser.add_argument("--output", type=Path, help="new output directory")
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--max-archives", type=int, default=100)
    parser.add_argument("--max-files", type=int, default=2_000)
    parser.add_argument("--max-total-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument("--max-single-file-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    output = args.output
    if output is None:
        output = Path("/home/ubuntu/output/unpacked") / _archive_stem(args.archive.name)
    limits = Limits(
        max_depth=max(0, args.max_depth),
        max_archives=max(1, args.max_archives),
        max_files=max(1, args.max_files),
        max_total_bytes=max(1, args.max_total_bytes),
        max_single_file_bytes=max(1, args.max_single_file_bytes),
        timeout_seconds=max(1, min(args.timeout_seconds, 600)),
    )
    try:
        manifest = unpack_recursive(args.archive, output, limits)
    except (OSError, rarfile.Error, zipfile.BadZipFile, UnpackError) as exc:
        print(
            json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"success": True, **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
