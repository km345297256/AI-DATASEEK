"""
Shell Service Implementation - Async Version
"""
import os
import uuid
import getpass
import socket
import logging
import asyncio
import codecs
import hashlib
import json
import re
import signal
import threading
import time
import sys
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from app.models.shell import (
    ShellExecResult, ShellViewResult, ShellWaitResult,
    ShellWriteResult, ShellKillResult, ShellTask, ConsoleRecord, ShellExecutionReceipt
)
from app.core.exceptions import AppException, ResourceNotFoundException, BadRequestException
from app.services.program import (
    BOOTSTRAP, append_program_output, prepare_program, program_command, program_feedback,
)
from app.services.shell_output import ShellOutputBuffer, utf8_prefix
from app.services.analysis_workspace import prepare_analysis_workspace

# Set up logger
logger = logging.getLogger(__name__)


def _opaque_log_identifier(value: Any, *, namespace: str) -> str:
    """Return a stable correlation token without logging caller input."""
    if value is None or value == "":
        return ""
    digest = hashlib.sha256(
        str(value).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"{namespace}:sha256:{digest}"


_PUBLIC_COMMAND_FAILURES = frozenset({
    "Shell session was cancelled before process creation",
    "Shell session was cancelled during process creation",
    "Shell session changed while command was running",
})


def _public_command_failure(error: Exception) -> str:
    """Preserve stable control-flow errors without echoing arbitrary details."""
    detail = str(error)
    if isinstance(error, RuntimeError) and detail in _PUBLIC_COMMAND_FAILURES:
        return f"Command execution failed: {detail}"
    return "Command execution failed"


@dataclass
class _PendingShellExec:
    """One in-flight exec generation which can be cancelled before publish."""

    cancelled: bool = False
    operation_id: Optional[str] = None


@dataclass
class _ShellExecutionAttempt:
    session_id: str
    operation_id: str
    command_digest: str
    state: str = "starting"
    creation_attempted: bool = False
    process: Optional[asyncio.subprocess.Process] = None
    process_identity: Optional[int] = None
    process_group: Optional[Dict[str, Any]] = None
    receipt: Optional[ShellExecutionReceipt] = None


class ShellService:
    MAX_CONSOLE_RECORDS = 64
    MAX_CONSOLE_BYTES = 128 * 1024
    EXEC_COMPLETION_GRACE_SECONDS = 5
    OUTPUT_READER_DRAIN_GRACE_SECONDS = 1
    REPLACED_PROCESS_TERMINATION_GRACE_SECONDS = 1
    KILL_PROCESS_TERMINATION_GRACE_SECONDS = 2
    FORCE_KILL_WAIT_SECONDS = 1
    PROCESS_GROUP_POLL_INTERVAL_SECONDS = 0.05
    MAX_PRE_CANCELLED_EXEC_SESSIONS = 4_096
    PRE_CANCELLED_EXEC_TTL_SECONDS = 30.0
    MAX_PENDING_EXECUTION_OPERATIONS = 4_096
    MAX_RECEIPT_PROCESS_SCAN_ENTRIES = 16_384
    RECEIPT_PROCESS_SCAN_SECONDS = 0.1
    # One nonce per server process, shared by all service instances. Restarted
    # servers cannot attest to operations from the previous process.
    SERVER_INSTANCE_ID = uuid.uuid4().hex
    _execution_operations: Dict[str, _ShellExecutionAttempt] = {}

    # Store active shell sessions
    active_shells: Dict[str, Dict[str, Any]] = {}
    
    # Store shell tasks
    shell_tasks: Dict[str, ShellTask] = {}

    # Process creation is asynchronous, so kill/release can arrive after an
    # exec starts but before its process is visible in active_shells. Keep a
    # bounded, generation-specific marker for that gap. A regular threading
    # lock is intentional: critical sections never await and the service is
    # also exercised from more than one event loop by the in-process API tests.
    _session_state_lock = threading.RLock()
    _pending_execs: Dict[str, _PendingShellExec] = {}
    _pre_cancelled_execs: OrderedDict[str, float] = OrderedDict()
    _monotonic = staticmethod(time.monotonic)

    def _register_execution_operation(
        self, session_id: str, operation_id: str, command: str, exec_dir: str,
    ) -> _ShellExecutionAttempt:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise BadRequestException("Invalid execution operation ID")
        digest = hashlib.sha256(json.dumps(
            {"command": command, "exec_dir": exec_dir},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        with self._session_state_lock:
            if operation_id in self._execution_operations:
                raise BadRequestException("Execution operation ID already used")
            # Bound unresolved concurrent operations, not the lifetime amount
            # of completed analysis. Keep terminal nonce/receipt identities so
            # freeing an active slot can never authorize an old launch again.
            if sum(item.receipt is None for item in self._execution_operations.values()) >= self.MAX_PENDING_EXECUTION_OPERATIONS:
                raise BadRequestException("Pending execution operation capacity reached")
            record = _ShellExecutionAttempt(session_id, operation_id, digest)
            self._execution_operations[operation_id] = record
            return record

    def _receipt_group_is_quiescent(self, process_group: Optional[Dict[str, Any]]) -> bool:
        """Prove the original group has no running members; never signal it.

        The teardown helper's `retired` flag is intentionally not proof: it can
        also indicate PID reuse or inability to validate an identity.
        """
        if os.name != "posix" or not process_group:
            return False
        pgid = process_group.get("process_group_id")
        if type(pgid) is not int or pgid <= 1 or pgid == os.getpgrp():
            return False
        if process_group.get("session_id") != pgid or process_group.get("leader_pid") != pgid:
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        # Signal-zero sees zombies too. On Linux we can prove an otherwise
        # present group contains only inert zombies. A failed read of a still
        # present /proc entry invalidates that proof; never treat it as empty.
        if not os.path.isdir("/proc"):
            return False
        deadline = self._monotonic() + self.RECEIPT_PROCESS_SCAN_SECONDS
        try:
            with os.scandir("/proc") as entries:
                for scanned, entry in enumerate(entries):
                    if (scanned >= self.MAX_RECEIPT_PROCESS_SCAN_ENTRIES
                        or self._monotonic() >= deadline):
                        return False
                    if not entry.name.isdigit():
                        continue
                    identity = self._read_linux_process_identity(int(entry.name))
                    if identity is None:
                        if os.path.exists(entry.path):
                            return False
                        continue
                    if identity["process_group_id"] == pgid and identity["state"] not in {"Z", "X"}:
                        return False
        except OSError:
            return False
        return True

    def _execution_receipt(self, record: _ShellExecutionAttempt) -> ShellExecutionReceipt:
        with self._session_state_lock:
            if record.receipt is not None:
                return record.receipt.model_copy()
            state = record.state
            returncode = None
            quiescent = state == "not_started"
            if record.process is not None:
                observed_returncode = record.process.returncode
                if type(observed_returncode) is int:
                    state = "exited"
                    returncode = observed_returncode
                    quiescent = self._receipt_group_is_quiescent(record.process_group)
                elif observed_returncode is None:
                    state = "running"
                else:
                    state = "unknown"
            receipt = ShellExecutionReceipt(
                operation_id=record.operation_id,
                command_digest=record.command_digest,
                server_instance_id=self.SERVER_INSTANCE_ID,
                state=state,
                returncode=returncode,
                process_tree_quiescent=quiescent,
            )
            if quiescent:
                record.receipt = receipt
                record.process = None
                record.process_group = None
            return receipt.model_copy()

    async def operation_status(self, session_id: str, operation_id: str) -> Optional[ShellExecutionReceipt]:
        with self._session_state_lock:
            record = self._execution_operations.get(operation_id)
            if record is None or record.session_id != session_id:
                return None
            return self._execution_receipt(record)

    def _require_current_operation(
        self, session_id: str, operation_id: str,
        process: Optional[asyncio.subprocess.Process] = None,
    ) -> None:
        with self._session_state_lock:
            record = self._execution_operations.get(operation_id)
            shell = self.active_shells.get(session_id)
            if (record is None or record.session_id != session_id or not shell
                or shell.get("operation_id") != operation_id
                or record.process_identity != id(shell.get("process"))
                or (process is not None and shell.get("process") is not process)):
                raise BadRequestException("Execution operation no longer owns shell")

    def _prune_expired_pre_cancellations(self, now: float) -> None:
        """Discard expired tombstones; caller must hold _session_state_lock."""
        expired_session_ids = [
            session_id
            for session_id, expires_at in self._pre_cancelled_execs.items()
            if expires_at <= now
        ]
        for session_id in expired_session_ids:
            self._pre_cancelled_execs.pop(session_id, None)

    def _begin_exec(
        self,
        session_id: str,
        operation_id: Optional[str] = None,
    ) -> tuple[_PendingShellExec, Optional[Dict[str, Any]]]:
        operation = _PendingShellExec(operation_id=operation_id)
        with self._session_state_lock:
            self._prune_expired_pre_cancellations(self._monotonic())
            if session_id in self._pre_cancelled_execs:
                self._pre_cancelled_execs.pop(session_id, None)
                operation.cancelled = True
            previous_operation = self._pending_execs.get(session_id)
            if previous_operation is not None:
                # Only the newest exec for one private session may publish a
                # process. The displaced generation will clean up its process.
                previous_operation.cancelled = True
            self._pending_execs[session_id] = operation
            shell = self.active_shells.get(session_id)
        return operation, shell

    def _cancel_pending_exec(
        self,
        session_id: str,
        operation_id: Optional[str] = None,
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
        """Cancel the current generation and atomically snapshot its shell."""
        with self._session_state_lock:
            now = self._monotonic()
            self._prune_expired_pre_cancellations(now)
            operation = self._pending_execs.get(session_id)
            shell = self.active_shells.get(session_id)
            if operation_id is not None:
                record = self._execution_operations.get(operation_id)
                if record is None or record.session_id != session_id:
                    raise BadRequestException("Execution operation no longer owns shell")
                if operation is not None:
                    if operation.operation_id != operation_id:
                        raise BadRequestException("Execution operation no longer owns shell")
                    # Cancelling a replacement before publish must not target
                    # the prior shell merely because its ID is still visible.
                    if shell is not None and shell.get("operation_id") != operation_id:
                        shell = None
                else:
                    self._require_current_operation(session_id, operation_id)
            if operation is not None:
                operation.cancelled = True
            if operation is None and shell is None:
                # A cancellation HTTP request may overtake the corresponding
                # exec request before its handler registers a pending marker.
                # Remember exactly one future generation for this unique ID.
                self._pre_cancelled_execs[session_id] = (
                    now + self.PRE_CANCELLED_EXEC_TTL_SECONDS
                )
                self._pre_cancelled_execs.move_to_end(session_id)
                while (
                    len(self._pre_cancelled_execs)
                    > self.MAX_PRE_CANCELLED_EXEC_SESSIONS
                ):
                    self._pre_cancelled_execs.popitem(last=False)
        return operation is not None, shell

    def _exec_can_continue(
        self,
        session_id: str,
        operation: _PendingShellExec,
    ) -> bool:
        with self._session_state_lock:
            return (
                self._pending_execs.get(session_id) is operation
                and not operation.cancelled
            )

    def _publish_exec_shell(
        self,
        session_id: str,
        operation: _PendingShellExec,
        previous_shell: Optional[Dict[str, Any]],
        shell: Dict[str, Any],
    ) -> bool:
        """Publish only if no kill, release, or newer exec won the race."""
        with self._session_state_lock:
            if (
                self._pending_execs.get(session_id) is not operation
                or operation.cancelled
                or self.active_shells.get(session_id) is not previous_shell
            ):
                return False
            self.active_shells[session_id] = shell
            return True

    def _exec_owns_shell(
        self,
        session_id: str,
        operation: _PendingShellExec,
        shell: Dict[str, Any],
    ) -> bool:
        with self._session_state_lock:
            return (
                self._pending_execs.get(session_id) is operation
                and not operation.cancelled
                and self.active_shells.get(session_id) is shell
            )

    def _finish_exec(
        self,
        session_id: str,
        operation: _PendingShellExec,
    ) -> None:
        with self._session_state_lock:
            if self._pending_execs.get(session_id) is operation:
                self._pending_execs.pop(session_id, None)

    def _remove_ansi_escape_codes(self, text: str) -> str:
        """Remove ANSI escape codes from text"""
        # Pattern to match ANSI escape sequences
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    def _get_display_path(self, path: str) -> str:
        """Get the path for display, replacing user home directory with ~"""
        home_dir = os.path.expanduser("~")
        if path.startswith(home_dir):
            return path.replace(home_dir, "~", 1)
        return path

    def _format_ps1(self, exec_dir: str) -> str:
        """Format the command prompt"""
        username = getpass.getuser()
        hostname = socket.gethostname()
        display_dir = self._get_display_path(exec_dir)
        return f"{username}@{hostname}:{display_dir} $"

    async def _create_process(self, command: str, exec_dir: str, *, credentials: Optional[Dict[str, str]] = None) -> asyncio.subprocess.Process:
        """Create a new async subprocess"""
        logger.debug(
            "Creating shell process command_bytes=%d",
            len(command.encode("utf-8", errors="replace")),
        )
        process_kwargs: Dict[str, Any] = {}
        if credentials:
            # Only purpose-specific slot variables, never PATH, HOME, proxies,
            # loader options or the server's own environment. One child only.
            if len(credentials) > 8 or any(
                not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", slot)
                or not isinstance(value, str) or not 8 <= len(value.encode()) <= 8192 or "\x00" in value
                for slot, value in credentials.items()
            ):
                raise BadRequestException("Invalid credential slots")
            process_kwargs["env"] = {
                **os.environ,
                **{f"DATASEEK_CREDENTIAL_{slot.upper()}": value for slot, value in credentials.items()},
            }
        if os.name == "posix":
            # A command may launch an arbitrary descendant tree (for example,
            # ai-dataseek-tool -> subprocess.run).  Giving the shell its own
            # session also makes it the leader of a process group that can be
            # terminated without touching the sandbox server's group.
            process_kwargs["start_new_session"] = True

        prepare_analysis_workspace()
        process = await asyncio.create_subprocess_shell(
            command,
            executable="/bin/bash",
            cwd=exec_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # Redirect stderr to stdout
            stdin=asyncio.subprocess.PIPE,
            limit=1024*1024,  # Set buffer size to 1MB
            **process_kwargs,
        )

        process_group = self._capture_created_process_group(process)
        setattr(process, "_dataseek_process_group", process_group)
        return process

    async def _create_program_process(self, exec_dir: str, script_path: str,
                                      args: list[str], *, receipt_record=None) -> asyncio.subprocess.Process:
        prepare_analysis_workspace()
        snapshot, diagnostics, metadata = prepare_program(script_path, args)
        try:
            if receipt_record is not None:
                receipt_record.creation_attempted = True
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", BOOTSTRAP, str(snapshot.fileno()),
                str(diagnostics.fileno()), script_path, *args,
                cwd=exec_dir, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.PIPE,
                start_new_session=True,
                pass_fds=(snapshot.fileno(), diagnostics.fileno()), limit=1024 * 1024,
            )
        except BaseException:
            diagnostics.close()
            raise
        finally:
            snapshot.close()
        setattr(process, "_dataseek_process_group", self._capture_created_process_group(process))
        setattr(process, "_dataseek_program_execution", metadata)
        setattr(process, "_dataseek_program_diagnostics", diagnostics)
        return process

    def _read_linux_process_identity(self, pid: int) -> Optional[Dict[str, Any]]:
        """Read one Linux process identity from /proc without external commands."""
        if not os.path.isdir("/proc"):
            return None

        try:
            with open(
                os.path.join("/proc", str(pid), "stat"),
                "r",
                encoding="utf-8",
            ) as stat_file:
                stat = stat_file.read()
            # The process name is parenthesized and may itself contain spaces.
            fields = stat[stat.rfind(")") + 2:].split()
            return {
                "pid": pid,
                "state": fields[0],
                "process_group_id": int(fields[2]),
                "session_id": int(fields[3]),
                # /proc/<pid>/stat field 22, indexed from field 3 above.
                "start_time": int(fields[19]),
            }
        except (OSError, ValueError, IndexError):
            return None

    def _capture_created_process_group(
        self,
        process: asyncio.subprocess.Process,
    ) -> Optional[Dict[str, Any]]:
        """Capture the identity of the new session created for one command."""
        if os.name != "posix":
            return None

        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 1:
            return None

        current_process_group_id = os.getpgrp()
        if pid == current_process_group_id:
            return None

        # start_new_session=True above establishes PGID == SID == PID before
        # exec.  The leader may have already exited by the time this coroutine
        # resumes, so the launch contract is the source of truth in that race.
        leader_identity = self._read_linux_process_identity(pid)
        if leader_identity is not None and (
            leader_identity["process_group_id"] != pid
            or leader_identity["session_id"] != pid
        ):
            logger.error(
                "New shell did not become its expected group leader pid=%s identity=%s",
                pid,
                leader_identity,
            )
            return None

        return {
            "process_group_id": pid,
            "session_id": pid,
            "leader_pid": pid,
            "leader_start_time": (
                leader_identity["start_time"] if leader_identity else None
            ),
            # Once an owned group is observed empty or reused, it is never
            # eligible for signalling again even if the numeric PGID reappears.
            "retired": False,
        }

    def _capture_running_process_group(
        self,
        process: asyncio.subprocess.Process,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort identity for legacy/fake processes not created above."""
        if os.name != "posix":
            return None

        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 1:
            return None

        try:
            process_group_id = os.getpgid(pid)
            current_process_group_id = os.getpgrp()
        except (OSError, AttributeError):
            return None

        # start_new_session=True guarantees this for processes created above.
        # Re-check it at signal time so a foreign or inherited group can never
        # be targeted, and explicitly exclude the sandbox server's own group.
        if process_group_id != pid or process_group_id == current_process_group_id:
            logger.warning(
                "Refusing process-group signal pid=%s pgid=%s current_pgid=%s",
                pid,
                process_group_id,
                current_process_group_id,
            )
            return None

        leader_identity = self._read_linux_process_identity(pid)
        return {
            "process_group_id": process_group_id,
            "session_id": (
                leader_identity["session_id"] if leader_identity else process_group_id
            ),
            "leader_pid": pid,
            "leader_start_time": (
                leader_identity["start_time"] if leader_identity else None
            ),
            "retired": False,
        }

    def _linux_process_group_members(
        self,
        process_group_id: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """Return Linux group members, excluding unreaped zombies."""
        if not os.path.isdir("/proc"):
            return None

        members: List[Dict[str, Any]] = []
        try:
            entries = os.scandir("/proc")
        except OSError:
            return None

        with entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                identity = self._read_linux_process_identity(int(entry.name))
                if identity is None or (
                    identity["process_group_id"] != process_group_id
                ):
                    continue
                if identity["state"] not in {"Z", "X"}:
                    members.append(identity)

        return members

    def _process_group_exists(self, process_group_id: int) -> bool:
        """Check whether a verified child process group has live members."""
        if os.name != "posix" or process_group_id <= 1:
            return False

        try:
            if process_group_id == os.getpgrp():
                return False
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group still exists.  Signalling it will fall back safely.
            return True
        except OSError:
            return False

        # A container's PID 1 may reap orphaned grandchildren asynchronously.
        # killpg(..., 0) still sees such zombies even though they cannot run or
        # receive another signal.  Avoid delaying every successful teardown on
        # Linux while keeping the portable signal-zero check as the fallback.
        linux_members = self._linux_process_group_members(process_group_id)
        return True if linux_members is None else bool(linux_members)

    def _process_group_identity_is_current(
        self,
        process_group: Optional[Dict[str, Any]],
    ) -> bool:
        """Validate a cached group identity before every signal."""
        if not process_group or process_group.get("retired"):
            return False

        process_group_id = process_group.get("process_group_id")
        if not isinstance(process_group_id, int) or process_group_id <= 1:
            process_group["retired"] = True
            return False
        if process_group_id == os.getpgrp():
            process_group["retired"] = True
            return False
        if not self._process_group_exists(process_group_id):
            process_group["retired"] = True
            return False

        linux_members = self._linux_process_group_members(process_group_id)
        if linux_members is None:
            # On non-Linux POSIX, start_new_session plus the cached PGID is the
            # strongest portable identity available.  Numeric reuse after an
            # observed empty group is still prevented by the retired flag.
            return True

        expected_session_id = process_group.get("session_id")
        if not linux_members or any(
            member["session_id"] != expected_session_id
            for member in linux_members
        ):
            logger.warning(
                "Refusing reused or foreign process group pgid=%s",
                process_group_id,
            )
            process_group["retired"] = True
            return False

        leader_pid = process_group.get("leader_pid")
        expected_start_time = process_group.get("leader_start_time")
        current_leader = next(
            (member for member in linux_members if member["pid"] == leader_pid),
            None,
        )
        if (
            current_leader is not None
            and expected_start_time is not None
            and current_leader["start_time"] != expected_start_time
        ):
            logger.warning(
                "Refusing recycled process-group leader pid=%s expected_start=%s actual_start=%s",
                leader_pid,
                expected_start_time,
                current_leader["start_time"],
            )
            process_group["retired"] = True
            return False

        if expected_start_time is not None and any(
            member["start_time"] < expected_start_time
            for member in linux_members
        ):
            logger.warning(
                "Refusing process group with pre-existing members pgid=%s",
                process_group_id,
            )
            process_group["retired"] = True
            return False

        return True

    def _signal_process_group(
        self,
        process_group: Dict[str, Any],
        sig: signal.Signals,
    ) -> bool:
        """Signal a child group while guarding against the server's own group."""
        process_group_id = process_group.get("process_group_id")
        if (
            os.name != "posix"
            or not isinstance(process_group_id, int)
            or process_group_id <= 1
        ):
            return False

        try:
            if not self._process_group_identity_is_current(process_group):
                logger.error(
                    "Refusing to signal unverified process group pgid=%s",
                    process_group_id,
                )
                return False
            os.killpg(process_group_id, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.warning(
                "Unable to signal process group pgid=%s signal=%s error_type=%s",
                process_group_id,
                sig.name,
                type(exc).__name__,
            )
            return False

    async def _wait_for_process_tree_exit(
        self,
        process: asyncio.subprocess.Process,
        process_group: Optional[Dict[str, Any]],
        timeout_seconds: float,
    ) -> bool:
        """Wait, with one shared deadline, for the leader and its group to exit."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout_seconds)

        if process.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(process.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False

        while self._process_group_identity_is_current(process_group):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(
                min(self.PROCESS_GROUP_POLL_INTERVAL_SECONDS, remaining)
            )

        return True

    async def _terminate_process_tree(
        self,
        process: asyncio.subprocess.Process,
        timeout_seconds: float,
        process_group: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Terminate one command tree, preferring its verified process group."""
        if process_group is None:
            process_group = getattr(process, "_dataseek_process_group", None)
        if process_group is None:
            process_group = self._capture_running_process_group(process)

        process_group_id = (
            process_group.get("process_group_id") if process_group else None
        )
        tree_was_running = (
            process.returncode is None
            or self._process_group_identity_is_current(process_group)
        )
        if not tree_was_running:
            return False

        group_was_signalled = False

        if process_group is not None:
            group_was_signalled = self._signal_process_group(
                process_group,
                signal.SIGTERM,
            )

        if not group_was_signalled and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

        if await self._wait_for_process_tree_exit(
            process,
            process_group if group_was_signalled else None,
            timeout_seconds,
        ):
            return True

        logger.warning(
            "Process tree did not stop after SIGTERM; forcing termination pid=%s pgid=%s",
            getattr(process, "pid", None),
            process_group_id,
        )

        group_was_killed = False
        if process_group is not None and self._process_group_identity_is_current(
            process_group
        ):
            group_was_killed = self._signal_process_group(
                process_group,
                signal.SIGKILL,
            )

        if not group_was_killed and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

        stopped = await self._wait_for_process_tree_exit(
            process,
            process_group if group_was_killed else None,
            self.FORCE_KILL_WAIT_SECONDS,
        )
        if not stopped:
            logger.warning(
                "Process tree still visible after SIGKILL pid=%s pgid=%s",
                getattr(process, "pid", None),
                process_group_id,
            )
        return True

    def _append_process_output(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
        console_record: Optional[ConsoleRecord],
        output: str,
    ) -> None:
        """Append output only to the process and record that produced it."""
        if not output:
            return

        shell = self.active_shells.get(session_id)
        if not shell:
            return

        buffer = getattr(process, "_dataseek_output_buffer", None)
        if buffer is None:
            # Also supports old in-process adapters without changing their
            # launch/receipt protocol. Production allocates before publish.
            buffer = ShellOutputBuffer()
            setattr(process, "_dataseek_output_buffer", buffer)
            if shell.get("process") is process:
                shell["output_buffer"] = buffer
        if buffer.status == "closed":
            return
        buffer.append(output)

        program_output = None
        if getattr(process, "_dataseek_program_execution", None):
            previous, truncated = getattr(process, "_dataseek_program_output", ("", False))
            program_output, truncated = append_program_output(previous, output, truncated)
            setattr(process, "_dataseek_program_output", (program_output, truncated))

        if shell.get("process") is process:
            if program_output is None:
                shell["output"] = buffer.preview()
            else:
                shell["output"] = program_output
                shell["output_truncated"] = truncated

        if console_record is not None and any(
            record is console_record for record in shell.get("console", [])
        ):
            if program_output is None:
                console_record.output = buffer.preview()
            else:
                console_record.output = program_output
            console_record.output_truncated = (buffer.total_bytes > buffer.preview_bytes
                                                if program_output is None else truncated)
        self._bound_console(shell)

    def _bound_console(self, shell: Dict[str, Any]) -> None:
        """Bound legacy console history as well as the current output view."""
        records = shell.get("console", [])
        def size(record):
            return sum(len(value.encode("utf-8")) for value in (record.ps1, record.command, record.output))
        total = sum(size(record) for record in records)
        while len(records) > 1 and (len(records) > self.MAX_CONSOLE_RECORDS or total > self.MAX_CONSOLE_BYTES):
            total -= size(records.pop(0))
            shell["console_truncated"] = True
        if records and total > self.MAX_CONSOLE_BYTES:
            record = records[-1]
            # An unusually long command must not defeat the console cap. This
            # changes only its display copy, never the execution identity.
            for field in ("ps1", "command", "output"):
                setattr(record, field, utf8_prefix(getattr(record, field).encode("utf-8"), self.MAX_CONSOLE_BYTES // 3).decode("utf-8"))
            record.output_truncated = True
            shell["console_truncated"] = True

    async def _start_output_reader(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
        console_record: Optional[ConsoleRecord] = None,
    ):
        """Start a coroutine to continuously read process output and store it"""
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        logger.debug("Starting output reader session=%s", session_ref)
        shell = self.active_shells.get(session_id)
        if console_record is None and shell and shell.get("process") is process:
            console = shell.get("console") or []
            console_record = console[-1] if console else None

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        reached_eof = False
        try:
            while process.stdout:
                try:
                    buffer = await process.stdout.read(64 * 1024)
                    if not buffer:
                        # Process output ended
                        reached_eof = True
                        break

                    output = decoder.decode(buffer, final=False)
                    self._append_process_output(
                        session_id,
                        process,
                        console_record,
                        output,
                    )
                except Exception as e:
                    logger.error(
                        "Error reading process output error_type=%s",
                        type(e).__name__,
                    )
                    break
        finally:
            remaining_output = decoder.decode(b"", final=True)
            self._append_process_output(
                session_id,
                process,
                console_record,
                remaining_output,
            )
            captured = getattr(process, "_dataseek_output_buffer", None)
            if captured is not None:
                captured.stream_complete = reached_eof

        logger.debug("Output reader finished session=%s", session_ref)

    async def _wait_for_output_reader(
        self,
        session_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Give the matching reader bounded time to drain after process exit."""
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        shell = self.active_shells.get(session_id)
        if not shell or shell.get("process") is not process:
            return

        reader_task = shell.get("reader_task")
        if not reader_task or reader_task is asyncio.current_task():
            return

        try:
            await asyncio.wait_for(
                asyncio.shield(reader_task),
                timeout=self.OUTPUT_READER_DRAIN_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Output reader for session %s did not reach EOF within %ss",
                session_ref,
                self.OUTPUT_READER_DRAIN_GRACE_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Output reader failed session=%s error_type=%s",
                session_ref,
                type(e).__name__,
            )

    async def exec_program(self, session_id: str, exec_dir: str, script_path: str,
                           args: list[str], *, operation_id: Optional[str] = None) -> ShellExecResult:
        if not os.path.isabs(exec_dir):
            raise BadRequestException("Program working directory must be absolute")
        return await self.exec_command(
            session_id, exec_dir, program_command(script_path, args),
            operation_id=operation_id, _program=(script_path, args),
        )

    async def exec_command(self, session_id: str, exec_dir: Optional[str], command: str, *, credentials: Optional[Dict[str, str]] = None, operation_id: Optional[str] = None, _program: Optional[tuple[str, list[str]]] = None) -> ShellExecResult:
        """
        Asynchronously execute a command in the specified shell session
        """
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        command_bytes = len(command.encode("utf-8", errors="replace"))
        logger.info(
            "Executing command session=%s command_bytes=%d",
            session_ref,
            command_bytes,
        )
        if not exec_dir:
            exec_dir = os.path.expanduser("~")
        exec_dir = os.path.abspath(os.path.normpath(exec_dir))
        receipt_record = (self._register_execution_operation(session_id, operation_id, command, exec_dir)
                          if operation_id is not None else None)
        # A first program may legitimately use the platform's output directory
        # as cwd. Prepare that fixed directory before checking caller cwd;
        # arbitrary missing working/input directories must still fail below.
        try:
            prepare_analysis_workspace()
        except BadRequestException:
            if receipt_record is not None:
                receipt_record.state = "not_started"
            raise
        # Ensure directory exists
        if not os.path.exists(exec_dir):
            if receipt_record is not None:
                receipt_record.state = "not_started"
            logger.error(
                "Execution directory does not exist path=%s",
                _opaque_log_identifier(exec_dir, namespace="path"),
            )
            raise BadRequestException("Execution directory does not exist")
        
        operation: Optional[_PendingShellExec] = None
        try:
            # Create PS1 format
            ps1 = self._format_ps1(exec_dir)

            operation, previous_shell = self._begin_exec(session_id, operation_id)

            if previous_shell is None:
                logger.debug("Creating new shell session=%s", session_ref)
            else:
                # Execute command in an existing session
                logger.debug("Using existing shell session=%s", session_ref)
                old_process = previous_shell["process"]
                
                # A completed leader may have left live descendants in its
                # cached group, so always ask the tree terminator to check it.
                logger.debug(
                    "Terminating previous process tree session=%s",
                    session_ref,
                )
                await self._terminate_process_tree(
                    old_process,
                    timeout_seconds=(
                        self.REPLACED_PROCESS_TERMINATION_GRACE_SECONDS
                    ),
                    process_group=previous_shell.get("process_group"),
                )

                await self._wait_for_output_reader(session_id, old_process)
                program_feedback(previous_shell)

            # A release/kill during replacement can avoid spawning altogether.
            if not self._exec_can_continue(session_id, operation):
                raise RuntimeError("Shell session was cancelled before process creation")

            if receipt_record is not None and _program is None:
                receipt_record.creation_attempted = True
            if _program is not None:
                process = await self._create_program_process(exec_dir, *_program, receipt_record=receipt_record)
            else:
                process = (await self._create_process(command, exec_dir, credentials=credentials)
                           if credentials else await self._create_process(command, exec_dir))
            if receipt_record is not None:
                receipt_record.process = process
                receipt_record.process_identity = id(process)
                receipt_record.process_group = getattr(process, "_dataseek_process_group", None)
            console_record = ConsoleRecord(ps1=ps1, command=command, output="")
            console_history = (
                list(previous_shell.get("console", []))
                if previous_shell is not None
                else []
            )
            console_history.append(console_record)
            output_buffer = ShellOutputBuffer()
            setattr(process, "_dataseek_output_buffer", output_buffer)
            shell = {
                "process": process,
                "process_group": getattr(
                    process,
                    "_dataseek_process_group",
                    None,
                ),
                "exec_dir": exec_dir,
                "output": "",
                "output_buffer": output_buffer,
                "console": console_history,
                "console_truncated": bool(previous_shell and previous_shell.get("console_truncated")),
                "operation_id": operation_id,
                "program_execution": getattr(process, "_dataseek_program_execution", None),
                "program_diagnostics": getattr(process, "_dataseek_program_diagnostics", None),
            }
            self._bound_console(shell)

            if not self._publish_exec_shell(
                session_id,
                operation,
                previous_shell,
                shell,
            ):
                # kill/release is allowed to complete while process creation is
                # blocked. If creation then returns, terminate before exposing
                # the process or starting its output reader.
                await self._terminate_process_tree(
                    process,
                    timeout_seconds=self.KILL_PROCESS_TERMINATION_GRACE_SECONDS,
                    process_group=shell.get("process_group"),
                )
                program_feedback(shell)
                output_buffer.close()
                raise RuntimeError("Shell session was cancelled during process creation")

            if previous_shell is not None:
                # Replacement has its own output identity. Retire the previous
                # private files; a stale cursor can never read the new launch.
                self._retire_output(previous_shell)

            shell["reader_task"] = asyncio.create_task(
                self._start_output_reader(session_id, process, console_record)
            )
            
            # Try to wait for the process to complete (max 5 seconds)
            try:
                logger.debug(
                    "Waiting for process completion session=%s",
                    session_ref,
                )
                wait_result = await self.wait_for_process(
                    session_id,
                    seconds=self.EXEC_COMPLETION_GRACE_SECONDS,
                    **({"operation_id": operation_id} if operation_id is not None else {}),
                )
                if wait_result.status == "completed":
                    if not self._exec_owns_shell(session_id, operation, shell):
                        raise RuntimeError("Shell session changed while command was running")
                    # Process has completed, get the output
                    logger.debug(f"Process completed with code: {wait_result.returncode}")
                    view_result = await self.view_shell(
                        session_id, **({"operation_id": operation_id} if operation_id is not None else {}),
                    )
                    
                    return ShellExecResult(
                        session_id=session_id,
                        command=command,
                        status="completed",
                        returncode=wait_result.returncode,
                        output=view_result.output,
                        execution_receipt=self._execution_receipt(receipt_record) if receipt_record else None,
                        program_execution=program_feedback(shell),
                        output_metadata=output_buffer.metadata(),
                    )
            except Exception as e:
                # Other exceptions, ignore and continue
                logger.warning(
                    "Exception while waiting for process error_type=%s",
                    type(e).__name__,
                )
                pass

            if not self._exec_owns_shell(session_id, operation, shell):
                raise RuntimeError("Shell session changed while command was running")

            # Get current console records
            console = self.get_console_records(session_id)
            
            return ShellExecResult(
                session_id=session_id,
                command=command,
                status="running",
                execution_receipt=self._execution_receipt(receipt_record) if receipt_record else None,
                program_execution=program_feedback(shell),
                output_metadata=output_buffer.metadata(),
            )
        except Exception as e:
            logger.error("Command execution failed error_type=%s", type(e).__name__)
            raise AppException(
                message=_public_command_failure(e),
                data={"command_bytes": command_bytes},
            ) from e
        finally:
            if receipt_record is not None and receipt_record.process is None and receipt_record.receipt is None:
                receipt_record.state = "unknown" if receipt_record.creation_attempted else "not_started"
            if operation is not None:
                self._finish_exec(session_id, operation)

    async def view_shell(self, session_id: str, console: bool = False, *, operation_id: Optional[str] = None,
                         output_id: Optional[str] = None, cursor: Optional[int] = None,
                         max_bytes: int = 8192) -> ShellViewResult:
        """
        Asynchronously view the content of the specified shell session
        """
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        logger.debug("Viewing shell content session=%s", session_ref)
        if operation_id is not None:
            self._require_current_operation(session_id, operation_id)
        if session_id not in self.active_shells:
            logger.error("Shell session not found session=%s", session_ref)
            raise ResourceNotFoundException("Shell session does not exist")
        
        shell = self.active_shells[session_id]
        
        # Get raw output and filter ANSI escape codes
        raw_output = shell["output"]
        clean_output = self._remove_ansi_escape_codes(raw_output)
        buffer = shell.get("output_buffer")
        page = None
        if output_id is not None or cursor is not None:
            if buffer is None or output_id != buffer.output_id or cursor is None:
                raise BadRequestException("Output identity is unavailable or has been replaced")
            try:
                clean_output, page = buffer.read(cursor, max_bytes)
            except ValueError as error:
                raise BadRequestException(str(error)) from error
        
        # Get command console records with filtered output
        if console:
            console = self.get_console_records(session_id)
        else:
            console = None
        
        return ShellViewResult(
            output=clean_output,
            session_id=session_id,
            console=console,
            program_execution=program_feedback(shell),
            output_metadata=buffer.metadata() if buffer is not None else None,
            output_page=page,
            console_truncated=bool(shell.get("console_truncated")),
        )

    def get_console_records(self, session_id: str) -> List[ConsoleRecord]:
        """
        Get command console records for the specified session (this method doesn't need to be async)
        """
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        logger.debug("Getting console records session=%s", session_ref)
        if session_id not in self.active_shells:
            logger.error("Shell session not found session=%s", session_ref)
            raise ResourceNotFoundException("Shell session does not exist")
        
        # Get raw console records and filter ANSI escape codes
        raw_console = self.active_shells[session_id]["console"]
        clean_console = []
        for record in raw_console:
            clean_record = ConsoleRecord(
                ps1=record.ps1,
                command=record.command,
                output=self._remove_ansi_escape_codes(record.output),
                output_truncated=record.output_truncated,
            )
            clean_console.append(clean_record)
        
        return clean_console

    async def wait_for_process(self, session_id: str, seconds: Optional[int] = None, *, operation_id: Optional[str] = None) -> ShellWaitResult:
        """
        Asynchronously wait for the process in the specified shell session to return
        """
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        logger.debug(
            "Waiting for process session=%s timeout_seconds=%s",
            session_ref,
            seconds,
        )
        if operation_id is not None:
            self._require_current_operation(session_id, operation_id)
        if session_id not in self.active_shells:
            logger.error("Shell session not found session=%s", session_ref)
            raise ResourceNotFoundException("Shell session does not exist")
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            if seconds is None:
                seconds = 60

            # `asyncio.wait_for(coroutine, timeout=0)` cancels a newly-created
            # coroutine before it can observe an already-finished process.
            if process.returncode is None:
                await asyncio.wait_for(process.wait(), timeout=seconds)

            await self._wait_for_output_reader(session_id, process)
            if operation_id is not None:
                self._require_current_operation(session_id, operation_id, process)
            
            logger.info(f"Process completed with return code: {process.returncode}")
            return ShellWaitResult(
                status="completed",
                returncode=process.returncode
            )
        except asyncio.TimeoutError:
            if process.returncode is not None:
                await self._wait_for_output_reader(session_id, process)
                if operation_id is not None:
                    self._require_current_operation(session_id, operation_id, process)
                return ShellWaitResult(
                    status="completed",
                    returncode=process.returncode,
                )
            logger.info(
                "Process in session %s is still running after waiting %ss",
                session_ref,
                seconds,
            )
            if operation_id is not None:
                self._require_current_operation(session_id, operation_id, process)
            return ShellWaitResult(status="running", returncode=None)
        except Exception as e:
            logger.error("Failed to wait for process error_type=%s", type(e).__name__)
            raise AppException(message="Failed to wait for process") from e

    async def write_to_process(self, session_id: str, input_text: str, press_enter: bool) -> ShellWriteResult:
        """
        Asynchronously write input to the process in the specified shell session
        """
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        logger.debug(
            "Writing to process session=%s press_enter=%s input_bytes=%d",
            session_ref,
            press_enter,
            len(input_text.encode("utf-8", errors="replace")),
        )
        if session_id not in self.active_shells:
            logger.error("Shell session not found session=%s", session_ref)
            raise ResourceNotFoundException("Shell session does not exist")
        
        shell = self.active_shells[session_id]
        process = shell["process"]
        
        try:
            # Check if the process is still running
            if process.returncode is not None:
                logger.error(f"Process has already terminated, cannot write input")
                raise BadRequestException("Process has ended, cannot write input")
            
            # Prepare input data
            if press_enter:
                input_data = f"{input_text}\n".encode()
            else:
                input_data = input_text.encode()
            
            # Add input to output and console records
            input_str = input_data.decode('utf-8')
            self._append_process_output(session_id, process,
                                        shell["console"][-1] if shell["console"] else None, input_str)
            
            # Asynchronously write input
            process.stdin.write(input_data)
            await process.stdin.drain()
            
            logger.info(f"Successfully wrote input to process")
            
            return ShellWriteResult(
                status="success"
            )
        except Exception as e:
            logger.error("Failed to write input error_type=%s", type(e).__name__)
            raise AppException(message="Failed to write input") from e

    async def kill_process(self, session_id: str, *, operation_id: Optional[str] = None) -> ShellKillResult:
        """
        Asynchronously terminate the process in the specified shell session
        """
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        logger.info("Killing process session=%s", session_ref)
        pending_cancelled, shell = self._cancel_pending_exec(session_id, operation_id)
        if shell is None and pending_cancelled:
            # The exec generation will either observe this before spawning or
            # immediately terminate the process returned by its blocked spawn.
            return ShellKillResult(status="terminated", returncode=0)
        if shell is None:
            logger.error("Shell session not found session=%s", session_ref)
            raise ResourceNotFoundException("Shell session does not exist")

        process = shell["process"]
        
        try:
            # The shell leader can exit after daemonizing a descendant.  The
            # cached, launch-time group identity remains authoritative until
            # that group is observed empty or reused.
            terminated = await self._terminate_process_tree(
                process,
                timeout_seconds=self.KILL_PROCESS_TERMINATION_GRACE_SECONDS,
                process_group=shell.get("process_group"),
            )
            if terminated:
                logger.debug("Attempting to terminate process tree gracefully")
                await self._wait_for_output_reader(session_id, process)
                
                logger.info(f"Process terminated with return code: {process.returncode}")
                return ShellKillResult(
                    status="terminated",
                    returncode=process.returncode
                )
            else:
                await self._wait_for_output_reader(session_id, process)
                logger.info(f"Process was already terminated with return code: {process.returncode}")
                return ShellKillResult(
                    status="already_terminated",
                    returncode=process.returncode
                )
        except Exception as e:
            logger.error("Failed to kill process error_type=%s", type(e).__name__)
            raise AppException(message="Failed to terminate process") from e

    async def release_shell(self, session_id: str, *, operation_id: Optional[str] = None) -> ShellKillResult:
        """Idempotently terminate and discard a private shell channel."""
        session_ref = _opaque_log_identifier(session_id, namespace="session")
        _, shell = self._cancel_pending_exec(session_id, operation_id)
        if shell is None:
            return ShellKillResult(status="released", returncode=0)

        process = shell["process"]
        try:
            await self._terminate_process_tree(
                process,
                timeout_seconds=self.KILL_PROCESS_TERMINATION_GRACE_SECONDS,
                process_group=shell.get("process_group"),
            )
            await self._wait_for_output_reader(session_id, process)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Failed to release shell session=%s error_type=%s",
                session_ref,
                type(error).__name__,
            )
            raise AppException(message="Failed to release shell session") from error

        program_feedback(shell)
        self._retire_output(shell)

        # The id is private to one plugin invocation. Identity-check before
        # deletion in case a future caller accidentally attempts reuse.
        with self._session_state_lock:
            if self.active_shells.get(session_id) is shell:
                self.active_shells.pop(session_id, None)
        return ShellKillResult(
            status="released",
            returncode=process.returncode if process.returncode is not None else 0,
        )

    @staticmethod
    def _retire_output(shell: Dict[str, Any]) -> None:
        output_buffer = shell.get("output_buffer")
        if output_buffer is not None:
            output_buffer.close()
        reader = shell.get("reader_task")
        if reader is not None and not reader.done():
            # Process teardown already received its bounded final drain. Do
            # not leave a reader pinned by an inherited pipe after release.
            reader.cancel()

    def create_session_id(self) -> str:
        """
        Create a new session ID (this method doesn't need to be async)
        """
        session_id = str(uuid.uuid4())
        logger.debug(
            "Created new session ID session=%s",
            _opaque_log_identifier(session_id, namespace="session"),
        )
        return session_id

shell_service = ShellService()
