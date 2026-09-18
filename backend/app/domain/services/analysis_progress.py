"""Bounded, host-owned loop detection; no model may attest its own progress.

This guard never executes code. It stops redundant calls before dispatch and
gives the model one concrete way to change course within the authorized task.
Only execution evidence or new verified read evidence clears a stalled loop.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import posixpath
from pathlib import PurePosixPath
from app.domain.services.execution_identity import private_identity_hmac


CODE_SUFFIXES = frozenset({".py", ".r", ".js", ".ts", ".sh", ".ipynb"})
CODE_DIAGNOSTIC_ERRORS = frozenset({"SyntaxError", "IndentationError", "TabError", "NameError", "UnboundLocalError"})


@dataclass
class ProgramProgress:
    """Request-local private identities, not source or model progress claims."""

    revision: int = 0
    executed_revision: int = -1
    execution_evidence: str | None = None
    executed_source: str | None = None
    last_operation: str | None = None
    failures: dict[str, int] = field(default_factory=dict)
    diagnostic_required: bool = False
    direct_runner_supported: bool = False
    diagnostic_exception: str | None = None
    diagnostic_observations: set[str] = field(default_factory=set)
    confirmed_prerequisites: str | None = None
    prerequisites_ready: bool = False
    prerequisites_observed: bool = False


@dataclass
class ProgramLaunchFailure:
    """A failed invocation and its prerequisites, not an executed program error.

    Only a host-bound not-started receipt can make this failure recoverable.
    Its source snapshot belongs to the failed call, so a later receipt cannot
    retroactively erase an unconfirmed attempt with the same launch arguments.
    """

    target: str
    call_id: str
    prerequisites: str | None
    not_started_operation: str | None = None


def program_identity(path: str) -> str:
    return private_identity_hmac({"purpose": "analysis-code-target/v1", "path": posixpath.normpath(path)})


def call_identity(call: dict) -> str:
    # Ephemeral shell identifiers and timeouts are not a change in work.
    args = {key: value for key, value in (call.get("args") or {}).items()
            if key not in {"id", "timeout", "timeout_seconds"}}
    if call.get("name") == "program_run":
        # Omitted, null and empty argv all launch the same saved program.
        # Presentation spelling is not evidence that prerequisites changed.
        if args.get("argv") is None:
            args["argv"] = []
        for path_key in ("script_path", "exec_dir"):
            if isinstance(args.get(path_key), str):
                args[path_key] = posixpath.normpath(args[path_key])
    return private_identity_hmac({"purpose": "analysis-progress-call/v1",
                                  "name": call.get("name"), "args": args})


class AnalysisProgressGuard:
    MAX_RECORDS = 64

    def __init__(self) -> None:
        self.failures: OrderedDict[str, int] = OrderedDict()
        self.reads: OrderedDict[str, int] = OrderedDict()
        self.writes: OrderedDict[str, int] = OrderedDict()
        self.programs: OrderedDict[str, ProgramProgress] = OrderedDict()
        self.program_launch_failures: OrderedDict[str, ProgramLaunchFailure] = OrderedDict()
        self.evidence: set[str] = set()
        self.read_observations: set[str] = set()
        self.read_scope_paths: frozenset[str] | None = None
        self.unsafe_failed_calls: set[str] = set()
        self.stalled = False
        self.corrections = 0
        self.last_notice: str | None = None
        self.blocked_without_progress = 0
        self._blocked_targets: set[str | None] = set()

    def record_blocked(self, call: dict, reason: str, *, program_path: str | None = None) -> None:
        """Track dispatches that did no work; they cannot spin until a quota.

        This is a consecutive no-progress condition, not a task-wide limit.
        Concrete new evidence or a valid observation clears it.
        """
        self._increment(self.failures, call_identity(call))
        self.blocked_without_progress += 1
        self._blocked_targets.add(program_identity(program_path) if program_path else self._code_target(call))
        self.stalled = True
        self.last_notice = reason

    @property
    def should_stop(self) -> bool:
        return self.blocked_without_progress >= 2

    def record_observation(self) -> None:
        """A trusted original-operation observation is legitimate work."""
        self.blocked_without_progress = 0
        self._blocked_targets.clear()
        self.stalled = False

    @staticmethod
    def _code_target(call: dict) -> str | None:
        args = call.get("args") or {}
        path = args.get("file")
        if (call.get("name") not in {"file_write", "file_str_replace"}
                or not isinstance(path, str) or PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES):
            return None
        return program_identity(path)

    def _program(self, target: str) -> ProgramProgress:
        state = self.programs.setdefault(target, ProgramProgress())
        self.programs.move_to_end(target)
        while len(self.programs) > self.MAX_RECORDS:
            self.programs.popitem(last=False)
        return state

    def _increment(self, records: OrderedDict, key: str) -> None:
        records[key] = records.get(key, 0) + 1
        records.move_to_end(key)
        while len(records) > self.MAX_RECORDS:
            records.popitem(last=False)

    def before_call(self, call: dict, *, record: bool = True,
                    program_path: str | None = None) -> str | None:
        """program_path is supplied only for a resolved trusted program runner.

        A program launch is distinct from the raw invocation's argument hash:
        a corrected source version may legitimately use identical argv.
        """
        key = call_identity(call)
        target = program_identity(program_path) if program_path else self._code_target(call)
        state = self.programs.get(target) if target else None
        if state and state.diagnostic_required:
            reason = (
                "The same code error has recurred. This revision or execution was NOT dispatched. "
                "Use file_read to inspect the relevant lines of this failed program and choose a concrete "
                "correction. A new code-content observation allows one correction experiment; rereading "
                "the same content does not repeatedly reset this condition. Preserve verified outputs."
                if state.diagnostic_exception in CODE_DIAGNOSTIC_ERRORS else
                "The same program failure has recurred without new source evidence. "
                "This additional revision or execution was NOT dispatched. Use file_read to inspect the original input "
                "structure or relevant source sample and change the parsing/analysis strategy before "
                "trying again. Reading the generated program or changing a few "
                "characters is not new source evidence. Preserve already verified outputs."
            )
        elif (target and not program_path and call.get("name") == "file_write"
              and str((call.get("args") or {}).get("file", "")).endswith(".py")
              and (call.get("args") or {}).get("append") is not True
              and self.writes.get(target, 0) >= 2):
            reason = (
                "The saved program has already been written twice without a confirmed execution. "
                "This additional rewrite was NOT executed. Inspect the existing program if needed, "
                "then execute it once with program_run and validate the required outputs. "
                "Only revise it again after concrete execution feedback; do not start another draft."
            )
        elif (self.failures.get(key, 0) >= (1 if key in self.unsafe_failed_calls else 2)
              and not (program_path and self._prelaunch_prerequisite_changed(key, target))):
            reason = (
                "The same operation has already failed without new evidence justifying this repeat. This repeat was NOT executed. "
                "Use the confirmed error to change the inputs or use a supported alternative. "
                "Do not repeat a write whose execution state is unconfirmed."
            )
        elif self.reads.get(key, 0) >= 2:
            reason = (
                "The same read has already returned identical evidence twice. This repeat was NOT executed. "
                "Use that evidence to finish the requested analysis; inspect a different detail only if necessary."
            )
        else:
            return None
        if record:
            self.stalled = True
            self.corrections += 1
            self.last_notice = reason
        return reason

    @staticmethod
    def _call_instance(call: dict) -> str:
        return private_identity_hmac({"purpose": "program-progress-call-instance/v1", "id": call.get("id")})

    def _prelaunch_prerequisite_changed(self, key: str, target: str | None) -> bool:
        failure = self.program_launch_failures.get(key)
        state = self.programs.get(target) if target else None
        return bool(failure and failure.target == target and failure.not_started_operation
                    and failure.prerequisites is not None
                    and state and state.prerequisites_observed and state.prerequisites_ready
                    and state.confirmed_prerequisites is not None
                    and state.confirmed_prerequisites != failure.prerequisites)

    def record_program_prelaunch_failure(self, *, call: dict, path: str, operation_id: str) -> None:
        """Bind a reconciled, host-proven not-started receipt to its failed call.

        Receipt reconciliation may finish after a source file has been created.
        Keep the snapshot captured at failure time, not at reconciliation time.
        Neither a public error message nor a historical operation grants this
        permission; callers must authenticate the launch receipt first.
        """
        key = call_identity(call)
        failure = self.program_launch_failures.get(key)
        if (failure is None or failure.call_id != self._call_instance(call)
                or failure.target != program_identity(path)):
            return
        operation = private_identity_hmac({"purpose": "program-progress-operation/v1", "id": operation_id})
        if failure.not_started_operation not in (None, operation):
            return
        failure.not_started_operation = operation
        self._clear_recovered_prelaunch_block(key, failure.target)

    def record_program_prerequisites(self, *, path: str, prerequisite_digest: str, ready: bool = True) -> None:
        """Observe exact launch prerequisites through a trusted read-only probe.

        This is prerequisite repair, not scientific evidence or execution.
        A different but still-unusable working directory/source cannot permit
        replay. Unavailable probes must never call this method with invented
        identities. Timestamps, call IDs and other irrelevant noise cannot be
        part of the trusted prerequisite digest.
        """
        target = program_identity(path)
        state = self._program(target)
        state.confirmed_prerequisites = private_identity_hmac({"purpose": "program-progress-prerequisite/v1",
                                                              "content": prerequisite_digest})
        state.prerequisites_ready = ready is True
        state.prerequisites_observed = True
        for key, failure in self.program_launch_failures.items():
            if failure.target == target:
                self._clear_recovered_prelaunch_block(key, target)

    def record_program_source(self, *, path: str, content_digest: str) -> None:
        """Internal source-only convenience; production uses complete preflight."""
        self.record_program_prerequisites(path=path, prerequisite_digest=content_digest)

    def invalidate_program_prerequisites(self, *, path: str) -> None:
        """A failed fresh observation cannot borrow a prior ready snapshot."""
        state = self.programs.get(program_identity(path))
        if state:
            state.prerequisites_ready = False
            state.prerequisites_observed = False

    def _clear_recovered_prelaunch_block(self, key: str, target: str) -> None:
        if not self._prelaunch_prerequisite_changed(key, target):
            return
        if self._blocked_targets <= {target}:
            self.blocked_without_progress = 0
            self._blocked_targets.clear()
        if not self._blocked_targets and not any(item.diagnostic_required for item in self.programs.values()):
            self.stalled = False

    def record_program_execution(self, *, path: str, operation_id: str,
                                 source_digest: str, returncode: int,
                                 failure_fingerprint: str | None = None,
                                 call: dict | None = None, diagnostic: dict | None = None) -> None:
        """Consume host-validated direct-program evidence, never tool prose.

        Terminal shell state alone cannot attest which saved program ran. This
        method is called only after the trusted runner validates its receipt.
        Failure novelty/source observations allow correction; source churn alone
        does not turn a repeatedly identical failure into useful progress.
        """
        target = program_identity(path)
        state = self._program(target)
        state.direct_runner_supported = True
        operation = private_identity_hmac({"purpose": "program-progress-operation/v1", "id": operation_id})
        if state.last_operation == operation:
            return
        if call is not None:
            # Only this newly confirmed invocation graduates from raw failure
            # handling to version-bound program feedback. Historical receipts
            # must not forgive a later unreadable-source/prelaunch failure.
            key = call_identity(call)
            self.failures.pop(key, None)
            self.unsafe_failed_calls.discard(key)
            self.program_launch_failures.pop(key, None)
        state.last_operation = operation
        state.executed_revision = state.revision
        state.executed_source = private_identity_hmac({"purpose": "program-progress-source/v1", "digest": source_digest})
        evidence = self.evidence_digest()
        if state.execution_evidence != evidence:
            state.failures.clear()
        state.execution_evidence = evidence
        state.diagnostic_exception = diagnostic.get("exception_type") if isinstance(diagnostic, dict) else None
        self.writes.pop(target, None)
        self.reads.clear()  # The program may have changed derived outputs.
        if returncode == 0:
            state.failures.clear()
            state.diagnostic_required = False
            state.diagnostic_exception = None
        else:
            signature = private_identity_hmac({"purpose": "program-progress-failure/v1",
                "fingerprint": failure_fingerprint or f"exit:{returncode}"})
            state.failures[signature] = state.failures.get(signature, 0) + 1
            while len(state.failures) > self.MAX_RECORDS:
                state.failures.pop(next(iter(state.failures)))
            state.diagnostic_required = state.failures[signature] >= 2
        if state.diagnostic_required:
            self.stalled = True
            self.last_notice = (
                "PROGRESS CHECK: repeated program failure without new source evidence. "
                "Use file_read to inspect the original input structure or a representative failing record and change "
                "the parsing/analysis strategy. Do not keep patching and rerunning blindly."
            )
        else:
            self.stalled = False
            self.blocked_without_progress = 0
            self._blocked_targets.clear()

    def record_program_diagnostic(self, *, path: str, content_digest: str) -> None:
        """A narrow code-error experiment, not dataset/output progress.

        The digest must come from an identity-checked core file_read. It cannot
        relieve data-parse errors or reset any other program's diagnostic gate.
        """
        target = program_identity(path)
        state = self.programs.get(target)
        if not state or state.diagnostic_exception not in CODE_DIAGNOSTIC_ERRORS:
            return
        if content_digest in state.diagnostic_observations:
            return
        # Keep request-local opaque identities: evicting them would make an old
        # observation novel again, while a numeric ceiling would be a hidden
        # task quota. Source bytes never enter this set.
        state.diagnostic_observations.add(content_digest)
        if not state.diagnostic_required:
            return
        state.diagnostic_required = False
        if self._blocked_targets <= {target}:
            self.blocked_without_progress = 0
            self._blocked_targets.clear()
        if not self._blocked_targets and not any(item.diagnostic_required for item in self.programs.values()):
            self.stalled = False

    def record(self, call: dict, *, succeeded: bool, read_only: bool = False,
               result_digest: str | None = None, confirmed_execution: bool = False,
               program_path: str | None = None) -> None:
        key = call_identity(call)
        if succeeded and not read_only:
            # A successful content write (including core file_write without
            # a shell receipt) may change anything inspected earlier. Forget
            # read deduplication, not write/replay safety or drafting history.
            self.reads.clear()
        if confirmed_execution:
            # A shell may have run only `ls`, or hidden a failed child behind a
            # pipeline. It can invalidate cached reads but cannot reset draft
            # history or attest progress for any saved program.
            self.reads.clear()
        if not succeeded:
            self._increment(self.failures, key)
            if not read_only and len(self.unsafe_failed_calls) < self.MAX_RECORDS:
                self.unsafe_failed_calls.add(key)
            self.program_launch_failures.pop(key, None)
            if program_path:
                target = program_identity(program_path)
                state = self._program(target)
                self.program_launch_failures[key] = ProgramLaunchFailure(
                    target=target, call_id=self._call_instance(call),
                    prerequisites=state.confirmed_prerequisites if state.prerequisites_observed else None,
                )
                self.program_launch_failures.move_to_end(key)
                while len(self.program_launch_failures) > self.MAX_RECORDS:
                    self.program_launch_failures.popitem(last=False)
            return
        self.failures.pop(key, None)
        self.program_launch_failures.pop(key, None)
        target = self._code_target(call)
        if target:
            state = self._program(target)
            state.revision += 1
            state.direct_runner_supported = str((call.get("args") or {}).get("file", "")).endswith(".py")
            if call.get("name") == "file_write" and (call.get("args") or {}).get("append") is not True:
                self._increment(self.writes, target)
        if read_only and result_digest:
            evidence = private_identity_hmac({"purpose": "analysis-read-evidence/v1",
                                             "call": key, "result": result_digest})
            if evidence not in self.read_observations:
                if len(self.read_observations) >= self.MAX_RECORDS:
                    self.read_observations.pop()
                self.read_observations.add(evidence)
                args = call.get("args") or {}
                path = args.get("file")
                generated_program = isinstance(path, str) and program_identity(path) in self.programs
                source_read = not generated_program and (
                    self.read_scope_paths is None or path in self.read_scope_paths
                )
                if source_read:
                    if len(self.evidence) >= self.MAX_RECORDS:
                        self.evidence.pop()
                    self.evidence.add(evidence)
                    self.stalled = False
                    self.blocked_without_progress = 0
                    self._blocked_targets.clear()
                    for state in self.programs.values():
                        state.diagnostic_required = False
                # A changed answer makes this a useful fresh read.
                self.reads.pop(key, None)
            self._increment(self.reads, key)

    def instruction(self) -> str:
        if any(state.diagnostic_required for state in self.programs.values()):
            return ("PROGRESS CHECK: the program has repeated the same failure without new source evidence. "
                    "Use file_read to inspect original input structure/a representative failing record and change strategy "
                    "before more revisions. For a confirmed syntax/name code error, inspect the exact failing program "
                    "lines with file_read for one correction experiment. Re-reading a generated script is not "
                    "source evidence and cannot relieve data-parsing failures.")
        if any(count >= 2 and self.programs.get(target) and self.programs[target].direct_runner_supported
               for target, count in self.writes.items()):
            return ("PROGRESS CHECK: stop drafting the saved program. Execute the existing version "
                    "once with program_run within the task scope, inspect its exit result and validate actual deliverables. "
                    "A saved program is not evidence that a chart or table was produced.")
        if any(count >= 2 for count in self.writes.values()):
            return ("PROGRESS CHECK: avoid repeated whole-program drafts. Run the saved program with its "
                    "supported language runtime, inspect concrete feedback, and validate actual deliverables.")
        return self.last_notice if self.stalled and self.last_notice else ""

    def evidence_digest(self) -> str:
        return private_identity_hmac({"purpose": "analysis-progress-evidence/v1",
                                      "evidence": sorted(self.evidence)})
