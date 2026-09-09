"""One admission/accounting boundary for every physical model request.

The task ledger is context-local and shared by child asyncio tasks, including
JSON repair and tool-owned browser calls. It never queries a user's quota.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from app.core.config import get_settings
from app.domain.models.execution_environment import safe_public_identifier
from app.domain.models.model_trace import MemoryChange, ModelTraceRecord
from app.domain.services.context_budget import ContextBudgetExceeded, prepare_context
from app.domain.services.execution_identity import private_identity_hmac
from app.domain.services.token_usage_service import TokenUsageService

logger = logging.getLogger(__name__)
USAGE_RECORDED_KEY = "dataseek_model_usage_recorded"


class ModelBudgetStopped(asyncio.CancelledError):
    """End the dependent task; generic parser/Agent retries must not catch it."""

    def __init__(self, code: str = "task_token_budget_exceeded"):
        self.code = code
        scope = _SCOPE.get()
        if scope is not None:
            scope.ledger.stopped_code = code
        super().__init__("Model execution stopped at its configured runtime boundary")


class ModelTraceStore(Protocol):
    async def put(self, record: ModelTraceRecord) -> None: ...


@dataclass
class TaskTokenLedger:
    token_limit: int | None = None
    call_limit: int | None = None
    charged_tokens: int = 0
    calls: int = 0
    closed: bool = False
    stopped_code: str | None = None

    def reserve(self, tokens: int) -> int:
        if self.stopped_code:
            raise ModelBudgetStopped(self.stopped_code)
        if self.closed:
            raise ModelBudgetStopped("runtime_closed")
        if self.call_limit is not None and self.calls >= self.call_limit:
            raise ModelBudgetStopped("task_call_budget_exceeded")
        if self.token_limit is not None and self.charged_tokens + tokens > self.token_limit:
            raise ModelBudgetStopped("task_token_budget_exceeded")
        # No await: admission is atomic among tasks on this event loop.
        self.charged_tokens += tokens
        self.calls += 1
        return self.calls

    def settle(self, reserved: int, actual: int | None) -> None:
        # An error or missing usage may still represent a billed request.
        if actual is not None:
            self.charged_tokens += actual - reserved


@dataclass
class ModelExecutionScope:
    user_id: str
    session_id: str
    task_id: str
    ledger: TaskTokenLedger
    store: ModelTraceStore | None = None
    pending_changes: list[MemoryChange] = field(default_factory=list)
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    durable_budget: Any = None


_SCOPE: ContextVar[ModelExecutionScope | None] = ContextVar("model_execution_scope", default=None)
_ROLE: ContextVar[tuple[str, str | None]] = ContextVar("model_call_role", default=("auxiliary", None))
_ANALYSIS_BUDGET: ContextVar[Any] = ContextVar("analysis_budget", default=None)


def current_analysis_budget():
    """Original-request metering, independent of the Task's usage counters."""
    explicit = _ANALYSIS_BUDGET.get()
    scope = _SCOPE.get()
    return explicit if explicit is not None else scope.durable_budget if scope is not None else None


@contextmanager
def analysis_budget_scope(handle):
    """Attach per-input durable accounting without resetting Task counters."""
    token = _ANALYSIS_BUDGET.set(handle)
    try:
        yield handle
    finally:
        _ANALYSIS_BUDGET.reset(token)


def model_stop_reason() -> str | None:
    scope = _SCOPE.get()
    return scope.ledger.stopped_code if scope is not None else None


@contextmanager
def model_execution_scope(*, user_id: str, session_id: str, task_id: str, store=None,
                          token_limit: int | None = None, call_limit: int | None = None,
                          durable_budget=None):
    # Production does not consult the removed task-quota environment settings.
    # Explicit limits remain available only for isolated internal callers/tests.
    scope = ModelExecutionScope(user_id, session_id, task_id, TaskTokenLedger(
        token_limit, call_limit,
    ), store, durable_budget=durable_budget)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        scope.ledger.closed = True
        _SCOPE.reset(token)


@contextmanager
def model_call_role(role: str):
    token = _ROLE.set((safe_public_identifier(role)[:64], uuid4().hex))
    try:
        yield
    finally:
        _ROLE.reset(token)


def _message_payload(messages) -> list:
    # Artifacts and provider metadata are not part of a model request. Do not
    # include their potentially huge/private extra payloads even in a digest.
    return [{
        "type": getattr(message, "type", "unknown"),
        "name": getattr(message, "name", None),
        "content": getattr(message, "content", ""),
        "tool_calls": getattr(message, "tool_calls", None),
        "tool_call_id": getattr(message, "tool_call_id", None),
        "provider_fields": {key: value for key, value in (getattr(message, "additional_kwargs", None) or {}).items()
                            if key in {"function_call", "tool_calls", "reasoning_content", "audio"}},
    } for message in messages]


def _request_hmac(messages, tools, response_format) -> str:
    return private_identity_hmac({"purpose": "model-request/v1", "messages": _message_payload(messages),
                                  "tools": tools, "response_format": response_format})


def memory_checkpoint(messages) -> tuple[str, int, int] | None:
    if _SCOPE.get() is None:
        return None
    payload = _message_payload(messages)
    return (private_identity_hmac({"purpose": "memory-transformation/v1", "messages": payload}),
            len(payload), len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()))


def note_memory_change(before, messages, reason: str) -> None:
    scope = _SCOPE.get()
    if scope is None or before is None:
        return
    after = memory_checkpoint(messages)
    if after is None or before[0] == after[0]:
        return
    if len(scope.pending_changes) >= 256:
        raise ModelBudgetStopped("trace_store_unavailable")
    scope.pending_changes.append(MemoryChange(
        reason=reason, before_hmac=before[0], after_hmac=after[0],
        messages_before=before[1], messages_after=after[1], bytes_before=before[2], bytes_after=after[2],
    ))


async def _store(scope, record) -> None:
    if scope is None or scope.store is None:
        return
    try:
        async with asyncio.timeout(get_settings().model_trace_store_timeout_seconds):
            await scope.store.put(record)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("Model trace unavailable error_type=%s", type(error).__name__)
        raise ModelBudgetStopped("trace_store_unavailable") from None


async def flush_memory_changes() -> None:
    scope = _SCOPE.get()
    if scope is None:
        return
    async with scope.flush_lock:
        while scope.pending_changes:
            change = scope.pending_changes[0]
            role, logical_id = _ROLE.get()
            record = ModelTraceRecord(user_id=scope.user_id, session_id=scope.session_id, task_id=scope.task_id,
                kind="memory_change", status="recorded", role=role, logical_call_id=logical_id,
                memory_change=change, finished_at=datetime.now(UTC))
            await _store(scope, record)
            scope.pending_changes.pop(0)


def _usage(message) -> dict[str, int] | None:
    try:
        usage = TokenUsageService().extract_usage(message)
    except (ValueError, TypeError, OverflowError):
        return None
    if not usage or any(isinstance(value, bool) or value < 0 for value in usage.values()):
        return None
    # Provider totals must not erase separately reported input/output tokens.
    usage["total_tokens"] = max(usage["total_tokens"], usage["prompt_tokens"] + usage["completion_tokens"])
    return usage


async def invoke_model_request(*, messages, tool_schemas=(), response_format=None, max_output_tokens: int,
                               provider: str, model_name: str, driver_version: str = "langchain-driver/v1",
                               invoke: Callable[[list, int], Awaitable[Any]]):
    settings = get_settings()
    scope = _SCOPE.get()
    durable_budget = current_analysis_budget()
    durable_reservation_id = None
    role, logical_id = _ROLE.get()
    phase_started = time.perf_counter()
    await flush_memory_changes()
    memory_flush_ms = (time.perf_counter() - phase_started) * 1000
    trace = ModelTraceRecord(user_id=scope.user_id if scope else "", session_id=scope.session_id if scope else "",
        task_id=scope.task_id if scope else "standalone", role=role, logical_call_id=logical_id,
        provider=safe_public_identifier(provider), model_name=safe_public_identifier(model_name),
        driver_version=safe_public_identifier(driver_version)[:64], reserved_output_tokens=max_output_tokens,
        task_token_limit=scope.ledger.token_limit if scope else None, task_call_limit=scope.ledger.call_limit if scope else None)
    trace.timings.memory_flush_ms = memory_flush_ms
    try:
        phase_started = time.perf_counter()
        try:
            prepared = prepare_context(messages, tool_schemas=tool_schemas, response_format=response_format,
                capacity_tokens=settings.model_context_capacity_tokens, max_output_tokens=max_output_tokens,
                safety_tokens=settings.model_context_safety_tokens)
        finally:
            trace.timings.context_prepare_ms = (time.perf_counter() - phase_started) * 1000
        trace.input_tokens_before = prepared.input_tokens_before
        trace.input_tokens_after = prepared.input_tokens_after
        trace.tool_tokens = prepared.tool_tokens
        trace.input_limit = prepared.input_limit
        trace.compactions = list(prepared.records)
        trace.estimator_version = prepared.estimator_version
        if scope is not None:
            phase_started = time.perf_counter()
            trace.request_hmac_before = _request_hmac(messages, tool_schemas, response_format)
            trace.request_hmac_after = _request_hmac(prepared.messages, tool_schemas, response_format)
            trace.timings.request_identity_ms = (time.perf_counter() - phase_started) * 1000
            reserved = prepared.input_tokens_after + max_output_tokens
            trace.call_index = scope.ledger.reserve(reserved)
            trace.task_tokens_charged = scope.ledger.charged_tokens
        if durable_budget is not None:
            from app.domain.services.analysis_budget import BudgetUnavailableError
            if scope is not None and (durable_budget.user_id != scope.user_id or durable_budget.session_id != scope.session_id):
                raise ModelBudgetStopped("trace_store_unavailable")
            try:
                admission = await durable_budget.reserve_model_request(
                    prepared.input_tokens_after + max_output_tokens, reservation_id=trace.trace_id)
            except BudgetUnavailableError:
                raise ModelBudgetStopped("trace_store_unavailable") from None
            if not admission.allowed:
                code = admission.reason if admission.reason in {"task_call_budget_exceeded", "task_token_budget_exceeded", "analysis_budget_deadline_exceeded"} else "trace_store_unavailable"
                raise ModelBudgetStopped(code)
            durable_reservation_id = admission.reservation_id
    except (ContextBudgetExceeded, ModelBudgetStopped) as error:
        trace.status = "budget_exceeded"
        trace.error_code = getattr(error, "code", "context_budget_exceeded")
        trace.finished_at = datetime.now(UTC)
        await _store(scope, trace)
        if scope is not None:
            raise ModelBudgetStopped(trace.error_code) from None
        raise
    phase_started = time.perf_counter()
    await _store(scope, trace)  # Never send a changed prompt before its audit commits.
    trace.timings.admission_store_ms = (time.perf_counter() - phase_started) * 1000
    phase_started = time.perf_counter()
    try:
        if durable_budget is not None and durable_budget.require_live is not None:
            await durable_budget.require_live()
        if durable_budget is not None and admission.deadline_at is not None:
            remaining = (admission.deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise ModelBudgetStopped("analysis_budget_deadline_exceeded")
            deadline_window = asyncio.timeout(remaining)
            try:
                async with deadline_window:
                    message = await invoke(prepared.messages, max_output_tokens)
            except TimeoutError:
                if deadline_window.expired():
                    raise ModelBudgetStopped("analysis_budget_deadline_exceeded") from None
                raise
        else:
            message = await invoke(prepared.messages, max_output_tokens)
    except BaseException as error:
        trace.timings.provider_call_ms = (time.perf_counter() - phase_started) * 1000
        trace.status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        trace.error_code = (error.code if isinstance(error, ModelBudgetStopped) else
                            "cancelled" if isinstance(error, asyncio.CancelledError) else "provider_error")
        trace.usage_source = "reservation"
        trace.finished_at = datetime.now(UTC)
        await _store(scope, trace)
        raise
    trace.timings.provider_call_ms = (time.perf_counter() - phase_started) * 1000
    phase_started = time.perf_counter()
    usage = _usage(message)
    if scope is not None:
        scope.ledger.settle(reserved, usage["total_tokens"] if usage else None)
        trace.task_tokens_charged = scope.ledger.charged_tokens
    durable_settlement_failed = False
    if durable_budget is not None and durable_reservation_id is not None:
        from app.domain.services.analysis_budget import BudgetUnavailableError
        try:
            await durable_budget.settle_model_request(durable_reservation_id, usage["total_tokens"] if usage else None)
        except BudgetUnavailableError:
            # The response already exists: preserve conservative reservation and
            # retain its actual-usage trace before stopping. Never retry a
            # potentially billed physical request to repair accounting.
            durable_settlement_failed = True
    if usage:
        trace.actual_input_tokens = usage["prompt_tokens"]
        trace.actual_output_tokens = usage["completion_tokens"]
        trace.actual_total_tokens = usage["total_tokens"]
        trace.usage_source = "provider"
    else:
        trace.usage_source = "reservation"
    trace.timings.usage_settlement_ms = (time.perf_counter() - phase_started) * 1000
    trace.status = "succeeded"
    trace.finished_at = datetime.now(UTC)
    await _store(scope, trace)
    if durable_settlement_failed:
        raise ModelBudgetStopped("trace_store_unavailable") from None
    if scope is not None and usage:
        try:
            async with asyncio.timeout(settings.model_trace_store_timeout_seconds):
                await TokenUsageService().record_from_message(message, user_id=scope.user_id, session_id=scope.session_id,
                    task_id=scope.task_id, model_provider=provider, model_name=model_name)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The trace above already durably records provider usage. Legacy
            # aggregate reporting must not hold up delivery or double count it.
            logger.warning("Model usage reporting unavailable error_type=%s", type(error).__name__)
        message.additional_kwargs[USAGE_RECORDED_KEY] = True
    logger.info("model_request role=%s provider=%s input_estimate=%d output_reserve=%d compacted=%d usage_source=%s",
                role, trace.provider, trace.input_tokens_after, max_output_tokens, len(trace.compactions), trace.usage_source)
    return message
