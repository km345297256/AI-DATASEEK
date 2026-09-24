"""Private dataset-view preparation and execution-user read admission.

Source bytes/permissions are never changed. Managed data gets an immutable,
metadata-fingerprinted copy; host-path data is only probed, never copied.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import PurePosixPath

import docker


VIEW_DIRECTORY = ".analysis-views-v1"
_DATASET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MESSAGES = {
    "dataset_unreadable": "Dataset files cannot be read by the analysis user. Grant read/traverse access to the source or import an accessible managed copy; source permissions were not changed.",
    "dataset_unsafe": "Dataset contains an unsafe link, special file or changed path and cannot be mounted for analysis.",
    "dataset_changed": "Dataset metadata identity checks disagree in the read-only preparation view. Inspect the preparation diagnostics; this does not prove that source bytes changed.",
    "dataset_limit": "Dataset readability inspection exceeds the safe directory/file limits.",
    "dataset_preparation_failed": "Dataset read-only analysis view could not be prepared. No analysis was started.",
}


class DatasetReadabilityError(RuntimeError):
    def __init__(self, code: str, diagnostic: dict | None = None):
        self.code = code if code in _MESSAGES else "dataset_preparation_failed"
        self.message = _MESSAGES[self.code]
        self.diagnostic = _safe_diagnostic(diagnostic)
        super().__init__(self.message)


def _safe_diagnostic(value):
    """Only anonymous comparison facts may cross the helper boundary."""
    if not isinstance(value, dict):
        return None
    stages = {"entry_open", "file_read", "directory_scan", "source_copy"}
    fields = {"device", "inode", "size", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "link_count"}
    changed = value.get("changed_fields")
    if (not isinstance(value.get("stage"), str) or value["stage"] not in stages
            or not isinstance(value.get("object_kind"), str) or value["object_kind"] not in {"file", "directory"}
            or not isinstance(value.get("object_id"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", value["object_id"])
            or not isinstance(changed, list) or not 1 <= len(changed) <= len(fields)
            or any(not isinstance(field, str) or field not in fields for field in changed)):
        return None
    return {key: value[key] for key in ("stage", "object_kind", "object_id", "changed_fields")}


def _dataset_view_operation(operation, root, dataset_id=None):
    """Fixed stdlib helper, shared by Docker execution and filesystem tests."""
    import errno
    import ctypes
    import hashlib
    import json
    import os
    import re
    import stat
    import struct
    import sys
    from types import SimpleNamespace
    import uuid

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

    failure_diagnostic = None

    def fail(code, diagnostic=None):
        nonlocal failure_diagnostic
        failure_diagnostic = diagnostic
        raise ValueError(code)

    def component(value):
        return (isinstance(value, str) and value not in {"", ".", ".."}
                and not any(char in value for char in "/\\")
                and not any(ord(char) < 32 or ord(char) == 127 for char in value))

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid,
                info.st_nlink)

    def require_same(expected, current, stage, relative):
        if expected != current:
            fields = ("device", "inode", "size", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "link_count")
            fail("dataset_changed", {
                "stage": stage,
                "object_kind": "directory" if stat.S_ISDIR(expected[5]) else "file",
                "object_id": hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16],
                "changed_fields": [field for field, a, b in zip(fields, expected, current) if a != b],
            })

    # A new virtiofs bind mount can report synthetic root ownership until its
    # attributes are fetched from the host. Ordinary fstat/statx may both reuse
    # that cache. Ask for synchronized metadata at EVERY comparison, including
    # directory entries; do not warm up the scan or drop ownership/ctime checks.
    # Linux UAPI struct statx has fixed-width fields and a 256-byte ABI.
    statx = None
    if sys.platform == "linux":
        library = ctypes.CDLL(None, use_errno=True)
        statx = getattr(library, "statx", None)
        if statx is not None:
            statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_uint, ctypes.c_void_p]
            statx.restype = ctypes.c_int

    def metadata(fd, name=None):
        nonlocal statx
        if statx is None:
            # Non-Linux/old-libc fallback remains the original strict check.
            return os.fstat(fd) if name is None else os.stat(name, dir_fd=fd, follow_symlinks=False)
        buffer = ctypes.create_string_buffer(256)
        flags = 0x2000 | (0x1000 if name is None else 0x100)  # FORCE_SYNC | EMPTY_PATH/NOFOLLOW
        if statx(fd, b"" if name is None else os.fsencode(name), flags, 0x7ff, buffer) != 0:
            error = ctypes.get_errno()
            if error == errno.ENOSYS:
                statx = None
                return metadata(fd, name)
            raise OSError(error, "dataset metadata inspection failed")
        raw = buffer.raw
        def field(fmt, offset):
            return struct.unpack_from("=" + fmt, raw, offset)[0]
        # TYPE, MODE, NLINK, UID, GID, MTIME, CTIME, INO and SIZE are required.
        # Missing fields must not be fabricated as zero and admitted.
        if field("I", 0) & 0x3df != 0x3df:
            fail("dataset_preparation_failed")
        return SimpleNamespace(
            st_dev=os.makedev(field("I", 136), field("I", 140)),
            st_ino=field("Q", 32), st_size=field("Q", 40),
            st_mtime_ns=field("q", 112) * 1_000_000_000 + field("I", 120),
            st_ctime_ns=field("q", 96) * 1_000_000_000 + field("I", 104),
            st_mode=field("H", 28), st_uid=field("I", 20), st_gid=field("I", 24),
            st_nlink=field("I", 16),
        )

    def scan(root_fd, *, snapshot=False):
        files, directories, seen = {}, {}, set()
        entries = 0

        def visit(fd, relative, depth):
            nonlocal entries
            info = metadata(fd)
            if not stat.S_ISDIR(info.st_mode):
                fail("dataset_unsafe")
            if depth > 256 or len(directories) >= 40000:
                fail("dataset_limit")
            inode = (info.st_dev, info.st_ino)
            if inode in seen:
                fail("dataset_unsafe")
            seen.add(inode)
            if snapshot and (stat.S_IMODE(info.st_mode) != 0o555 or info.st_uid != os.geteuid()):
                fail("dataset_unsafe")
            directories[relative] = identity(info)
            with os.scandir(fd) as children:
                for entry in children:
                    entries += 1
                    if entries > 200000:
                        fail("dataset_limit")
                    if not component(entry.name):
                        fail("dataset_unsafe")
                    before = metadata(fd, entry.name)
                    path = relative + "/" + entry.name if relative else entry.name
                    if len(path.encode("utf-8", errors="strict")) > 4096:
                        fail("dataset_limit")
                    if stat.S_ISDIR(before.st_mode):
                        child = os.open(entry.name, directory_flags, dir_fd=fd)
                        try:
                            require_same(identity(before), identity(metadata(child)), "entry_open", path)
                            visit(child, path, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                        if len(files) >= 20000:
                            fail("dataset_limit")
                        child = os.open(entry.name, file_flags, dir_fd=fd)
                        try:
                            current = metadata(child)
                            require_same(identity(before), identity(current), "entry_open", path)
                            if snapshot and (stat.S_IMODE(current.st_mode) != 0o444 or current.st_uid != os.geteuid()):
                                fail("dataset_unsafe")
                            # Prove open/read permission for EVERY file without
                            # hashing or consuming the contents of giant inputs.
                            os.read(child, 1)
                            require_same(identity(current), identity(metadata(child)), "file_read", path)
                            files[path] = identity(current)
                        finally:
                            os.close(child)
                    else:
                        fail("dataset_unsafe")
            require_same(identity(info), identity(metadata(fd)), "directory_scan", relative or ".")

        visit(root_fd, "", 0)
        return files, directories

    def open_relative(root_fd, path, *, directory=False):
        fd = os.dup(root_fd)
        try:
            parts = path.split("/") if path else []
            for index, part in enumerate(parts):
                if not component(part):
                    fail("dataset_unsafe")
                flags = directory_flags if directory or index < len(parts) - 1 else file_flags
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def ensure_cache_directory(parent, name):
        created = False
        try:
            os.mkdir(name, 0o755, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        fd = os.open(name, directory_flags, dir_fd=parent)
        try:
            details = metadata(fd)
            if details.st_uid != os.geteuid() or details.st_mode & 0o022:
                fail("dataset_unsafe")
            if created:
                os.fchmod(fd, 0o755)  # Explicit despite an inherited umask 077.
            return fd
        except BaseException:
            os.close(fd)
            raise

    def discard_stage(parent_fd, name):
        # Only our unpublished UUID directory is removed; never a source,
        # published generation or symlink target.
        fd = os.open(name, directory_flags, dir_fd=parent_fd)
        try:
            os.fchmod(fd, 0o755)
            with os.scandir(fd) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        discard_stage(fd, entry.name)
                    else:
                        os.unlink(entry.name, dir_fd=fd)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent_fd)

    def prepare(root_fd):
        if not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", dataset_id):
            fail("dataset_unsafe")
        source_fd = os.open(dataset_id, directory_flags, dir_fd=root_fd)
        cache_fd = parent_fd = None
        try:
            source_files, source_dirs = scan(source_fd)
            if not source_files:
                fail("dataset_unsafe")
            manifest = {"version": 1, "files": source_files, "directories": source_dirs}
            fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            cache_fd = ensure_cache_directory(root_fd, ".analysis-views-v1")
            parent_fd = ensure_cache_directory(cache_fd, dataset_id)

            def verify_view():
                fd = os.open(fingerprint, directory_flags, dir_fd=parent_fd)
                try:
                    files, dirs = scan(fd, snapshot=True)
                    if (set(files) != set(source_files) or set(dirs) != set(source_dirs)
                            or any(files[path][2] != original[2] or files[path][:2] == original[:2]
                                   for path, original in source_files.items())):
                        fail("dataset_unsafe")
                finally:
                    os.close(fd)

            try:
                verify_view()
                return {"ok": True, "fingerprint": fingerprint, "file_count": len(source_files), "reused": True}
            except FileNotFoundError:
                pass

            stage = ".preparing-" + uuid.uuid4().hex
            os.mkdir(stage, 0o700, dir_fd=parent_fd)
            stage_fd = os.open(stage, directory_flags, dir_fd=parent_fd)
            published = False
            try:
                for path in sorted(source_dirs, key=lambda value: (value.count("/"), value)):
                    if not path:
                        continue
                    parent, _, name = path.rpartition("/")
                    fd = open_relative(stage_fd, parent, directory=True)
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    finally:
                        os.close(fd)
                for path, original in sorted(source_files.items()):
                    source = open_relative(source_fd, path)
                    parent, _, name = path.rpartition("/")
                    fd = open_relative(stage_fd, parent, directory=True)
                    target = None
                    try:
                        require_same(original, identity(metadata(source)), "source_copy", path)
                        target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         0o600, dir_fd=fd)
                        while True:
                            content = os.read(source, 1024 * 1024)
                            if not content:
                                break
                            remaining = memoryview(content)
                            while remaining:
                                written = os.write(target, remaining)
                                if written <= 0:
                                    fail("dataset_preparation_failed")
                                remaining = remaining[written:]
                        require_same(original, identity(metadata(source)), "source_copy", path)
                        if metadata(target).st_size != original[2]:
                            fail("dataset_changed")
                        os.fchmod(target, 0o444)
                        os.fsync(target)
                    finally:
                        os.close(source)
                        os.close(fd)
                        if target is not None:
                            os.close(target)
                if scan(source_fd) != (source_files, source_dirs):
                    fail("dataset_changed")
                for path in sorted(source_dirs, key=lambda value: (value.count("/"), value), reverse=True):
                    fd = open_relative(stage_fd, path, directory=True)
                    try:
                        os.fchmod(fd, 0o555)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                try:
                    os.rename(stage, fingerprint, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    published = True
                except OSError as exc:
                    if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY, errno.EACCES}:
                        raise
                    # Another preparation published the same immutable input
                    # signature; validate it, never overwrite or modify it.
                    # Some filesystems report EACCES for a read-only destination
                    # directory. It is accepted ONLY if verify_view succeeds.
                verify_view()
                return {"ok": True, "fingerprint": fingerprint, "file_count": len(source_files), "reused": False}
            finally:
                os.close(stage_fd)
                if not published:
                    discard_stage(parent_fd, stage)
        finally:
            os.close(source_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            if cache_fd is not None:
                os.close(cache_fd)

    try:
        fd = os.open(root, file_flags)
        try:
            info = metadata(fd)
            if operation == "prepare" and stat.S_ISDIR(info.st_mode):
                return prepare(fd)
            if operation != "probe":
                fail("dataset_unsafe")
            if stat.S_ISDIR(info.st_mode):
                files, _ = scan(fd)
                return {"ok": True, "file_count": len(files)}
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                os.read(fd, 1)
                require_same(identity(info), identity(metadata(fd)), "file_read", ".")
                return {"ok": True, "file_count": 1}
            fail("dataset_unsafe")
        finally:
            os.close(fd)
    except PermissionError:
        return {"ok": False, "code": "dataset_unreadable"}
    except ValueError as error:
        result = {"ok": False, "code": str(error) if str(error) in {
            "dataset_unsafe", "dataset_changed", "dataset_limit"} else "dataset_preparation_failed"}
        if failure_diagnostic is not None:
            result["diagnostic"] = failure_diagnostic
        return result
    except OSError:
        return {"ok": False, "code": "dataset_unsafe"}


def _run_helper(client, *, image, operation, mount, dataset_id=None):
    script = inspect.getsource(_dataset_view_operation) + '''
import json, os, pwd, sys
if sys.argv[1] == "probe" and os.geteuid() != pwd.getpwnam("ubuntu").pw_uid:
    print(json.dumps({"ok": False, "code": "dataset_preparation_failed"}))
else:
    print(json.dumps(_dataset_view_operation(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)))
'''
    try:
        output = client.containers.run(image=image, entrypoint="python3",
            command=["-c", script, operation, mount["Target"], *([dataset_id] if dataset_id else [])],
            mounts=[mount], network_disabled=True, read_only=True, remove=True,
            user="ubuntu" if operation == "probe" else "0:0",
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            security_opt=["no-new-privileges:true"],
            **({"cap_drop": ["ALL"]} if operation == "probe" else {}))
        result = json.loads(output.decode("utf-8") if isinstance(output, bytes) else output)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError
        if not result["ok"]:
            raise DatasetReadabilityError(result.get("code"), result.get("diagnostic"))
        if type(result.get("file_count")) is not int or not 0 <= result["file_count"] <= 20000:
            raise ValueError
        return result
    except DatasetReadabilityError:
        raise
    except Exception:
        raise DatasetReadabilityError("dataset_preparation_failed") from None


def verify_dataset_readability(client, *, image: str, source: str) -> None:
    """Read-probe the exact mounted source under the image's actual ubuntu UID."""
    _run_helper(client, image=image, operation="probe",
                mount=docker.types.Mount(target="/dataset", source=source, type="bind", read_only=True))


def prepare_managed_dataset_source(client, *, image: str, volume: str, dataset_id: str) -> str:
    """Return only a private Docker-host path to a verified immutable view."""
    if not isinstance(dataset_id, str) or not _DATASET_ID.fullmatch(dataset_id):
        raise DatasetReadabilityError("dataset_unsafe")
    try:
        volume_root = client.volumes.get(volume).attrs["Mountpoint"]
        if not isinstance(volume_root, str) or not PurePosixPath(volume_root).is_absolute() or ".." in PurePosixPath(volume_root).parts:
            raise ValueError
    except Exception:
        raise DatasetReadabilityError("dataset_preparation_failed") from None
    result = _run_helper(client, image=image, operation="prepare", dataset_id=dataset_id,
        mount=docker.types.Mount(target="/managed", source=volume, type="volume", read_only=False))
    fingerprint = result.get("fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DatasetReadabilityError("dataset_preparation_failed")
    source = str(PurePosixPath(volume_root) / VIEW_DIRECTORY / dataset_id / fingerprint)
    verify_dataset_readability(client, image=image, source=source)
    return source


def prepare_registered_managed_dataset(*, image: str, volume: str, dataset_id: str, timeout: float) -> None:
    client = None
    try:
        client = docker.from_env(timeout=timeout)
        prepare_managed_dataset_source(client, image=image, volume=volume, dataset_id=dataset_id)
    except DatasetReadabilityError:
        raise
    except Exception:
        raise DatasetReadabilityError("dataset_preparation_failed") from None
    finally:
        if client is not None:
            client.close()
