"""Private adapter-bound program feedback; never inferred from model output."""
import json
import hashlib
import inspect
import re
import asyncio
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProgramExecutionFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1, le=1)
    script_path: str
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    returncode: int | None
    failure_fingerprint: str | None
    diagnostic: dict[str, Any] | None
    output_truncated: bool


def program_command(script_path: str, args: list[str]) -> str:
    return json.dumps({"kind": "python_program", "script_path": script_path,
                       "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


MAX_REVIEW_SOURCE_BYTES = 24_000


def validated_source_snapshot(value, source_digest: str) -> dict | None:
    """Validate complete immutable bytes; this checks shape, not authenticity."""
    if (type(value) is not dict or set(value) != {
            "version", "encoding", "size_bytes", "sha256", "content"}
            or type(value.get("version")) is not int or value["version"] != 1
            or value.get("encoding") != "utf-8"
            or type(value.get("size_bytes")) is not int
            or not 0 <= value["size_bytes"] <= MAX_REVIEW_SOURCE_BYTES
            or not isinstance(value.get("content"), str)
            or len(value["content"]) > MAX_REVIEW_SOURCE_BYTES
            or not isinstance(source_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source_digest)
            or value.get("sha256") != source_digest):
        return None
    try:
        encoded = value["content"].encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if (len(encoded) != value["size_bytes"]
            or hashlib.sha256(encoded).hexdigest() != source_digest):
        return None
    return dict(value)


def consume_program_feedback(result, attempt):
    """Trusted adapter only. Strip private code before every public return.

    Even absent/invalid attempts or malformed feedback cannot leak the private
    source through console views, model observations or persisted ToolResults.
    Input dictionaries are not mutated; private ledger and returned data do not
    share the snapshot object.
    """
    data = result.data if isinstance(result.data, dict) else {}
    raw = data.get("program_execution")
    snapshot = None
    if isinstance(raw, dict):
        public = {key: value for key, value in raw.items() if key != "source_snapshot"}
        result = result.model_copy(update={"data": {**data, "program_execution": public}})
        snapshot = validated_source_snapshot(raw.get("source_snapshot"), raw.get("source_digest"))
    else:
        public = raw
        if raw is not None:
            # A malformed non-object feedback has no legitimate public schema.
            # Do not let nested opaque private fields bypass dictionary removal.
            result = result.model_copy(update={"data": {key: value for key, value in data.items()
                                                        if key != "program_execution"}})
    try:
        feedback = ProgramExecutionFeedback.model_validate(public)
    except (ValueError, TypeError):
        return result
    if (not feedback.script_path.startswith("/")
            or feedback.failure_fingerprint is not None
            and not re.fullmatch(r"[0-9a-f]{64}", feedback.failure_fingerprint)):
        return result
    if attempt is not None:
        prior = attempt.program_execution
        if prior is None or (prior["script_path"] == feedback.script_path
                             and prior["source_digest"] == feedback.source_digest):
            private = feedback.model_dump()
            if snapshot is None and prior is not None:
                snapshot = validated_source_snapshot(prior.get("source_snapshot"), feedback.source_digest)
            if snapshot is not None:
                private["source_snapshot"] = snapshot
            attempt.program_execution = private
    return result


def is_trusted_program_tool(tool) -> bool:
    """Identify the registered first-party implementation, never a name/contract.

    This also gates the controlled long-running execution policy. Plugin
    metadata, look-alike wrappers and arbitrary sandbox adapters cannot opt in.
    """
    from app.domain.services.tools.base import Tool
    from app.domain.services.tools.shell import ShellToolkit
    if (type(tool) is not Tool or type(getattr(tool, "toolkit", None)) is not ShellToolkit
            or tool.name != "program_run"
            or tool._tool is not ShellToolkit.program_run
            or not any(candidate is tool for candidate in tool.toolkit.tools)):
        return False
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
    from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox, WorkerAgentSandbox
    sandbox = tool.toolkit.sandbox
    if type(sandbox) is NodeBoundSandbox:
        sandbox = inspect.getattr_static(sandbox, "sandbox", None)
    if (type(sandbox) not in {DockerSandbox, WorkerAgentSandbox}
            or sandbox.supports_execution_receipts is not True):
        return False
    return True


def resolved_program_path(tool, call: dict) -> str | None:
    if (not isinstance(call, dict) or call.get("name") != "program_run"
            or not is_trusted_program_tool(tool)):
        return None
    args = call.get("args")
    path = args.get("script_path") if isinstance(args, dict) else None
    return path if isinstance(path, str) and path.startswith("/") and path.endswith(".py") else None


def trusted_program_execution_feedback(tool, call: dict, result, ledger) -> dict | None:
    """Use private launch-bound evidence, not the supplied public result."""
    from app.domain.services.execution_evidence import ToolExecutionLedger, shell_command_digest
    path = resolved_program_path(tool, call)
    if path is None or type(ledger) is not ToolExecutionLedger:
        return None
    arguments = call["args"]
    digest = shell_command_digest(arguments.get("exec_dir", ""),
                                  program_command(path, arguments.get("argv") or []))
    for attempt in reversed(list(ledger._attempts.values())):
        if attempt.tool_call_id != call.get("id"):
            continue
        # A reused call identifier must not fall back to an older successful
        # launch if the newest attempt is unknown or belongs to other inputs.
        feedback = attempt.program_execution
        if (not attempt.confirmed
                or attempt.command_digest != digest or attempt.sandbox_id != str(tool.toolkit.sandbox.id)
                or attempt.shell_id != arguments.get("id") or not feedback
                or attempt.receipt.state != "exited"
                or attempt.receipt.returncode != feedback["returncode"]
                or feedback["script_path"] != path):
            return None
        return {**feedback, "operation_id": attempt.operation_id}
    return None


def trusted_program_prelaunch_failure(tool, call: dict, ledger) -> dict | None:
    """Observe the newest equivalent launch, never forgive a later unknown.

    A failed HTTP response need not carry a receipt; independent reconciliation
    can establish not_started later. Only the private, launch-bound ledger is
    authoritative. The prior call ID lets the guard match its exact failure.
    """
    from app.domain.services.execution_evidence import ToolExecutionLedger, shell_command_digest
    path = resolved_program_path(tool, call)
    if path is None or type(ledger) is not ToolExecutionLedger:
        return None
    arguments = call["args"]
    digest = shell_command_digest(arguments.get("exec_dir", ""),
                                  program_command(path, arguments.get("argv") or []))
    for attempt in reversed(list(ledger._attempts.values())):
        if attempt.sandbox_id != str(tool.toolkit.sandbox.id) or attempt.command_digest != digest:
            continue
        if not attempt.confirmed or attempt.receipt.state != "not_started":
            return None
        return {"script_path": path, "tool_call_id": attempt.tool_call_id,
                "operation_id": attempt.operation_id}
    return None


async def trusted_program_prerequisites(tool, call: dict) -> dict | None:
    """Read stable program and working-directory prerequisites privately.

    This is dependency freshness, not dataset evidence or permission to replay.
    The probe rejects links/races and includes real source bytes plus directory
    readiness, not mtimes or cosmetic call changes. Never infer freshness from
    a truncated file_read, a successful mkdir, or model-supplied metadata.
    """
    from app.domain.models.tool_result import ToolResult
    path = resolved_program_path(tool, call)
    if path is None:
        return None
    exec_dir = (call.get("args") or {}).get("exec_dir")
    for value in (path, exec_dir):
        if (not isinstance(value, str) or not value.startswith("/")
                or str(PurePosixPath(value)) != value or ".." in PurePosixPath(value).parts
                or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
    # program_run accepts saved scripts throughout the authorized sandbox,
    # not just final-deliverable output/. Match that launch contract here:
    # limiting the read-only probe to output/ makes a legitimate scripts/
    # creation permanently indistinguishable from an unchanged failed launch.
    # This probe grants no execute/write authority and never clears an unknown
    # operation; recovery still requires its private not_started receipt.
    try:
        async with asyncio.timeout(3):
            result = await tool.toolkit.sandbox.program_preflight(exec_dir, path)
        if not isinstance(result, ToolResult) or result.success is not True:
            return None
        data = result.data if isinstance(result.data, dict) else {}
        digest = data.get("prerequisite_digest")
        cwd, source = data.get("cwd"), data.get("source")
        if (type(data.get("version")) is not int or data["version"] != 1
                or data.get("status") not in {"ready", "blocked"}
                or type(data.get("ready")) is not bool
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(cwd, dict) or not isinstance(source, dict)):
            return None
        if (cwd.get("state") not in {"ready", "missing", "not_directory", "not_accessible"}
                or source.get("state") not in {"ready", "missing", "not_regular", "not_readable", "too_large"}):
            return None
        if source["state"] == "ready" and (not isinstance(source.get("source_digest"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", source["source_digest"])):
            return None
        ready = cwd["state"] == "ready" and source["state"] == "ready"
        if data["ready"] != ready or (data["status"] == "ready") != ready:
            return None
        return {"prerequisite_digest": digest, "ready": ready}
    except Exception:
        # Failure to observe freshness grants no reset. Cancellation propagates.
        return None
