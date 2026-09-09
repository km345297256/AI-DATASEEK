"""Bounded, host-owned loop detection; no model may attest its own progress.

This guard never executes code. It stops redundant calls before dispatch and
gives the model one concrete way to change course within its existing budget.
Only execution evidence or new verified read evidence clears a stalled loop.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Any

from app.domain.services.execution_identity import private_identity_hmac


CODE_SUFFIXES = frozenset({".py", ".r", ".js", ".ts", ".sh", ".ipynb"})


def call_identity(call: dict) -> str:
    # Ephemeral shell identifiers and timeouts are not a change in work.
    args = {key: value for key, value in (call.get("args") or {}).items()
            if key not in {"id", "timeout", "timeout_seconds"}}
    return private_identity_hmac({"purpose": "analysis-progress-call/v1",
                                  "name": call.get("name"), "args": args})


class AnalysisProgressGuard:
    MAX_RECORDS = 64

    def __init__(self) -> None:
        self.failures: OrderedDict[str, int] = OrderedDict()
        self.reads: OrderedDict[str, int] = OrderedDict()
        self.writes: OrderedDict[str, int] = OrderedDict()
        self.evidence: set[str] = set()
        self.read_observations: set[str] = set()
        self.read_scope_paths: frozenset[str] | None = None
        self.unsafe_failed_calls: set[str] = set()
        self.stalled = False
        self.corrections = 0
        self.last_notice: str | None = None
        self.blocked_without_progress = 0

    def record_blocked(self, call: dict, reason: str) -> None:
        """Track dispatches that did no work; they cannot spin until a quota.

        This is a consecutive no-progress condition, not a task-wide limit.
        Concrete new evidence or a valid observation clears it.
        """
        self._increment(self.failures, call_identity(call))
        self.blocked_without_progress += 1
        self.stalled = True
        self.last_notice = reason

    @property
    def should_stop(self) -> bool:
        return self.blocked_without_progress >= 2

    def record_observation(self) -> None:
        """A trusted original-operation observation is legitimate work."""
        self.blocked_without_progress = 0
        self.stalled = False

    @staticmethod
    def _code_target(call: dict) -> str | None:
        args = call.get("args") or {}
        path = args.get("file")
        if (call.get("name") != "file_write" or args.get("append") is True
                or not isinstance(path, str) or PurePosixPath(path).suffix.lower() not in CODE_SUFFIXES):
            return None
        return private_identity_hmac({"purpose": "analysis-code-target/v1", "path": path})

    def _increment(self, records: OrderedDict, key: str) -> None:
        records[key] = records.get(key, 0) + 1
        records.move_to_end(key)
        while len(records) > self.MAX_RECORDS:
            records.popitem(last=False)

    def before_call(self, call: dict, *, record: bool = True) -> str | None:
        key = call_identity(call)
        target = self._code_target(call)
        if target and self.writes.get(target, 0) >= 2:
            reason = (
                "The saved program has already been written twice without a confirmed execution. "
                "This additional rewrite was NOT executed. Inspect the existing program if needed, "
                "then execute it once with bounded tools and validate the required outputs. "
                "Only revise it again after concrete execution feedback; do not start another draft."
            )
        elif self.failures.get(key, 0) >= (1 if key in self.unsafe_failed_calls else 2):
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

    def record(self, call: dict, *, succeeded: bool, read_only: bool = False,
               result_digest: str | None = None, confirmed_execution: bool = False) -> None:
        key = call_identity(call)
        if succeeded and not read_only:
            # A successful content write (including core file_write without
            # a shell receipt) may change anything inspected earlier. Forget
            # read deduplication, not write/replay safety or drafting history.
            self.reads.clear()
        if confirmed_execution:
            # A failed but confirmed execution supplies concrete feedback for
            # a correction. It does not count as new evidence for a grant.
            self.writes.clear()
            # A real execution can change previously inspected outputs. Permit
            # a fresh read before deciding whether its evidence is unchanged.
            self.reads.clear()
            self.stalled = False
            self.blocked_without_progress = 0
        if not succeeded:
            self._increment(self.failures, key)
            if not read_only and len(self.unsafe_failed_calls) < self.MAX_RECORDS:
                self.unsafe_failed_calls.add(key)
            return
        self.failures.pop(key, None)
        target = self._code_target(call)
        if target:
            self._increment(self.writes, target)
        if read_only and result_digest:
            evidence = private_identity_hmac({"purpose": "analysis-read-evidence/v1",
                                             "call": key, "result": result_digest})
            if evidence not in self.read_observations:
                if len(self.read_observations) >= self.MAX_RECORDS:
                    self.read_observations.pop()
                self.read_observations.add(evidence)
                args = call.get("args") or {}
                if self.read_scope_paths is None or args.get("file") in self.read_scope_paths:
                    if len(self.evidence) >= self.MAX_RECORDS:
                        self.evidence.pop()
                    self.evidence.add(evidence)
                self.stalled = False
                self.blocked_without_progress = 0
                # A changed answer makes this a useful fresh read.
                self.reads.pop(key, None)
            self._increment(self.reads, key)

    def instruction(self) -> str:
        if any(count >= 2 for count in self.writes.values()):
            return ("PROGRESS CHECK: stop drafting the saved program. Execute the existing version "
                    "once within the task scope, inspect its exit result and validate actual deliverables. "
                    "A saved program is not evidence that a chart or table was produced.")
        return self.last_notice if self.stalled and self.last_notice else ""

    def evidence_digest(self) -> str:
        return private_identity_hmac({"purpose": "analysis-progress-evidence/v1",
                                      "evidence": sorted(self.evidence)})
