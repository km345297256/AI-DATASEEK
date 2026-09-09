"""Private host-observed execution receipts; completion is not replay permission.

Only trusted shell adapters register operations or observe protocol responses.
Neither model messages nor arbitrary plugin result dictionaries enter this
ledger. Public summaries contain counts and state only, never operation tokens.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import math
import posixpath
import time
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models.tool_result import ToolResult


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: int = Field(ge=1, le=1)
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    server_instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: Literal["starting", "running", "exited", "not_started", "unknown"]
    returncode: int | None
    process_tree_quiescent: bool

    @model_validator(mode="after")
    def consistent_terminal_state(self):
        if self.state == "exited" and self.returncode is None:
            raise ValueError("Exited execution requires an exit code")
        if self.state != "exited" and self.returncode is not None:
            raise ValueError("Non-exited execution cannot claim an exit code")
        if self.process_tree_quiescent and self.state not in {"exited", "not_started"}:
            raise ValueError("Only a confirmed terminal state can be quiescent")
        return self


def shell_command_digest(exec_dir: str, command: str) -> str:
    normalized_dir = posixpath.normpath(exec_dir) if exec_dir.startswith("/") else exec_dir
    payload = json.dumps({"command": command, "exec_dir": normalized_dir}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ShellExecutionAttempt:
    tool_call_id: str
    operation_id: str
    sandbox_id: str
    shell_id: str
    command_digest: str
    query: Callable[[], Awaitable[ToolResult]] = field(repr=False)
    receipt: ExecutionReceipt | None = None
    reconciliation_queries: int = 0
    observer_call_ids: set[str] = field(default_factory=set, repr=False)
    observed_at: float = field(default=0, repr=False)
    consecutive_query_failures: int = 0
    observation_block_reason: str | None = None
    query_in_flight: bool = field(default=False, repr=False)
    last_query_at: float = field(default=0, repr=False)

    def belongs_to_call(self, tool_call_id: str) -> bool:
        return self.tool_call_id == tool_call_id or tool_call_id in self.observer_call_ids

    def observe(self, value: Any) -> bool:
        try:
            receipt = ExecutionReceipt.model_validate(value)
        except (ValueError, TypeError):
            if not self.confirmed:
                self.observation_block_reason = "execution_receipt_invalid"
            return False
        if receipt.operation_id != self.operation_id or receipt.command_digest != self.command_digest:
            if not self.confirmed:
                self.observation_block_reason = "execution_identity_lost"
            return False
        previous = self.receipt
        if previous is not None and receipt.server_instance_id != previous.server_instance_id:
            if not self.confirmed:
                self.observation_block_reason = "execution_identity_lost"
            return False
        # Terminal proof is immutable for one attempt, never replaced by a
        # stale running response or by a contradictory exit code.
        if previous is not None and previous.process_tree_quiescent:
            return receipt == previous
        if self.observation_block_reason is not None:
            return False
        if previous is not None and previous.state == "exited" and (
            receipt.state != "exited" or receipt.returncode != previous.returncode
        ):
            self.observation_block_reason = "execution_receipt_invalid"
            return False
        self.receipt = receipt
        self.observed_at = time.monotonic()
        self.consecutive_query_failures = 0
        if receipt.state == "unknown":
            self.observation_block_reason = "execution_state_unknown"
        return True

    @property
    def confirmed(self) -> bool:
        return bool(self.receipt and self.receipt.state in {"exited", "not_started"}
                    and self.receipt.process_tree_quiescent)

    @property
    def replay_safe(self) -> bool:
        return bool(self.confirmed and self.receipt.state == "not_started")


class ToolExecutionLedger:
    MAX_PENDING_OPERATIONS = 128
    MAX_CONSECUTIVE_QUERY_FAILURES = 2

    def __init__(self):
        self._attempts: dict[str, ShellExecutionAttempt] = {}
        self._unknown_calls: set[str] = set()
        self._nonreplayable_calls: set[str] = set()

    def register(self, attempt: ShellExecutionAttempt) -> None:
        if (sum(not previous.confirmed for previous in self._attempts.values()) >= self.MAX_PENDING_OPERATIONS
            or attempt.operation_id in self._attempts):
            from app.domain.services.tools.tool_contract import ToolContractError
            raise ToolContractError([], code="execution_evidence_capacity")
        self._attempts[attempt.operation_id] = attempt

    def has_attempts(self, tool_call_id: str) -> bool:
        return any(attempt.belongs_to_call(tool_call_id) for attempt in self._attempts.values())

    def record_unknown(self, tool_call_id: str) -> None:
        if not self.has_attempts(tool_call_id):
            self._unknown_calls.add(tool_call_id)

    def record_nonreplayable(self, tool_call_id: str) -> None:
        """A completed operation without a replay guarantee is not pending.

        In particular, a successful external/MCP write with no shell receipt
        must not gain replay permission from all(empty_attempts). Host callers
        record this fact; tool-result claims can never clear it.
        """
        self._nonreplayable_calls.add(tool_call_id)

    def _summary(self, tool_call_id: str | None = None) -> dict[str, Any]:
        attempts = [attempt for attempt in self._attempts.values()
                    if tool_call_id is None or attempt.belongs_to_call(tool_call_id)]
        unknown = self._unknown_calls if tool_call_id is None else self._unknown_calls & {tool_call_id}
        nonreplayable = (self._nonreplayable_calls if tool_call_id is None
                         else self._nonreplayable_calls & {tool_call_id})
        pending = {call_id for attempt in attempts if not attempt.confirmed
                   for call_id in ({attempt.tool_call_id} | attempt.observer_call_ids)
                   if tool_call_id is None or call_id == tool_call_id} | unknown
        unresolved = [attempt for attempt in attempts if not attempt.confirmed]
        blocked = [attempt for attempt in unresolved if not _can_observe(attempt)]
        observable = any(_can_observe(attempt) for attempt in unresolved)
        reason = ("opaque_execution_unknown" if unknown else
                  _observation_failure_reason(blocked[0]) if blocked else
                  "execution_observable" if observable else "none")
        return {"pending_execution": bool(pending), "execution_confirmed": not pending,
                "replay_safe": not unknown and not nonreplayable and all(attempt.replay_safe for attempt in attempts),
                "tracked_operation_count": len(attempts),
                "confirmed_failed_operation_count": sum(bool(attempt.confirmed and attempt.receipt.state == "exited"
                    and attempt.receipt.returncode != 0) for attempt in attempts),
                "unresolved_call_count": len(pending),
                "nonreplayable_call_count": len(nonreplayable),
                "has_observable_pending": observable,
                "has_unresolvable_pending": bool(unknown or blocked),
                "reason": reason}

    def call_summary(self, tool_call_id: str) -> dict[str, Any]:
        return self._summary(tool_call_id)

    def summary(self) -> dict[str, Any]:
        return self._summary()

    def failure_state(self, tool_call_id: str, fallback: str = "unknown") -> str:
        if not self.has_attempts(tool_call_id):
            return "unknown" if tool_call_id in self._unknown_calls else fallback
        summary = self.call_summary(tool_call_id)
        if summary["pending_execution"]:
            return "unknown"
        return "not_started" if summary["replay_safe"] else "confirmed_terminal"

    async def reconcile_pending(self, *, max_operations: int = 4, timeout_seconds: float = 3,
                                phase: Literal["intermediate", "final"] = "intermediate") -> dict[str, Any]:
        # Hard local ceilings apply even when a caller supplies a larger value.
        limit = max(0, min(max_operations, 8)) if type(max_operations) is int else 0
        duration = (max(0.05, min(timeout_seconds, 5))
                    if type(timeout_seconds) in {int, float} and math.isfinite(timeout_seconds) else 3)
        now = time.monotonic()
        selected = [attempt for attempt in sorted(self._attempts.values(), key=lambda item: item.last_query_at)
                    if _can_observe(attempt) and not attempt.query_in_flight
                    # Avoid immediately polling a fresh receipt. This is a
                    # cadence guard, not a cumulative observation allowance.
                    and not (phase != "final" and attempt.receipt is not None
                             and now - attempt.observed_at < 0.25)][:limit]
        attempted, changed = await _bounded_observation_batch(selected, duration)
        return {**self.summary(), "attempted_count": attempted, "changed_count": changed}


def _can_observe(attempt: ShellExecutionAttempt) -> bool:
    return (not attempt.confirmed and attempt.observation_block_reason is None
            and attempt.consecutive_query_failures < ToolExecutionLedger.MAX_CONSECUTIVE_QUERY_FAILURES)


def _observation_failure_reason(attempt: ShellExecutionAttempt) -> str:
    return attempt.observation_block_reason or "execution_status_query_failed"


async def _bounded_observation_batch(
    attempts: list[ShellExecutionAttempt], duration: float,
) -> tuple[int, int]:
    """Observe only original operations, with a shared deadline and no replay.

    Fetch tasks never apply evidence themselves. A late result after timeout
    cannot silently revive an operation or mutate a report already returned.
    """
    attempted = changed = 0
    tasks: dict[asyncio.Task, ShellExecutionAttempt] = {}
    deadline = asyncio.get_running_loop().time() + duration

    async def fetch(attempt: ShellExecutionAttempt):
        nonlocal attempted
        attempted += 1
        attempt.reconciliation_queries += 1
        attempt.last_query_at = time.monotonic()
        return await attempt.query()

    def discard_result(task: asyncio.Task):
        if not task.cancelled():
            task.exception()

    try:
        for attempt in attempts:
            if not _can_observe(attempt) or attempt.query_in_flight:
                continue
            attempt.query_in_flight = True
            tasks[asyncio.create_task(fetch(attempt))] = attempt
        if not tasks:
            return 0, 0
        # Keep a small part of the same hard deadline for cancellation cleanup.
        done, pending = await asyncio.wait(tasks, timeout=max(0, duration - min(0.05, duration / 5)))
        for task in done:
            attempt = tasks[task]
            previous = attempt.receipt
            try:
                result = task.result()
                accepted = (isinstance(result, ToolResult) and result.success is True
                            and attempt.observe(result.data))
            except Exception:
                accepted = False
            if accepted:
                changed += int(attempt.receipt != previous)
            else:
                attempt.consecutive_query_failures += 1
        for task in pending:
            tasks[task].consecutive_query_failures += 1
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()))
            for task in pending:
                if not task.done():
                    tasks[task].observation_block_reason = "execution_status_query_unresponsive"
        return attempted, changed
    finally:
        for task, attempt in tasks.items():
            if not task.done():
                task.cancel()
                # An externally cancelled batch must not allow another query
                # to overlap cleanup of this same operation.
                task.add_done_callback(lambda _task, item=attempt: setattr(item, "query_in_flight", False))
            else:
                attempt.query_in_flight = False
            task.add_done_callback(discard_result)


_SCOPE: ContextVar[tuple[ToolExecutionLedger, str] | None] = ContextVar("trusted_execution_scope", default=None)


@contextmanager
def tool_execution_scope(ledger: ToolExecutionLedger, tool_call_id: str):
    token = _SCOPE.set((ledger, tool_call_id))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def register_shell_attempt(sandbox: Any, shell_id: str, exec_dir: str, command: str) -> ShellExecutionAttempt | None:
    scope = _SCOPE.get()
    if (scope is None or getattr(sandbox, "supports_execution_receipts", False) is not True
            or not callable(getattr(sandbox, "exec_command_tracked", None))
            or not callable(getattr(sandbox, "shell_operation_status", None))):
        return None
    ledger, call_id = scope
    operation_id = uuid4().hex
    attempt = ShellExecutionAttempt(tool_call_id=call_id, operation_id=operation_id,
        sandbox_id=str(sandbox.id), shell_id=shell_id, command_digest=shell_command_digest(exec_dir, command),
        query=lambda: sandbox.shell_operation_status(shell_id, operation_id))
    ledger.register(attempt)
    return attempt


def current_shell_attempt(sandbox: Any, shell_id: str) -> ShellExecutionAttempt | None:
    scope = _SCOPE.get()
    if scope is None:
        return None
    ledger, call_id = scope
    return next((attempt for attempt in reversed(list(ledger._attempts.values()))
                 if attempt.belongs_to_call(call_id) and attempt.sandbox_id == str(sandbox.id)
                 and attempt.shell_id == shell_id), None)


def bind_shell_observation(sandbox: Any, shell_id: str) -> ShellExecutionAttempt | None:
    """Bind a later wait/view to our expected original operation, not a new run.

    The adapter must send this operation ID to the server, which rejects a
    replaced shell generation. A shell ID or returned exit code alone is never
    terminal evidence. No receipt means the observing call stays unresolved.
    """
    scope = _SCOPE.get()
    if scope is None or getattr(sandbox, "supports_execution_receipts", False) is not True:
        return None
    ledger, call_id = scope
    operation = current_shell_attempt(sandbox, shell_id)
    if operation is None:
        operation = next((attempt for attempt in reversed(list(ledger._attempts.values()))
                          if attempt.sandbox_id == str(sandbox.id) and attempt.shell_id == shell_id), None)
    if operation is not None:
        operation.observer_call_ids.add(call_id)
    return operation


async def refresh_shell_attempt(attempt: ShellExecutionAttempt | None) -> None:
    if attempt is None or not _can_observe(attempt):
        return
    await _bounded_observation_batch([attempt], 2)


def consume_shell_receipt(result: ToolResult, attempt: ShellExecutionAttempt | None) -> ToolResult:
    """Consume a trusted adapter response before exposing ordinary tool data."""
    if not isinstance(result.data, dict) or "execution_receipt" not in result.data:
        return result
    data = dict(result.data)
    receipt = data.pop("execution_receipt")
    if attempt is not None:
        attempt.observe(receipt)
    return result.model_copy(update={"data": data})
