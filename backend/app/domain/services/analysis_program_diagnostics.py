"""Trusted, read-only saved-code diagnostics and scoped recovery planning.

Planning never grants read authority or counts as an observation. Its calls
must go through the ordinary registered-tool authorization/event pipeline;
only the checked results below may be consumed by the progress guard.
"""
from dataclasses import dataclass
import posixpath
from pathlib import PurePosixPath
from uuid import uuid4

from app.domain.models.tool_result import ToolResult
from app.domain.services.execution_identity import private_identity_hmac


@dataclass(frozen=True)
class ProgramDiagnosticPlan:
    tool_calls: list[dict]
    instruction: str


def _canonical_path(value) -> bool:
    return (isinstance(value, str) and value.startswith("/")
            and str(PurePosixPath(value)) == value
            and ".." not in PurePosixPath(value).parts and "\\" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


class ProgramDiagnosticPlanner:
    """Offer one host-selected inspection for each bound failure/input cause.

    This is neither an execution retry allowance nor a task quota. Identical
    causes cannot refresh themselves by changing call IDs or script wording;
    a different confirmed failure or different bound input can be inspected.
    Failed/denied reads are still attempted observations, never progress.
    """

    MAX_INPUT_SAMPLES = 2
    SOURCE_CONTEXT_LINES = 3

    def __init__(self) -> None:
        self._attempted: set[str] = set()

    def plan(self, *, program_tool, program_call: dict, ledger, progress,
             tools) -> ProgramDiagnosticPlan | None:
        from app.domain.services.analysis_progress import program_identity
        from app.domain.services.program_execution import (
            trusted_program_execution_feedback, validated_source_snapshot,
        )
        from app.domain.services.tools.base import Tool
        from app.domain.services.tools.file import FileToolkit

        feedback = trusted_program_execution_feedback(program_tool, program_call, None, ledger)
        if (not feedback or type(feedback.get("returncode")) is not int
                or feedback["returncode"] == 0 or ledger.summary()["pending_execution"]):
            return None
        path = feedback["script_path"]
        if not _canonical_path(path):
            return None
        target = program_identity(path)
        state = progress.programs.get(target)
        operation = private_identity_hmac({"purpose": "program-progress-operation/v1",
                                           "id": feedback["operation_id"]})
        if (state is None or not state.diagnostic_required or not state.failure_identity
                or state.last_operation != operation):
            return None
        snapshot = validated_source_snapshot(feedback.get("source_snapshot"), feedback["source_digest"])
        if snapshot is None:
            return None
        diagnostic = feedback.get("diagnostic")
        line = diagnostic.get("line") if isinstance(diagnostic, dict) else None
        if type(line) is not int or not 1 <= line <= len(snapshot["content"].splitlines()):
            return None

        # Name/metadata lookalikes, unregistered wrappers and a different
        # sandbox must never become a host-authorized diagnostic reader.
        candidates = [tool for tool in tools if getattr(tool, "name", None) == "file_read"]
        if len(candidates) != 1:
            return None
        reader = candidates[0]
        if (type(reader) is not Tool or type(getattr(reader, "toolkit", None)) is not FileToolkit
                or reader._tool is not FileToolkit.file_read
                or not any(candidate is reader for candidate in reader.toolkit.tools)
                or reader.toolkit.sandbox is not program_tool.toolkit.sandbox):
            return None

        # Bind only the current immutable launch, not the guard's accumulated
        # inputs from older script versions. Existing scope is not expanded.
        inputs = progress._program_inputs(snapshot, feedback["source_digest"], program_call)
        paths = sorted(path for path in (progress.read_scope_paths or ())
                       if _canonical_path(path) and program_identity(path) in inputs
                       and program_identity(path) not in progress.programs)
        if not paths:
            return None
        identity = private_identity_hmac({"purpose": "program-host-diagnosis/v1",
            "program": target, "failure": state.failure_identity,
            "inputs": [program_identity(path) for path in paths]})
        if identity in self._attempted:
            return None

        # No input line-range arguments: the existing file_read default uses
        # a bounded 10,000-character prefix, whereas arbitrary range reads may
        # materialize a complete input. Source ranges are only planned from a
        # validated <=24 KB execution snapshot and remain hash-checked later.
        arguments = [{"file": path, "sudo": False} for path in paths[:self.MAX_INPUT_SAMPLES]]
        arguments.append({"file": feedback["script_path"], "sudo": False,
                          "start_line": max(0, line - 1 - self.SOURCE_CONTEXT_LINES),
                          "end_line": line + self.SOURCE_CONTEXT_LINES})
        self._attempted.add(identity)
        calls = [{"name": "file_read", "id": f"host-diagnostic-{uuid4().hex}",
                  "type": "tool_call", "args": args} for args in arguments]
        return ProgramDiagnosticPlan(tool_calls=calls, instruction=(
            "HOST DIAGNOSTIC INSPECTION: read the bounded authorized input samples and the confirmed "
            "failing source lines below before deciding the correction. These are samples, not proof "
            "of the full dataset or a successful analysis. Check assumptions about shape, empty/null "
            "values, labels, types and uniqueness against actual observations; repair dependent accesses "
            "as well as the failing assertion. State one correction hypothesis, use the smallest justified "
            "edit, validate a representative sample, then run the saved program. Do not remove an assertion "
            "without addressing its cause or repeat the same read to gain another retry. If a read fails "
            "or is insufficient, use an authorized format-aware inspection; do not claim it succeeded."
        ))


def _trusted_read_content(tool, call: dict, result) -> str | None:
    if call.get("name") != "file_read":
        return None
    from app.domain.services.tools.base import Tool
    from app.domain.services.tools.file import FileToolkit
    if (type(tool) is not Tool or type(getattr(tool, "toolkit", None)) is not FileToolkit
            or tool._tool is not FileToolkit.file_read
            or not any(candidate is tool for candidate in tool.toolkit.tools)
            or getattr(result, "tool_call_id", None) != call.get("id")):
        return None
    artifact = getattr(result, "artifact", None)
    args = call.get("args") or {}
    path = args.get("file")
    if (not isinstance(artifact, ToolResult) or artifact.success is not True
            or getattr(result, "status", None) == "error"
            or not isinstance(path, str) or args.get("sudo") not in (None, False)):
        return None
    data = artifact.data if isinstance(artifact.data, dict) else {}
    content, observed_path = data.get("content"), data.get("file")
    if (not isinstance(content, str) or not content.strip() or not isinstance(observed_path, str)
            or posixpath.normpath(observed_path) != posixpath.normpath(path)):
        return None
    return content


def program_diagnostic_read_digest(tool, call: dict, result) -> str | None:
    content = _trusted_read_content(tool, call, result)
    if content is None:
        return None
    # Neither call IDs nor requested line ranges create novelty when the
    # returned contents are identical. No source content enters guard state.
    return private_identity_hmac({"purpose": "program-diagnostic-read/v1", "content": content})


def program_diagnostic_read_lines(tool, call: dict, result) -> dict | None:
    """Private content-only line identities from a checked core file_read.

    These enable a diagnosis of the exact failed line, never a claim that code
    executed or that an input/result is correct. Negative slicing and truncated
    reads cannot be placed reliably and provide no line-level proof.
    """
    content = _trusted_read_content(tool, call, result)
    if content is None or content.endswith("(truncated)"):
        return None
    args = call.get("args") or {}
    start, end = args.get("start_line"), args.get("end_line")
    if any(value is not None and (type(value) is not int or value < 0) for value in (start, end)):
        return None
    return {"start_line": start or 0, "line_digests": [
        private_identity_hmac({"purpose": "program-diagnostic-line/v1", "content": line})
        for line in content.splitlines()]}
