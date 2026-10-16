"""Preserve dynamic sandbox writable files before their AutoRemove stop.

Called only by the locked production entry after its frontend/backend stop.
Docker export deliberately excludes mounted datasets; the migration handles
those separately. No environment, command arguments, or mount paths are saved.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

ARCHIVE_DIRECTORY = Path("/data2/backups/sandboxes")
PROJECT = "ai-dataseek"
FIXED_SERVICES = {"frontend", "backend", "mongodb", "redis", "minio", "sandbox-image", "jupyter-image"}
NAME = re.compile(r"/ai-dataseek-sandbox-[0-9a-f]{8}")
IDENTITY = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
RESERVE_BYTES = 1024 ** 3
ARCHIVE_OVERHEAD = 512 * 1024 ** 2


class SandboxArchiveError(RuntimeError):
    """Fixed operator diagnostics that never include Docker command output."""


def require(condition, message):
    if not condition:
        raise SandboxArchiveError(message)


def execute(run, arguments):
    try:
        return run(arguments)
    except Exception:
        raise SandboxArchiveError("Sandbox operation failed; original containers and completed archives require inspection") from None


def decode(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise SandboxArchiveError("Invalid Docker sandbox metadata") from None


def discover(run, manifest):
    identifiers = set()
    # The union catches both mislabeled project sandboxes and a foreign object
    # occupying the sandbox name prefix. Known fixed Compose services are skipped.
    for filters in (("label=com.docker.compose.project=" + PROJECT,),
                    ("name=^/ai-dataseek-sandbox-",)):
        arguments = ["docker", "container", "ls", "--all", "--quiet", "--no-trunc"]
        for item in filters:
            arguments += ["--filter", item]
        found = execute(run, arguments).split()
        require(all(IDENTITY.fullmatch(item) for item in found), "Invalid sandbox container identity")
        identifiers.update(found)
    if not identifiers:
        return []
    containers = decode(execute(run, ["docker", "container", "inspect", "--size", *sorted(identifiers)]))
    require(isinstance(containers, list) and len(containers) == len(identifiers), "Incomplete sandbox inventory")
    result, seen = [], set()
    for container in containers:
        require(isinstance(container, dict), "Invalid sandbox inventory")
        identity = container.get("Id")
        require(identity in identifiers and identity not in seen, "Sandbox identity changed during inventory")
        seen.add(identity)
        labels = container.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        name = container.get("Name", "")
        if (service in FIXED_SERVICES and name in {"/" + PROJECT + "-" + service + "-1",
                "/DataSeek-v2" + ("" if service == "frontend" else "-" + service)}
                and labels.get("com.docker.compose.project") == PROJECT):
            continue
        validate_target(container, manifest)
        result.append(container)
    return result


def validate_target(container, manifest):
    image = (manifest.get("sandbox") or {}).get("id")
    labels = container.get("Config", {}).get("Labels") or {}
    require(isinstance(image, str) and IMAGE.fullmatch(image), "A pinned sandbox image identity is required before archival")
    require(IDENTITY.fullmatch(container.get("Id", "")) and NAME.fullmatch(container.get("Name", ""))
            and labels.get("com.docker.compose.project") == PROJECT
            and labels.get("com.docker.compose.service") == "sandbox-image"
            and container.get("Image") == image
            and container.get("HostConfig", {}).get("AutoRemove") is True,
            "Unknown production sandbox object; archival stop refused")
    state = container.get("State", {})
    require(type(state.get("Running")) is bool and type(state.get("Paused")) is bool
            and not state.get("Restarting") and not state.get("Dead"), "Sandbox state cannot be safely archived")
    require(type(container.get("SizeRootFs")) is int and container["SizeRootFs"] >= 0,
            "Sandbox filesystem size is unavailable")
    mounts = container.get("Mounts")
    require(isinstance(mounts, list) and all(isinstance(mount, dict)
            and mount.get("Type") == "bind" and mount.get("RW") is False for mount in mounts),
            "Sandbox has unapproved writable or volume mounts; archival stop refused")


def prepare_directory(directory):
    directory = Path(directory).absolute()
    # Never traverse an existing symlink when choosing a root-owned backup path.
    for component in [*reversed(directory.parents), directory]:
        require(not component.is_symlink(), "Sandbox archive directory must not traverse symlinks")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(directory.is_dir(), "Sandbox archive directory is not a directory")
    directory.chmod(0o700)
    return directory


def check_space(directory, containers):
    # RootFs includes the image layers as export stores the merged filesystem.
    needed = RESERVE_BYTES + sum(item["SizeRootFs"] * 5 // 4 + ARCHIVE_OVERHEAD for item in containers)
    require(shutil.disk_usage(directory).free >= needed, "Insufficient free space for durable sandbox archives")


def fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_archive(target, *, cwd, env, directory, timeout):
    identity = target["Id"]
    final = directory / (identity + ".tar")
    temporary = directory / (identity + ".tar.partial")
    metadata_path = directory / (identity + ".json")
    metadata_temporary = directory / (identity + ".json.partial")
    require(not any(path.exists() or path.is_symlink() for path in
                    (final, temporary, metadata_path, metadata_temporary)),
            "A sandbox archive already exists; inspect it before retrying to avoid overwriting preserved data")
    process = None
    owned_partial_paths = []
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        owned_partial_paths.append(temporary)
        with os.fdopen(descriptor, "wb") as output:
            docker = "docker"
            if (shutil.which("docker", path=env.get("PATH")) == "/snap/bin/docker"
                    and os.access("/snap/docker/current/bin/docker", os.X_OK)):
                docker = "/snap/docker/current/bin/docker"
            process = subprocess.Popen([docker, "container", "export", identity], cwd=cwd, env=env,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, "Sandbox archive export timed out; original container was preserved")
                try:
                    code = process.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    require(shutil.disk_usage(directory).free >= RESERVE_BYTES,
                            "Sandbox archive reserve space exhausted; original container was preserved")
                    print(json.dumps({"status": "sandbox_archiving", "id": identity[:12],
                                      "bytes": temporary.stat().st_size}), flush=True)
            require(code == 0, "Sandbox archive export failed; original container was preserved")
            output.flush()
            os.fsync(output.fileno())
        size = temporary.stat().st_size
        require(size >= 1024, "Sandbox export is incomplete; original container was preserved")
        digest = hashlib.sha256()
        with temporary.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
            source.seek(-1024, os.SEEK_END)
            require(source.read() == b"\0" * 1024, "Sandbox export is missing its tar terminator")
        snapshot = {"id": identity, "name": target["Name"], "image": target["Image"],
                    "project": PROJECT, "service": "sandbox-image", "auto_remove": True,
                    "mounts_excluded": len(target["Mounts"]), "rootfs_bytes": target["SizeRootFs"],
                    "archive_bytes": size, "sha256": digest.hexdigest(), "archived_at_unix": int(time.time())}
        descriptor = os.open(metadata_temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        owned_partial_paths.append(metadata_temporary)
        with os.fdopen(descriptor, "w") as output:
            json.dump(snapshot, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        # Hard linking is an atomic no-overwrite publication on this filesystem.
        os.link(temporary, final, follow_symlinks=False)
        temporary.unlink()
        os.link(metadata_temporary, metadata_path, follow_symlinks=False)
        metadata_temporary.unlink()
        fsync_directory(directory)
        return snapshot
    except (OSError, subprocess.SubprocessError):
        raise SandboxArchiveError("Sandbox archive could not be saved; original container was preserved") from None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for partial in owned_partial_paths:
            if partial.exists():
                partial.unlink()


def archive_and_stop_sandboxes(run, manifest, *, cwd, env, directory=ARCHIVE_DIRECTORY, timeout=900):
    """Archive exact dynamic sandboxes, then stop them; caller owns deploy lock.

    No target is stopped until every selected target has a durable export. On a
    failure, containers paused here are resumed, and no volume/image is removed.
    Existing paused state is preserved on failure. Returns sanitized snapshots.
    """
    require(type(timeout) in {int, float} and 0 < timeout <= 1800, "Invalid sandbox archive timeout")
    targets = discover(run, manifest)
    if not targets:
        return []
    directory = prepare_directory(directory)
    check_space(directory, targets)
    paused_here, ready, snapshots = set(), [], []
    try:
        for target in targets:
            identity = target["Id"]
            if target["State"]["Running"] and not target["State"]["Paused"]:
                paused_here.add(identity)
                execute(run, ["docker", "container", "pause", identity])
            inspected = decode(execute(run, ["docker", "container", "inspect", "--size", identity]))
            require(isinstance(inspected, list) and len(inspected) == 1, "Sandbox disappeared before archival")
            frozen = inspected[0]
            validate_target(frozen, manifest)
            require(frozen["Id"] == identity and frozen["Name"] == target["Name"]
                    and (not frozen["State"]["Running"] or frozen["State"]["Paused"]),
                    "Sandbox could not be frozen for archival")
            ready.append(frozen)
        check_space(directory, ready)
        for target in ready:
            print(json.dumps({"status": "sandbox_archive_started", "id": target["Id"][:12]}), flush=True)
            snapshots.append(export_archive(target, cwd=cwd, env=env, directory=directory, timeout=timeout))
        for target in ready:
            identity = target["Id"]
            if target["State"]["Paused"]:
                execute(run, ["docker", "container", "unpause", identity])
                paused_here.discard(identity)
            # AutoRemove may delete a sandbox that exits immediately on unpause.
            try:
                execute(run, ["docker", "container", "stop", "--time", "30", identity])
            except SandboxArchiveError:
                remaining = execute(run, ["docker", "container", "ls", "--all", "--quiet", "--no-trunc",
                                          "--filter", "id=" + identity]).split()
                require(not remaining, "An archived sandbox could not be stopped")
            remaining = execute(run, ["docker", "container", "ls", "--all", "--quiet", "--no-trunc",
                                      "--filter", "id=" + identity]).split()
            require(not remaining or remaining == [identity], "Unexpected sandbox stop verification result")
            if remaining:
                stopped = decode(execute(run, ["docker", "container", "inspect", identity]))
                require(isinstance(stopped, list) and len(stopped) == 1 and stopped[0].get("Id") == identity
                        and stopped[0].get("State", {}).get("Running") is False,
                        "An archived sandbox is still running")
            print(json.dumps({"status": "sandbox_archived_and_stopped", "id": identity[:12]}), flush=True)
        return snapshots
    finally:
        failures = []
        for identity in sorted(paused_here):
            try:
                state = decode(execute(run, ["docker", "container", "inspect", identity]))
                require(isinstance(state, list) and len(state) == 1 and state[0].get("Id") == identity,
                        "Sandbox state could not be checked before resuming")
                if state[0].get("State", {}).get("Paused"):
                    execute(run, ["docker", "container", "unpause", identity])
            except SandboxArchiveError:
                failures.append(identity)
        require(not failures, "Sandbox archival failed and a sandbox could not be resumed; inspect the preserved containers")
