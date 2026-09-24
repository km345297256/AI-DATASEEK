"""Bounded, host-owned loop detection; no model may attest its own progress.

This guard never executes code. It stops redundant calls before dispatch and
gives the model one concrete way to change course within the authorized task.
Only execution evidence or new verified read evidence clears a stalled loop.
"""
from __future__ import annotations

import ast
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
    unsuccessful_executions: int = 0
    input_targets: frozenset[str] | None = None
    failure_identity: str | None = None
    failed_line: int | None = None
    failed_line_digest: str | None = None
    failed_full_read_digest: str | None = None
    failed_program_inspected: bool = False
    post_failure_inputs: set[str] = field(default_factory=set)
    joint_diagnostic_trials: set[str] = field(default_factory=set)


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
    # A diagnostic checkpoint, not a task-wide retry budget. Failures with
    # changing text/source versions still need an evidence-backed correction.
    DIAGNOSE_AFTER_FAILURES = 3

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
                "The program has repeated a code error or failed across several revisions. "
                "This revision or execution was NOT dispatched. "
                "Use file_read to inspect the relevant lines of this failed program and choose a concrete "
                "correction hypothesis and a minimal check before a full run. "
                "A new code-content observation allows one correction experiment; rereading "
                "the same content does not repeatedly reset this condition. Preserve verified outputs."
                if state.diagnostic_exception in CODE_DIAGNOSTIC_ERRORS else
                "The program has repeated the same failure or failed across several revisions without new source evidence. "
                "This additional revision or execution was NOT dispatched. Use file_read to inspect the original input "
                "structure or relevant source sample, state a concrete correction hypothesis, and validate "
                "a minimal sample before a full run. If this small input was already fully read, inspect it again "
                "AND inspect the confirmed failing program line with file_read for one joint diagnosis trial. "
                "Reading only an unrelated input or the generated program, or changing a few characters, "
                "is not new source evidence. Preserve already verified outputs."
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
                                 call: dict | None = None, diagnostic: dict | None = None,
                                 source_snapshot: dict | None = None) -> None:
        """Consume host-validated direct-program evidence, never tool prose.

        Terminal shell state alone cannot attest which saved program ran. This
        method is called only after the trusted runner validates its receipt.
        A new failure can justify a correction, but a series of changing errors
        is not by itself progress. Only successful execution or relevant read
        evidence can recover from a diagnosed failure cycle.
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
        state.execution_evidence = self.evidence_digest()
        state.failed_program_inspected = False
        state.post_failure_inputs.clear()
        state.failed_line = state.failed_line_digest = state.failed_full_read_digest = None
        inputs = self._program_inputs(source_snapshot, source_digest, call)
        if state.unsuccessful_executions == 0 or returncode == 0:
            state.input_targets = inputs or None
        elif inputs:
            # A later edit cannot make a known input unrelated or erase the
            # diagnostic state. These identities are hints for targeted reads,
            # never claims that a program read/analysed the referenced files.
            state.input_targets = (state.input_targets or frozenset()) | inputs
        state.diagnostic_exception = diagnostic.get("exception_type") if isinstance(diagnostic, dict) else None
        self.writes.pop(target, None)
        self.reads.clear()  # The program may have changed derived outputs.
        if returncode == 0:
            state.failures.clear()
            state.diagnostic_required = False
            state.diagnostic_exception = None
            state.unsuccessful_executions = 0
            state.failure_identity = None
        else:
            state.unsuccessful_executions += 1
            signature = private_identity_hmac({"purpose": "program-progress-failure/v1",
                "fingerprint": failure_fingerprint or f"exit:{returncode}"})
            state.failure_identity = signature
            self._bind_failed_diagnostic(state, source_snapshot, source_digest, diagnostic)
            state.failures[signature] = state.failures.get(signature, 0) + 1
            while len(state.failures) > self.MAX_RECORDS:
                state.failures.pop(next(iter(state.failures)))
            state.diagnostic_required = (state.failures[signature] >= 2
                                         or state.unsuccessful_executions >= self.DIAGNOSE_AFTER_FAILURES)
        if state.diagnostic_required:
            self.stalled = True
            self.last_notice = (
                "PROGRESS CHECK: repeated program failures without new source evidence, even if error text changes. "
                "Use file_read to inspect the original input structure or a representative failing record and change "
                "the parsing/analysis strategy. State the correction hypothesis and validate a minimal sample "
                "before the full run. For an already-read small input, inspect it and the confirmed failing "
                "program line together for one joint diagnosis trial. Do not keep patching and rerunning blindly."
            )
        else:
            self._blocked_targets.discard(target)
            if not self._blocked_targets:
                self.blocked_without_progress = 0
            self.stalled = bool(self._blocked_targets) or any(item.diagnostic_required for item in self.programs.values())

    def _program_inputs(self, snapshot: dict | None, source_digest: str,
                        call: dict | None) -> frozenset[str]:
        """Find scoped path references in immutable executed bytes/argv.

        Literal references narrow the diagnostic target; they do not attest
        reads or infer code semantics. Dynamic paths and unavailable snapshots
        remain unknown, so they retain the request-scope fallback. No source
        bytes, paths, or ASTs are stored in progress records.
        """
        if not self.read_scope_paths:
            return frozenset()
        from app.domain.services.program_execution import validated_source_snapshot
        validated = validated_source_snapshot(snapshot, source_digest)
        references = []
        if validated:
            try:
                tree = ast.parse(validated["content"])
                references.extend(node.value for node in ast.walk(tree)
                                  if isinstance(node, ast.Constant) and isinstance(node.value, str))
            except (SyntaxError, ValueError, RecursionError):
                pass  # Syntax diagnostics can inspect the failed source itself.
        arguments = (call or {}).get("args") or {}
        argv = arguments.get("argv")
        if isinstance(argv, list):
            references.extend(item for item in argv if isinstance(item, str))
        paths = {posixpath.normpath(value) for value in references if value.startswith("/")}
        # Do not make a broad root literal a universal diagnostic escape hatch.
        # A dataset/source directory is useful, but / and /home/ubuntu are not.
        return frozenset(program_identity(path) for path in self.read_scope_paths
                         if any(posixpath.normpath(path) == value or
                                len(PurePosixPath(value).parts) >= 4 and
                                posixpath.normpath(path).startswith(value.rstrip("/") + "/")
                                for value in paths))

    @staticmethod
    def _bind_failed_diagnostic(state: ProgramProgress, snapshot: dict | None, source_digest: str,
                               diagnostic: dict | None) -> None:
        from app.domain.services.program_execution import validated_source_snapshot
        validated = validated_source_snapshot(snapshot, source_digest)
        if not validated:
            return
        # file_read uses text-mode universal newlines. Keep only opaque content
        # identities, not a second copy of code or traceback text in the guard.
        content = validated["content"].replace("\r\n", "\n").replace("\r", "\n")
        state.failed_full_read_digest = private_identity_hmac({"purpose": "program-diagnostic-read/v1", "content": content})
        line = diagnostic.get("line") if isinstance(diagnostic, dict) else None
        lines = content.splitlines()
        if type(line) is int and 1 <= line <= len(lines):
            state.failed_line = line - 1
            state.failed_line_digest = private_identity_hmac({"purpose": "program-diagnostic-line/v1",
                                                              "content": lines[line - 1]})

    def _try_joint_diagnostic(self, target: str, state: ProgramProgress) -> None:
        """One paired inspection of a confirmed error and its bound input.

        Full small inputs need not magically contain new bytes after failure.
        Re-observation plus inspection of the failed program is a diagnosis,
        not scientific progress. An error/input bundle is consumable once,
        independent of revision, call IDs and line-range spelling.
        """
        if not (state.diagnostic_required and state.failure_identity and state.failed_program_inspected):
            return
        bundles = {private_identity_hmac({"purpose": "program-joint-diagnosis/v1",
                    "error": state.failure_identity, "input": identity}) for identity in state.post_failure_inputs}
        if not bundles - state.joint_diagnostic_trials:
            return
        state.joint_diagnostic_trials.update(bundles)
        state.diagnostic_required = False
        if self._blocked_targets <= {target}:
            self.blocked_without_progress = 0
            self._blocked_targets.clear()
        if not self._blocked_targets and not any(item.diagnostic_required for item in self.programs.values()):
            self.stalled = False

    def record_program_diagnostic(self, *, path: str, content_digest: str,
                                  line_observation: dict | None = None) -> None:
        """A narrow code-error experiment, not dataset/output progress.

        The digest must come from an identity-checked core file_read. A code
        read alone cannot relieve data-parse errors: those require the paired,
        post-failure original-input diagnosis above. Neither path resets any
        other program's diagnostic gate or confirms scientific progress.
        """
        target = program_identity(path)
        state = self.programs.get(target)
        if not state:
            return
        if state.diagnostic_required and state.failure_identity:
            full_read = state.failed_full_read_digest is not None and content_digest == state.failed_full_read_digest
            located_read = False
            if state.failed_line is not None and isinstance(line_observation, dict):
                start, lines = line_observation.get("start_line"), line_observation.get("line_digests")
                if (type(start) is int and start >= 0 and isinstance(lines, list)
                        and 0 <= state.failed_line - start < len(lines)):
                    located_read = lines[state.failed_line - start] == state.failed_line_digest
            if full_read or located_read:
                state.failed_program_inspected = True
                self._try_joint_diagnostic(target, state)
        if state.diagnostic_exception not in CODE_DIAGNOSTIC_ERRORS:
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
               program_path: str | None = None,
               read_content_digest: str | None = None) -> None:
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
            args = call.get("args") or {}
            path = args.get("file")
            source_target = program_identity(path) if isinstance(path, str) else None
            # Paired diagnosis admits a post-failure re-read of already known
            # original bytes, but only from a checked core content receipt and
            # only for the program whose executed snapshot references it.
            if (read_content_digest and isinstance(path, str) and self.read_scope_paths is not None
                    and path in self.read_scope_paths and source_target not in self.programs):
                for target, state in self.programs.items():
                    if (state.failure_identity and state.input_targets is not None
                            and source_target in state.input_targets):
                        state.post_failure_inputs.add(private_identity_hmac({"purpose": "program-diagnostic-input/v1",
                            "target": source_target, "content": read_content_digest}))
                        self._try_joint_diagnostic(target, state)
            evidence = private_identity_hmac({"purpose": "analysis-read-evidence/v1",
                                             "call": source_target if read_content_digest and source_target else key,
                                             "result": read_content_digest or result_digest})
            if evidence not in self.read_observations:
                if len(self.read_observations) >= self.MAX_RECORDS:
                    self.read_observations.pop()
                self.read_observations.add(evidence)
                generated_program = isinstance(path, str) and program_identity(path) in self.programs
                source_read = not generated_program and (
                    self.read_scope_paths is None or path in self.read_scope_paths
                )
                if source_read:
                    if len(self.evidence) >= self.MAX_RECORDS:
                        self.evidence.pop()
                    self.evidence.add(evidence)
                    recovered = set()
                    for target, state in self.programs.items():
                        if state.input_targets is None or source_target in state.input_targets:
                            # One evidence-backed experiment, not a reset of
                            # the failed execution history. Another failure
                            # after the checkpoint needs another diagnosis.
                            state.diagnostic_required = False
                            recovered.add(target)
                    if self._blocked_targets <= recovered or not self.programs:
                        self.blocked_without_progress = 0
                        self._blocked_targets.clear()
                    if not self._blocked_targets and not any(state.diagnostic_required for state in self.programs.values()):
                        self.stalled = False
                # A changed answer makes this a useful fresh read.
                self.reads.pop(key, None)
            self._increment(self.reads, key)

    def instruction(self) -> str:
        if any(state.diagnostic_required for state in self.programs.values()):
            return ("PROGRESS CHECK: the program has repeated the same failure or failed across several revisions "
                    "without new source evidence. "
                    "Use file_read to inspect original input structure/a representative failing record and change strategy "
                    "before more revisions. State a correction hypothesis, validate a minimal sample, then run the full analysis. "
                    "For a confirmed syntax/name code error, inspect the exact failing program "
                    "lines with file_read for one correction experiment. Re-reading a generated script is not "
                    "source evidence. If the original small input was already fully read, pair a post-failure "
                    "read of it with inspection of the confirmed failing program line for one joint diagnosis trial.")
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
