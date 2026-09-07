#!/usr/bin/env python3
"""Run trusted Tool plugins installed in the sandbox image."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import importlib.util
import json
import locale
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PLUGIN_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
_PORTABLE_PATH = re.compile(r"^[\x20-\x7e]+$")
_CONTRACT_FILES = ("Dockerfile", "pyproject.toml", "supervisord.conf", "uv.lock")
_CONTRACT_DIRECTORIES = ("app", "scientific_operators", "scripts")
_MAX_PLUGIN_OUTPUT_BYTES = 2 * 1024 * 1024
_PIPE_READ_CHUNK_BYTES = 64 * 1024
_FORWARDED_SIGNAL_KILL_GRACE_SECONDS = 0.25


@dataclass
class _SharedOutputBudget:
    """Allocate one hard in-memory budget across stdout and stderr."""

    remaining: int
    exceeded: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    exceeded_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )

    def retain(self, chunk: bytes) -> bytes:
        with self._lock:
            retained_bytes = min(len(chunk), self.remaining)
            self.remaining -= retained_bytes
            if retained_bytes != len(chunk):
                self.exceeded = True
                self.exceeded_event.set()
            return chunk[:retained_bytes]


@dataclass
class _BoundedPipeCapture:
    budget: _SharedOutputBudget
    content: bytearray = field(default_factory=bytearray)
    error: BaseException | None = None

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(_PIPE_READ_CHUNK_BYTES)
                if not chunk:
                    break
                retained = self.budget.retain(chunk)
                if retained:
                    self.content.extend(retained)
        except BaseException as exc:
            self.error = exc
        finally:
            stream.close()


@dataclass(frozen=True)
class _BoundedCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    output_limit_exceeded: bool


def _run_command_with_bounded_output(command: list[str]) -> _BoundedCommandResult:
    """Drain both child pipes while retaining at most the shared hard limit.

    Reading continues after the limit is reached so a well-behaved child can
    exit instead of deadlocking on a full OS pipe. The discarded bytes never
    enter a Python aggregate or the outer ShellService output buffer.
    """
    termination_signals = (signal.SIGTERM, signal.SIGINT)
    can_forward_signals = (
        os.name == "posix"
        and threading.current_thread() is threading.main_thread()
    )
    previous_handlers: dict[signal.Signals, Any] = {}
    escalation_started = threading.Event()
    escalation_threads: list[threading.Thread] = []
    process: subprocess.Popen[bytes] | None = None
    pending_termination_signal: signal.Signals | None = None

    def signal_tool_tree(sig: signal.Signals) -> None:
        active_process = process
        if active_process is None:
            return
        try:
            if can_forward_signals:
                os.killpg(active_process.pid, sig)
            else:
                active_process.send_signal(sig)
        except ProcessLookupError:
            return
        except (AttributeError, PermissionError):
            try:
                if active_process.poll() is None:
                    active_process.send_signal(sig)
            except ProcessLookupError:
                pass

    def force_kill_forwarded_signal() -> None:
        time.sleep(_FORWARDED_SIGNAL_KILL_GRACE_SECONDS)
        # The group leader may have exited after TERM while a descendant that
        # inherited its pipes ignores the signal. Always target the owned
        # group; killpg safely reports ESRCH once every member is gone.
        signal_tool_tree(signal.SIGKILL)

    def ensure_signal_escalation() -> None:
        if escalation_started.is_set():
            return
        escalation_started.set()
        escalation_thread = threading.Thread(
            target=force_kill_forwarded_signal,
            name="plugin-signal-escalation",
        )
        escalation_threads.append(escalation_thread)
        escalation_thread.start()

    def forward_termination(sig_number: int, _frame: Any) -> None:
        nonlocal pending_termination_signal
        requested_signal = signal.Signals(sig_number)
        if process is None:
            pending_termination_signal = requested_signal
            return
        signal_tool_tree(requested_signal)
        ensure_signal_escalation()

    if can_forward_signals:
        for termination_signal in termination_signals:
            previous_handlers[termination_signal] = signal.getsignal(
                termination_signal
            )
            signal.signal(termination_signal, forward_termination)

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # The inner group lets the runner stop an entire tool tree when
            # its output budget is exceeded. TERM/INT forwarding below keeps
            # it coupled to the outer ShellService cancellation group.
            start_new_session=can_forward_signals,
        )
        if pending_termination_signal is not None:
            signal_tool_tree(pending_termination_signal)
            ensure_signal_escalation()
    except BaseException:
        if can_forward_signals:
            for termination_signal, handler in previous_handlers.items():
                signal.signal(termination_signal, handler)
        raise

    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        if can_forward_signals:
            for termination_signal, handler in previous_handlers.items():
                signal.signal(termination_signal, handler)
        raise RuntimeError("Plugin tool output pipes are unavailable")

    budget = _SharedOutputBudget(_MAX_PLUGIN_OUTPUT_BYTES)
    stdout_capture = _BoundedPipeCapture(budget)
    stderr_capture = _BoundedPipeCapture(budget)
    readers = (
        threading.Thread(
            target=stdout_capture.drain,
            args=(process.stdout,),
            name="plugin-stdout-reader",
        ),
        threading.Thread(
            target=stderr_capture.drain,
            args=(process.stderr,),
            name="plugin-stderr-reader",
        ),
    )
    for reader in readers:
        reader.start()

    try:
        while process.poll() is None or any(reader.is_alive() for reader in readers):
            if not budget.exceeded_event.wait(timeout=0.05):
                continue
            # A group leader can exit after spawning a descendant that keeps
            # an inherited pipe open. Keep monitoring the readers so that
            # such a descendant cannot bypass the aggregate output ceiling.
            signal_tool_tree(signal.SIGKILL)
            break
        returncode = process.wait()
        for reader in readers:
            reader.join()
        for escalation_thread in escalation_threads:
            escalation_thread.join()
    finally:
        if can_forward_signals:
            for termination_signal, handler in previous_handlers.items():
                signal.signal(termination_signal, handler)

    capture_error = stdout_capture.error or stderr_capture.error
    if capture_error is not None:
        raise RuntimeError("Unable to capture plugin tool output") from capture_error
    return _BoundedCommandResult(
        returncode=returncode,
        stdout=bytes(stdout_capture.content),
        stderr=bytes(stderr_capture.content),
        output_limit_exceeded=budget.exceeded,
    )


def tools_directory() -> Path:
    return Path(os.getenv("AI_DATASEEK_TOOLS_DIR", "/opt/ai-dataseek/tools")).resolve()


def execution_contract_directory() -> Path:
    return Path(os.getenv("AI_DATASEEK_EXECUTION_CONTRACT_DIR", "/app")).resolve()


def _ignored_bundle_path(relative_path: Path) -> bool:
    return (
        any(part in {"__pycache__", ".pytest_cache"} for part in relative_path.parts)
        or relative_path.name == ".DS_Store"
        or relative_path.name.endswith(".pyc")
    )


def _read_tree(
    root: Path,
    logical_prefix: str,
) -> tuple[dict[Path, bytes], list[dict[str, str]]]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Execution bundle source is unavailable: {logical_prefix}")
    files: dict[Path, bytes] = {}
    records: list[dict[str, str]] = []
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        relative_path = path.relative_to(root)
        if _ignored_bundle_path(relative_path):
            continue
        logical_path = f"{logical_prefix}/{relative_path.as_posix()}"
        if not _PORTABLE_PATH.fullmatch(logical_path):
            raise RuntimeError(f"Execution bundle path is not portable: {logical_path}")
        if path.is_symlink():
            raise RuntimeError(
                f"Execution bundle cannot contain symbolic links: {logical_path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(
                f"Execution bundle contains an unsupported entry: {logical_path}"
            )
        content = path.read_bytes()
        files[relative_path] = content
        records.append({
            "path": logical_path,
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    return files, records


def _load_registry_from_files(
    root: Path,
    files: dict[Path, bytes],
) -> tuple[dict[str, tuple[dict[str, Any], Path]], str]:
    root = root.expanduser().resolve()
    registry: dict[str, tuple[dict[str, Any], Path]] = {}
    digest_items: list[dict[str, str]] = []
    manifest_files = sorted(
        (
            (relative_path, content)
            for relative_path, content in files.items()
            if len(relative_path.parts) == 2 and relative_path.name == "manifest.json"
        ),
        key=lambda item: item[0].parent.as_posix(),
    )
    for relative_manifest_path, raw_manifest in manifest_files:
        manifest_path = root / relative_manifest_path
        plugin_dir = manifest_path.parent.resolve()
        if root not in plugin_dir.parents:
            raise RuntimeError(f"Plugin escapes tool directory: {plugin_dir}")
        manifest = json.loads(raw_manifest.decode("utf-8"))
        plugin = manifest.get("plugin")
        version = manifest.get("version")
        definitions = manifest.get("tools")
        if not isinstance(plugin, str) or not plugin:
            raise RuntimeError(f"Invalid plugin name in {manifest_path}")
        if not isinstance(version, str) or not _PLUGIN_VERSION.fullmatch(version):
            raise RuntimeError(f"Invalid plugin version in {manifest_path}")
        if not isinstance(definitions, list) or not definitions:
            raise RuntimeError(f"Invalid tool list in {manifest_path}")
        digest_items.append({
            "plugin": plugin,
            "version": version,
            "manifest_digest": hashlib.sha256(raw_manifest).hexdigest(),
        })
        handler_name = manifest.get("handler", "handler.py")
        handler_path = (plugin_dir / handler_name).resolve()
        try:
            relative_handler_path = handler_path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"Invalid plugin handler: {handler_path}") from exc
        if (
            plugin_dir not in handler_path.parents
            or relative_handler_path not in files
            or not handler_path.is_file()
        ):
            raise RuntimeError(f"Invalid plugin handler: {handler_path}")
        for definition in definitions:
            name = definition.get("name") if isinstance(definition, dict) else None
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"Invalid tool definition in {manifest_path}")
            if name in registry:
                raise RuntimeError(f"Duplicate plugin tool name: {name}")
            registry[name] = (definition, handler_path)
    canonical = json.dumps(
        sorted(digest_items, key=lambda item: item["plugin"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return registry, hashlib.sha256(canonical).hexdigest()


def load_registry_snapshot(
    root: Path,
) -> tuple[dict[str, tuple[dict[str, Any], Path]], str]:
    """Load one registry generation and compute the Cordis manifest digest."""
    resolved_root = root.expanduser().resolve()
    files, _ = _read_tree(resolved_root, "tools")
    return _load_registry_from_files(resolved_root, files)


def load_execution_registry_snapshot(
    root: Path,
    contract_root: Path,
) -> tuple[dict[str, tuple[dict[str, Any], Path]], str, str]:
    """Load and hash the complete source contract in one tool-tree read."""
    resolved_root = root.expanduser().resolve()
    files, records = _read_tree(resolved_root, "tools")
    registry, manifest_digest = _load_registry_from_files(resolved_root, files)

    resolved_contract_root = contract_root.expanduser().resolve()
    for filename in _CONTRACT_FILES:
        path = resolved_contract_root / filename
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Sandbox execution contract is incomplete: {filename}")
        content = path.read_bytes()
        records.append({
            "path": f"sandbox/{filename}",
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    for directory in _CONTRACT_DIRECTORIES:
        _, directory_records = _read_tree(
            resolved_contract_root / directory,
            f"sandbox/{directory}",
        )
        records.extend(directory_records)

    canonical = json.dumps(
        sorted(records, key=lambda item: item["path"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return registry, manifest_digest, hashlib.sha256(canonical).hexdigest()


def load_registry(root: Path) -> dict[str, tuple[dict[str, Any], Path]]:
    return load_registry_snapshot(root)[0]


def load_handler(path: Path):
    module_name = f"ai_dataseek_tool_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load plugin handler: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build_command = getattr(module, "build_command", None)
    if not callable(build_command):
        raise RuntimeError(f"Plugin handler has no build_command(): {path}")
    return build_command


def decode_arguments(value: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        result = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid plugin arguments") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Plugin arguments must be a JSON object")
    return result


def run_tool(
    name: str,
    encoded_arguments: str,
    catalog_manifest_digest: str | None = None,
    execution_bundle_digest: str | None = None,
) -> int:
    if execution_bundle_digest is not None:
        registry, actual_manifest_digest, actual_execution_bundle_digest = (
            load_execution_registry_snapshot(
                tools_directory(),
                execution_contract_directory(),
            )
        )
    else:
        registry, actual_manifest_digest = load_registry_snapshot(tools_directory())
        actual_execution_bundle_digest = None
    if catalog_manifest_digest is not None:
        if not _SHA256_HEX.fullmatch(catalog_manifest_digest) or not hmac.compare_digest(
            actual_manifest_digest,
            catalog_manifest_digest,
        ):
            raise RuntimeError(
                "Sandbox tool catalog does not match the active Agent catalog"
            )
    if execution_bundle_digest is not None:
        if not _SHA256_HEX.fullmatch(execution_bundle_digest) or not hmac.compare_digest(
            actual_execution_bundle_digest or "",
            execution_bundle_digest,
        ):
            raise RuntimeError(
                "Sandbox execution bundle does not match the active Agent catalog"
            )
    definition, handler_path = registry.get(name, (None, None))
    if definition is None or handler_path is None:
        raise RuntimeError(f"Unknown plugin tool: {name}")
    command = load_handler(handler_path)(name, decode_arguments(encoded_arguments))
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise RuntimeError(f"Plugin {name} returned an invalid command")
    completed = _run_command_with_bounded_output(command)
    if completed.output_limit_exceeded:
        # Do not forward a truncated JSON document or any attacker-controlled
        # prefix. The CLI boundary emits one small structured failure instead.
        raise RuntimeError(
            "Plugin tool output exceeded the combined in-memory limit"
        )

    encoding = locale.getpreferredencoding(False) or "utf-8"
    stdout = completed.stdout.decode(encoding)
    stderr = completed.stderr.decode(encoding)
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-DataSeek Tool plugin runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="List registered tools")
    list_parser.add_argument("--json", action="store_true")
    run_parser = subparsers.add_parser("run", help="Run one registered tool")
    run_parser.add_argument("name")
    run_parser.add_argument("--catalog-manifest-digest")
    run_parser.add_argument("--execution-bundle-digest")
    run_parser.add_argument("--arguments-base64", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            names = sorted(load_registry(tools_directory()))
            print(json.dumps(names) if args.json else "\n".join(names))
            return 0
        return run_tool(
            args.name,
            args.arguments_base64,
            args.catalog_manifest_digest,
            args.execution_bundle_digest,
        )
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
