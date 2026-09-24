"""Request-local protocol recovery, separate from scientific progress.

Preparing source can justify one further protocol correction after the usual
correction, but cannot reset loop detection or establish analysis completion.
Only native, pipeline-validated tool calls can ever be dispatched.
"""
from dataclasses import dataclass, field
from copy import deepcopy
from pathlib import PurePosixPath
import posixpath

from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_progress import CODE_SUFFIXES
from app.domain.services.execution_identity import private_identity_hmac


def _core_file_method(tool, name: str) -> bool:
    from app.domain.services.tools.base import Tool
    from app.domain.services.tools.file import FileToolkit
    return (name in {"file_write", "file_str_replace"} and type(tool) is Tool
            and type(getattr(tool, "toolkit", None)) is FileToolkit
            and tool._tool is getattr(FileToolkit, name)
            and any(candidate is tool for candidate in tool.toolkit.tools))


def _core_shell_method(tool, name: str) -> bool:
    from app.domain.services.tools.base import Tool
    from app.domain.services.tools.shell import ShellToolkit
    return (name in {"shell_run", "shell_exec", "program_run"} and type(tool) is Tool
            and type(getattr(tool, "toolkit", None)) is ShellToolkit
            and tool._tool is getattr(ShellToolkit, name)
            and any(candidate is tool for candidate in tool.toolkit.tools))


def confirmed_native_success(call: dict, result) -> bool:
    artifact = getattr(result, "artifact", None)
    return bool(isinstance(artifact, ToolResult) and artifact.success is True
        and getattr(result, "status", None) != "error"
        and getattr(result, "tool_call_id", None) == call.get("id"))


def confirmed_shell_success(tool, call: dict, ledger) -> bool:
    """Trust an identity-bound private terminal receipt, never public output."""
    from app.domain.services.execution_evidence import ToolExecutionLedger, shell_command_digest
    if not _core_shell_method(tool, call.get("name")) or call.get("name") == "program_run":
        return False
    if type(ledger) is not ToolExecutionLedger:
        return False
    args = call.get("args") or {}
    if not isinstance(args.get("exec_dir"), str) or not isinstance(args.get("command"), str):
        return False
    digest = shell_command_digest(args["exec_dir"], args["command"])
    for attempt in reversed(list(ledger._attempts.values())):
        if attempt.tool_call_id != call.get("id"):
            continue
        return bool(attempt.confirmed and attempt.receipt.state == "exited"
            and attempt.receipt.returncode == 0 and attempt.command_digest == digest
            and attempt.sandbox_id == str(tool.toolkit.sandbox.id)
            and attempt.shell_id == args.get("id"))
    return False


def confirmed_program_preparation(tool, call: dict, result) -> bool:
    """A successful first-party file operation is preparation, never execution."""
    args = call.get("args") or {}
    path = args.get("file")
    return bool(_core_file_method(tool, call.get("name"))
        and isinstance(path, str) and PurePosixPath(path).suffix.lower() in CODE_SUFFIXES
        and confirmed_native_success(call, result))


def native_operation_identity(tool, call: dict, *, program_prerequisites: dict | None = None) -> str:
    """Opaque operation identity; no model text or call ID grants novelty.

Normalize known first-party file semantics without invoking schema validators a
second time. Unknown tools retain all arguments, including business object IDs.
A trusted program preflight binds source/working-directory changes, so a real
new program revision is not mistaken for replay just because argv is unchanged.
"""
    name = call.get("name")
    args = dict(call.get("args") or {})
    if _core_file_method(tool, name):
        if isinstance(args.get("file"), str):
            args["file"] = posixpath.normpath(args["file"])
        args["sudo"] = bool(args.get("sudo"))
        if name == "file_write":
            content = args.get("content")
            if isinstance(content, str):
                args["content"] = ("\n" if args.get("leading_newline") else "") + content + ("\n" if args.get("trailing_newline") else "")
            args["append"] = bool(args.get("append"))
            args.pop("leading_newline", None)
            args.pop("trailing_newline", None)
    elif _core_shell_method(tool, name):
        args = {k: v for k, v in args.items() if k not in {"id", "timeout", "timeout_seconds"}}
        if name == "program_run":
            args["argv"] = args.get("argv") or []
        else:
            name = "shell_command"
        for key in ("script_path", "exec_dir"):
            if isinstance(args.get(key), str):
                args[key] = posixpath.normpath(args[key])
    return private_identity_hmac({"purpose": "terminal-protocol-operation/v1", "name": name,
        "arguments": args, "program_prerequisites": program_prerequisites})


@dataclass
class TerminalProtocolRecovery:
    """Lives inside one execute call; no task-wide/model-budget configuration."""

    last_progress: tuple | None = None
    preparation_version: int = 0
    last_correction_preparation: int = 0
    preparation_correction_used: bool = False
    completed_writes: set[str] = field(default_factory=set)
    completed_programs: dict[str, set[str | None]] = field(default_factory=dict)
    successful_executions: set[str] = field(default_factory=set)
    launches: dict[str, tuple] = field(default_factory=dict, repr=False)

    def observe_launch(self, tool, call: dict, *, prerequisites: dict | None = None) -> None:
        if _core_shell_method(tool, call.get("name")):
            # Save before memory compaction. Observer IDs never replace the
            # original launch arguments or grant authority to a plugin result.
            self.launches[call["id"]] = (tool, deepcopy(call), deepcopy(prerequisites))

    def refresh_confirmed_executions(self, ledger) -> None:
        from app.domain.services.program_execution import trusted_program_execution_feedback
        for tool, call, prerequisites in self.launches.values():
            identity = native_operation_identity(tool, call, program_prerequisites=prerequisites)
            if call.get("name") == "program_run":
                feedback = trusted_program_execution_feedback(tool, call, None, ledger)
                if not feedback or feedback.get("returncode") != 0:
                    continue
                self.record_program_success(call, feedback)
                self.record_program_completion(native_operation_identity(tool, call), prerequisites)
            elif confirmed_shell_success(tool, call, ledger):
                self.record_shell_success(identity)
            else:
                continue
            self.record_completed_write(identity, program_preparation=False)

    def progress(self, read_evidence: str) -> tuple:
        # Successful semantic identities only. Failed/new operation IDs cannot
        # reopen protocol correction; receipts prove execution, not science.
        return (read_evidence, frozenset(self.successful_executions))

    def record_program_success(self, call: dict, feedback: dict) -> None:
        if feedback.get("returncode") != 0:
            return
        args = call.get("args") or {}
        self.successful_executions.add(private_identity_hmac({
            "purpose": "terminal-protocol-program-success/v1",
            "source_digest": feedback["source_digest"],
            "argv": args.get("argv") or [],
            "exec_dir": posixpath.normpath(args.get("exec_dir", "")),
        }))

    def record_shell_success(self, identity: str) -> None:
        self.successful_executions.add(identity)

    def record_program_completion(self, base_identity: str, prerequisites: dict | None) -> None:
        fingerprint = prerequisites.get("prerequisite_digest") if prerequisites and prerequisites.get("ready") else None
        self.completed_programs.setdefault(base_identity, set()).add(fingerprint)

    def record_completed_write(self, identity: str, *, program_preparation: bool) -> None:
        if identity in self.completed_writes:
            return
        self.completed_writes.add(identity)
        if program_preparation:
            self.preparation_version += 1

    def correction_kind(self, progress: tuple) -> str | None:
        if progress != self.last_progress:
            self.last_progress = progress
            self.last_correction_preparation = self.preparation_version
            self.preparation_correction_used = False
            return "protocol"
        if (not self.preparation_correction_used
                and self.preparation_version > self.last_correction_preparation):
            self.last_correction_preparation = self.preparation_version
            self.preparation_correction_used = True
            return "prepared_program"
        return None

    def would_replay_completed_write(self, identity: str, *, program_base: str | None = None,
                                    prerequisites: dict | None = None) -> bool:
        if identity in self.completed_writes:
            return True
        prior = self.completed_programs.get(program_base, set())
        if not prior:
            return False
        current = prerequisites.get("prerequisite_digest") if prerequisites and prerequisites.get("ready") else None
        # Unknown freshness is not a new source version. Only a proven new
        # source/cwd fingerprint may distinguish a corrected same-path launch.
        return current is None or None in prior or current in prior
