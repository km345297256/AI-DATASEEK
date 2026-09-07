from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import inspect
import json
import logging
import math
import re
import time
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol
import uuid

from app.domain.models.audit import AuditRiskLevel, AuditStatus
from app.domain.external.spill import SpillArtifactStore
from app.domain.models.spill import SpillArtifactOwner
from app.domain.services.tools.spill import SpillArtifactInterceptor
from app.domain.services.tools.pipeline import (
    ToolExecutionContext,
    ToolExecutionInterceptor,
    ToolExecutionNext,
    opaque_log_identifier,
    summarize_argument_keys,
)


logger = logging.getLogger(__name__)

KNOWN_TOOL_EFFECTS = frozenset({
    "sandbox_read",
    "sandbox_write",
    "network",
    "credential_use",
    "external_side_effect",
})

_TRACE_ATTRIBUTE_KEYS = frozenset({
    "agent_id",
    "catalog_revision",
    "plugin",
    "plugin_version",
    "session_id",
    "task_id",
    "toolkit",
    "user_id",
    "workspace_id",
})
_TRACE_IDENTIFIER_ATTRIBUTES = {
    "agent_id": "agent",
    "session_id": "session",
    "task_id": "task",
    "user_id": "user",
    "workspace_id": "workspace",
}
_TRACE_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_TRACE_TOOLKIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TRACE_ATTRIBUTE_PATTERNS = {
    "catalog_revision": re.compile(r"^(?:[0-9a-f]{64}|unavailable)$"),
    "plugin": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$"),
    "plugin_version": re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,159}$"),
    "toolkit": _TRACE_TOOLKIT_PATTERN,
}


class ToolPolicyDefault(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolPolicySnapshot:
    """Immutable authorization inputs pinned for one execution generation.

    The zero-argument form is deliberately deny-all. Existing deployments can
    opt into :meth:`for_registered_tools` while richer per-user permissions are
    introduced; that compatibility mode still pins exact tool names and rejects
    unknown effects instead of becoming a wildcard allow policy.
    """

    version: str = "tool-policy/v1"
    default_decision: ToolPolicyDefault = ToolPolicyDefault.DENY
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_effects: frozenset[str] = field(default_factory=frozenset)
    granted_permissions: frozenset[str] = field(default_factory=frozenset)
    allow_unclassified_effects: bool = False
    metadata: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        version = self.version.strip() if isinstance(self.version, str) else ""
        if not version:
            raise ValueError("tool policy version must not be empty")
        object.__setattr__(self, "version", version)
        try:
            default_decision = ToolPolicyDefault(self.default_decision)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid default tool policy decision") from error
        object.__setattr__(self, "default_decision", default_decision)

        allowed_tools = _normalized_identifiers(self.allowed_tools, "allowed_tools")
        denied_tools = _normalized_identifiers(self.denied_tools, "denied_tools")
        overlap = allowed_tools & denied_tools
        if overlap:
            raise ValueError("a tool cannot be both explicitly allowed and denied")
        object.__setattr__(self, "allowed_tools", allowed_tools)
        object.__setattr__(self, "denied_tools", denied_tools)
        object.__setattr__(
            self,
            "allowed_effects",
            _normalized_identifiers(self.allowed_effects, "allowed_effects"),
        )
        object.__setattr__(
            self,
            "granted_permissions",
            _normalized_identifiers(self.granted_permissions, "granted_permissions"),
        )
        policy_metadata: dict[str, str | int | float | bool] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("tool policy metadata keys must be non-empty strings")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError("tool policy metadata values must be scalar")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("tool policy metadata numbers must be finite")
            policy_metadata[key] = value
        object.__setattr__(self, "metadata", MappingProxyType(policy_metadata))

    @classmethod
    def deny_all(cls, *, version: str = "tool-policy/deny-all-v1") -> "ToolPolicySnapshot":
        return cls(version=version)

    @classmethod
    def for_registered_tools(
        cls,
        tool_names: Iterable[str],
        *,
        version: str = "tool-policy/registered-tools-v1",
        granted_permissions: Iterable[str] = (),
        metadata: Mapping[str, str | int | float | bool] | None = None,
    ) -> "ToolPolicySnapshot":
        """Build the explicit compatibility policy for today's tool registry."""
        return cls(
            version=version,
            allowed_tools=frozenset(tool_names),
            allowed_effects=KNOWN_TOOL_EFFECTS,
            granted_permissions=frozenset(granted_permissions),
            allow_unclassified_effects=True,
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str
    rule_id: str


class ToolExecutionDeniedError(PermissionError):
    """Stable, value-free policy rejection surfaced through the current loop."""

    code = "tool_execution_denied"
    retryable = False

    def __init__(self, *, reason: str, rule_id: str):
        self.reason = reason
        self.rule_id = rule_id
        super().__init__(f"{self.code}: {reason} ({rule_id})")


class ToolExecutionTimeoutError(TimeoutError):
    """A deadline owned by :class:`ToolTimeoutInterceptor` expired."""

    code = "tool_execution_timeout"

    def __init__(self, timeout_seconds: float, *, retryable: bool = False):
        self.timeout_seconds = timeout_seconds
        self.retryable = retryable
        super().__init__(
            f"{self.code}: tool exceeded {timeout_seconds:g} seconds"
        )


class ToolExecutionContractError(RuntimeError):
    """A tool advertised an unsupported execution contract."""

    code = "tool_execution_contract_invalid"
    retryable = False

    def __init__(self, field_name: str):
        self.field_name = field_name
        super().__init__(f"{self.code}: invalid {field_name}")


PolicySnapshotProvider = Callable[
    [ToolExecutionContext],
    ToolPolicySnapshot | Awaitable[ToolPolicySnapshot],
]
PolicyEvaluator = Callable[
    [ToolExecutionContext, ToolPolicySnapshot],
    ToolPolicyDecision | Awaitable[ToolPolicyDecision],
]


class ToolPolicyGuardInterceptor(ToolExecutionInterceptor):
    """Fail-closed tool authorization evaluated before any handler runs."""

    def __init__(
        self,
        snapshot: ToolPolicySnapshot | PolicySnapshotProvider | None = None,
        *,
        evaluator: PolicyEvaluator | None = None,
        evaluation_timeout_seconds: float = 2.0,
    ) -> None:
        self._snapshot = snapshot or ToolPolicySnapshot.deny_all()
        self._evaluator = evaluator or evaluate_snapshot_policy
        self._evaluation_timeout_seconds = _positive_finite(
            evaluation_timeout_seconds,
            "evaluation_timeout_seconds",
        )

    async def guard(self, context: ToolExecutionContext) -> None:
        try:
            async with asyncio.timeout(self._evaluation_timeout_seconds):
                snapshot = await _resolve_policy_snapshot(self._snapshot, context)
                decision = self._evaluator(context, snapshot)
                if inspect.isawaitable(decision):
                    decision = await decision
                if not isinstance(decision, ToolPolicyDecision):
                    raise TypeError("policy evaluator returned an unsupported decision")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Tool policy evaluation failed closed tool=%s error_type=%s",
                opaque_log_identifier(context.tool_name, namespace="tool"),
                type(error).__name__,
            )
            context.metadata.update({
                "policy_snapshot_version": "unavailable",
                "policy_decision": "deny",
                "policy_reason": "policy_evaluation_failed",
                "policy_rule": "fail_closed",
            })
            raise ToolExecutionDeniedError(
                reason="policy_evaluation_failed",
                rule_id="fail_closed",
            ) from error

        context.metadata.update({
            "policy_snapshot_version": snapshot.version,
            "policy_decision": "allow" if decision.allowed else "deny",
            "policy_reason": decision.reason,
            "policy_rule": decision.rule_id,
        })
        effects, effects_valid = _contract_string_set(context, "effects")
        permissions, permissions_valid = _contract_string_set(context, "permissions")
        if effects_valid and effects is not None:
            context.metadata["tool_effects"] = tuple(sorted(effects))
        if permissions_valid and permissions is not None:
            context.metadata["tool_permissions"] = tuple(sorted(permissions))
        if not decision.allowed:
            raise ToolExecutionDeniedError(
                reason=decision.reason,
                rule_id=decision.rule_id,
            )


def evaluate_snapshot_policy(
    context: ToolExecutionContext,
    snapshot: ToolPolicySnapshot,
) -> ToolPolicyDecision:
    """Evaluate exact names, effects and permissions without argument values."""
    tool_name = context.tool_name
    if not tool_name:
        return ToolPolicyDecision(False, "missing_tool_name", "identity.required")
    if (
        context.actual_tool_name
        and context.requested_tool_name
        and context.actual_tool_name != context.requested_tool_name
    ):
        return ToolPolicyDecision(False, "tool_identity_mismatch", "identity.matches")
    if tool_name in snapshot.denied_tools:
        return ToolPolicyDecision(False, "tool_explicitly_denied", "tool.deny")
    if (
        tool_name not in snapshot.allowed_tools
        and snapshot.default_decision is ToolPolicyDefault.DENY
    ):
        return ToolPolicyDecision(False, "tool_not_allowed", "tool.allowlist")

    effects, effects_valid = _contract_string_set(context, "effects")
    if not effects_valid:
        return ToolPolicyDecision(False, "invalid_effect_contract", "effects.valid")
    if effects is None:
        if not snapshot.allow_unclassified_effects:
            return ToolPolicyDecision(
                False,
                "unclassified_tool_effects",
                "effects.classified",
            )
    else:
        denied_effects = effects - snapshot.allowed_effects
        if denied_effects:
            return ToolPolicyDecision(False, "effect_not_allowed", "effects.allowlist")

    permissions, permissions_valid = _contract_string_set(context, "permissions")
    if not permissions_valid:
        return ToolPolicyDecision(
            False,
            "invalid_permission_contract",
            "permissions.valid",
        )
    if permissions and not permissions.issubset(snapshot.granted_permissions):
        return ToolPolicyDecision(
            False,
            "permission_not_granted",
            "permissions.granted",
        )
    return ToolPolicyDecision(True, "policy_allowed", "snapshot")


class ToolTimeoutInterceptor(ToolExecutionInterceptor):
    """Apply an asyncio deadline while preserving external cancellation."""

    def __init__(
        self,
        default_timeout_seconds: float = 120.0,
        *,
        maximum_timeout_seconds: float = 300.0,
        cancellation_cleanup_timeout_seconds: float = 3.0,
    ) -> None:
        self.default_timeout_seconds = _positive_finite(
            default_timeout_seconds,
            "default_timeout_seconds",
        )
        self.maximum_timeout_seconds = _positive_finite(
            maximum_timeout_seconds,
            "maximum_timeout_seconds",
        )
        self.cancellation_cleanup_timeout_seconds = _positive_finite(
            cancellation_cleanup_timeout_seconds,
            "cancellation_cleanup_timeout_seconds",
        )
        if self.default_timeout_seconds > self.maximum_timeout_seconds:
            raise ValueError("default timeout must not exceed maximum timeout")

    async def execute(
        self,
        context: ToolExecutionContext,
        call_next: ToolExecutionNext,
    ) -> Any:
        timeout_seconds = self._resolve_timeout_seconds(context)
        context.metadata["execution_timeout_seconds"] = timeout_seconds
        cancellable = _contract_value(context, "cancellable")
        if isinstance(cancellable, bool):
            context.metadata["tool_cancellable"] = cancellable

        deadline = asyncio.timeout(timeout_seconds)
        try:
            async with deadline:
                return await call_next()
        except asyncio.CancelledError:
            context.metadata["execution_outcome_hint"] = "cancelled"
            await self._notify_cancellation(context, "cancelled")
            raise
        except TimeoutError as error:
            # A tool may legitimately raise its own TimeoutError. Convert only
            # the expiry owned by this interceptor.
            if not deadline.expired():
                raise
            context.metadata["execution_outcome_hint"] = "timeout"
            await self._notify_cancellation(context, "timeout")
            effects, effects_valid = _contract_string_set(context, "effects")
            retryable = bool(
                effects_valid
                and effects is not None
                and effects.issubset({"sandbox_read"})
            )
            raise ToolExecutionTimeoutError(
                timeout_seconds,
                retryable=retryable,
            ) from error

    def _resolve_timeout_seconds(self, context: ToolExecutionContext) -> float:
        configured = _contract_value(context, "timeout_seconds")
        if configured is None:
            configured = getattr(context.tool, "timeout_seconds", None)
        if isinstance(configured, bool) or not isinstance(configured, (int, float)):
            return self.default_timeout_seconds
        try:
            normalized = _positive_finite(float(configured), "tool timeout")
        except ValueError:
            return self.default_timeout_seconds
        return min(normalized, self.maximum_timeout_seconds)

    async def _notify_cancellation(
        self,
        context: ToolExecutionContext,
        reason: str,
    ) -> None:
        try:
            async with asyncio.timeout(self.cancellation_cleanup_timeout_seconds):
                await context.notify_cancellation(reason)
        except TimeoutError:
            logger.warning(
                "Tool cancellation cleanup timed out tool=%s reason=%s",
                opaque_log_identifier(context.tool_name, namespace="tool"),
                reason,
            )


class ToolConcurrencyInterceptor(ToolExecutionInterceptor):
    """Enforce contract concurrency per session/plugin/tool in this process."""

    def __init__(self, *, session_scope: str | None = None) -> None:
        self._session_scope = _safe_text(session_scope) if session_scope else None
        self._exclusive_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def execute(
        self,
        context: ToolExecutionContext,
        call_next: ToolExecutionNext,
    ) -> Any:
        concurrency = _contract_value(context, "concurrency")
        if concurrency is None:
            concurrency = "exclusive"
        if concurrency == "parallel":
            context.metadata["tool_concurrency"] = concurrency
            await self._notify_admitted(context)
            return await call_next()
        if concurrency != "exclusive":
            raise ToolExecutionContractError("execution.concurrency")
        context.metadata["tool_concurrency"] = concurrency
        lock = self._exclusive_locks.setdefault(
            _tool_concurrency_key(context, self._session_scope),
            asyncio.Lock(),
        )
        async with lock:
            await self._notify_admitted(context)
            return await call_next()

    @staticmethod
    async def _notify_admitted(context: ToolExecutionContext) -> None:
        callback = context.metadata.get("analysis_job_admitted")
        if callable(callback):
            await callback()


class ToolTracePhase(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ToolExecutionTraceEvent:
    schema_version: str
    invocation_id: str
    phase: ToolTracePhase
    occurred_at: str
    tool_name: str
    tool_call_id: str
    toolkit: str | None
    argument_keys: tuple[str, ...]
    duration_ms: float | None
    timeout_seconds: float | None
    cancellable: bool | None
    effects: tuple[str, ...]
    permissions: tuple[str, ...]
    policy_version: str | None
    policy_decision: str | None
    policy_reason: str | None
    policy_rule: str | None
    error_code: str | None
    error_type: str | None
    attributes: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "phase": self.phase.value,
            "occurred_at": self.occurred_at,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "toolkit": self.toolkit,
            "argument_keys": list(self.argument_keys),
            "duration_ms": self.duration_ms,
            "timeout_seconds": self.timeout_seconds,
            "cancellable": self.cancellable,
            "effects": list(self.effects),
            "permissions": list(self.permissions),
            "policy_version": self.policy_version,
            "policy_decision": self.policy_decision,
            "policy_reason": self.policy_reason,
            "policy_rule": self.policy_rule,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "attributes": dict(self.attributes),
        }


class ToolExecutionTraceSink(Protocol):
    def emit(
        self,
        event: ToolExecutionTraceEvent,
    ) -> None | Awaitable[None]:
        ...


class JsonLoggingToolTraceSink:
    """Emit one machine-readable, secret-free record per lifecycle event."""

    def __init__(self, trace_logger: logging.Logger | None = None) -> None:
        self._logger = trace_logger or logging.getLogger("app.tool_execution")

    def emit(self, event: ToolExecutionTraceEvent) -> None:
        self._logger.info(
            "tool_execution_trace=%s",
            json.dumps(event.as_dict(), ensure_ascii=True, separators=(",", ":")),
        )


class CompositeToolTraceSink:
    def __init__(self, sinks: Iterable[ToolExecutionTraceSink]) -> None:
        self._sinks = tuple(sinks)

    async def emit(self, event: ToolExecutionTraceEvent) -> None:
        for sink in self._sinks:
            result = sink.emit(event)
            if inspect.isawaitable(result):
                await result


class AuditServiceToolTraceSink:
    """Adapter from structured terminal traces to the existing audit store."""

    def __init__(
        self,
        audit_service: Any,
        *,
        actor_user_id: str,
        workspace_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        include_started: bool = False,
    ) -> None:
        self._audit_service = audit_service
        self._actor_user_id = actor_user_id
        self._workspace_id = workspace_id
        self._session_id = session_id
        self._task_id = task_id
        self._include_started = include_started

    async def emit(self, event: ToolExecutionTraceEvent) -> None:
        if event.phase is ToolTracePhase.STARTED and not self._include_started:
            return
        status = {
            ToolTracePhase.STARTED: AuditStatus.SUCCESS,
            ToolTracePhase.SUCCEEDED: AuditStatus.SUCCESS,
            ToolTracePhase.DENIED: AuditStatus.DENIED,
        }.get(event.phase, AuditStatus.FAILED)
        effects = set(event.effects)
        if effects & {"credential_use", "external_side_effect"}:
            risk_level = AuditRiskLevel.HIGH
        elif effects & {"network", "sandbox_write"}:
            risk_level = AuditRiskLevel.MEDIUM
        else:
            risk_level = AuditRiskLevel.LOW
        await self._audit_service.record(
            actor_user_id=self._actor_user_id,
            workspace_id=self._workspace_id,
            session_id=self._session_id,
            task_id=self._task_id,
            action="tool.execute",
            resource_type="tool",
            resource_id=event.tool_name,
            status=status,
            risk_level=risk_level,
            metadata={
                "trace_schema_version": event.schema_version,
                "invocation_id": event.invocation_id,
                "tool_call_id": event.tool_call_id,
                "toolkit": event.toolkit,
                "phase": event.phase.value,
                "argument_keys": list(event.argument_keys),
                "duration_ms": event.duration_ms,
                "timeout_seconds": event.timeout_seconds,
                "cancellable": event.cancellable,
                "effects": list(event.effects),
                "permissions": list(event.permissions),
                "policy_version": event.policy_version,
                "policy_decision": event.policy_decision,
                "policy_reason": event.policy_reason,
                "policy_rule": event.policy_rule,
                "error_code": event.error_code,
                "error_type": event.error_type,
            },
        )


class StructuredToolTraceInterceptor(ToolExecutionInterceptor):
    """Record one start and exactly one terminal event for each invocation."""

    _START_KEY = "_tool_trace_started_monotonic"
    _ID_KEY = "_tool_trace_invocation_id"
    _TERMINAL_KEY = "_tool_trace_terminal_emitted"

    def __init__(
        self,
        sink: ToolExecutionTraceSink | None = None,
        *,
        attributes: Mapping[str, Any] | None = None,
        strict: bool = False,
        sink_timeout_seconds: float = 2.0,
    ) -> None:
        self._sink = sink or JsonLoggingToolTraceSink()
        self._attributes = _safe_trace_attributes(attributes or {})
        self._strict = strict
        self._sink_timeout_seconds = _positive_finite(
            sink_timeout_seconds,
            "sink_timeout_seconds",
        )

    async def pre_execute(self, context: ToolExecutionContext) -> None:
        context.metadata.setdefault(self._START_KEY, time.monotonic())
        context.metadata.setdefault(self._ID_KEY, uuid.uuid4().hex)
        await self._emit(context, ToolTracePhase.STARTED)

    async def complete(self, context: ToolExecutionContext, result: Any) -> None:
        if _result_explicitly_failed(result):
            await self._emit_terminal(
                context,
                ToolTracePhase.FAILED,
                error_code="tool_result_failed",
            )
            return
        await self._emit_terminal(context, ToolTracePhase.SUCCEEDED)

    async def on_error(
        self,
        context: ToolExecutionContext,
        error: BaseException,
    ) -> None:
        if isinstance(error, ToolExecutionDeniedError) or getattr(error, "code", None) == "tool_authorization_stopped":
            phase = ToolTracePhase.DENIED
            error_code = error.code
        elif isinstance(error, ToolExecutionTimeoutError):
            phase = ToolTracePhase.TIMED_OUT
            error_code = error.code
        elif isinstance(error, asyncio.CancelledError):
            phase = ToolTracePhase.CANCELLED
            error_code = "tool_execution_cancelled"
        else:
            phase = ToolTracePhase.FAILED
            candidate_code = getattr(error, "code", None)
            error_code = (
                candidate_code
                if isinstance(candidate_code, str) and candidate_code
                else "tool_execution_failed"
            )
        await self._emit_terminal(
            context,
            phase,
            error_code=error_code,
            error_type=type(error).__name__,
            sink_timeout_seconds=(
                min(self._sink_timeout_seconds, 0.1)
                if isinstance(error, asyncio.CancelledError)
                else None
            ),
        )

    async def _emit_terminal(
        self,
        context: ToolExecutionContext,
        phase: ToolTracePhase,
        *,
        error_code: str | None = None,
        error_type: str | None = None,
        sink_timeout_seconds: float | None = None,
    ) -> None:
        if context.metadata.get(self._TERMINAL_KEY):
            return
        await self._emit(
            context,
            phase,
            error_code=error_code,
            error_type=error_type,
            sink_timeout_seconds=sink_timeout_seconds,
        )
        context.metadata[self._TERMINAL_KEY] = True

    async def _emit(
        self,
        context: ToolExecutionContext,
        phase: ToolTracePhase,
        *,
        error_code: str | None = None,
        error_type: str | None = None,
        sink_timeout_seconds: float | None = None,
    ) -> None:
        event = _trace_event(
            context,
            phase,
            self._attributes,
            error_code=error_code,
            error_type=error_type,
        )
        try:
            result = self._sink.emit(event)
            if inspect.isawaitable(result):
                async with asyncio.timeout(
                    sink_timeout_seconds or self._sink_timeout_seconds
                ):
                    await result
        except asyncio.CancelledError:
            # User/task cancellation is control flow, never a best-effort
            # telemetry failure. Suppressing it could let the tool execute
            # after its caller has cancelled the turn.
            raise
        except Exception as error:
            logger.warning(
                "Tool trace sink failed sink=%s error_type=%s",
                type(self._sink).__name__,
                type(error).__name__,
            )
            if self._strict:
                raise


def create_production_tool_interceptors(
    *,
    policy_snapshot: ToolPolicySnapshot | PolicySnapshotProvider | None = None,
    trace_sink: ToolExecutionTraceSink | None = None,
    trace_attributes: Mapping[str, Any] | None = None,
    default_timeout_seconds: float = 120.0,
    maximum_timeout_seconds: float = 120.0,
    cancellation_cleanup_timeout_seconds: float = 5.0,
    spill_store: SpillArtifactStore | None = None,
    spill_owner: SpillArtifactOwner | None = None,
    spill_max_inline_bytes: int = 50_000,
    spill_max_artifact_bytes: int = 32 * 1024 * 1024,
    spill_preview_bytes: int = 12_000,
    spill_store_timeout_seconds: float = 5.0,
    analysis_job_interceptor: ToolExecutionInterceptor | None = None,
    call_authorization_interceptor: ToolExecutionInterceptor | None = None,
) -> tuple[ToolExecutionInterceptor, ...]:
    """Return interceptors in the supported production registration order."""
    interceptors: list[ToolExecutionInterceptor] = [
        StructuredToolTraceInterceptor(trace_sink, attributes=trace_attributes),
        call_authorization_interceptor or ToolPolicyGuardInterceptor(policy_snapshot),
        ToolTimeoutInterceptor(
            default_timeout_seconds,
            maximum_timeout_seconds=maximum_timeout_seconds,
            cancellation_cleanup_timeout_seconds=(
                cancellation_cleanup_timeout_seconds
            ),
        ),
        ToolConcurrencyInterceptor(
            session_scope=(
                str(trace_attributes["session_id"])
                if trace_attributes and trace_attributes.get("session_id")
                else None
            ),
        ),
    ]
    if analysis_job_interceptor is not None:
        # Guards still run before execution. The job encloses timeout and
        # concurrency, so queued time and targeted cancellation are tracked.
        interceptors.insert(2, analysis_job_interceptor)
    if call_authorization_interceptor is not None:
        from app.domain.services.tools.authorization import ToolCallAdmissionInterceptor
        interceptors.append(ToolCallAdmissionInterceptor(call_authorization_interceptor))
    if spill_store is not None and spill_owner is not None:
        # Result hooks unwind in reverse order. Registering spill last makes it
        # the first transformer at the final result boundary while leaving all
        # pre-execution policy/timeout/concurrency behavior untouched.
        interceptors.append(SpillArtifactInterceptor(
            spill_store,
            owner=spill_owner,
            max_inline_bytes=spill_max_inline_bytes,
            max_artifact_bytes=spill_max_artifact_bytes,
            preview_bytes=spill_preview_bytes,
            store_timeout_seconds=spill_store_timeout_seconds,
        ))
    return tuple(interceptors)


async def _resolve_policy_snapshot(
    source: ToolPolicySnapshot | PolicySnapshotProvider,
    context: ToolExecutionContext,
) -> ToolPolicySnapshot:
    if isinstance(source, ToolPolicySnapshot):
        return source
    snapshot = source(context)
    if inspect.isawaitable(snapshot):
        snapshot = await snapshot
    if not isinstance(snapshot, ToolPolicySnapshot):
        raise TypeError("policy snapshot provider returned an unsupported value")
    return snapshot


def _normalized_identifiers(values: Iterable[str], label: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} must be a collection of strings")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must contain non-empty strings")
        normalized.add(value.strip())
    return frozenset(normalized)


def _tool_execution_contract(context: ToolExecutionContext) -> Mapping[str, Any] | None:
    candidates = (
        context.metadata.get("execution_contract"),
        getattr(context.tool, "execution_contract", None),
        getattr(context.tool, "definition", None),
        getattr(context.tool, "_definition", None),
    )
    for candidate in candidates:
        if hasattr(candidate, "model_dump"):
            candidate = candidate.model_dump(mode="python")
        if not isinstance(candidate, Mapping):
            continue
        execution = candidate.get("execution")
        if hasattr(execution, "model_dump"):
            execution = execution.model_dump(mode="python")
        if isinstance(execution, Mapping):
            return execution
        # Also accept an already-unwrapped execution object from an in-process
        # toolkit without forcing it to emulate the plugin manifest envelope.
        if any(
            key in candidate
            for key in ("timeout_seconds", "cancellable", "effects", "permissions")
        ):
            return candidate
    return None


def _tool_concurrency_key(
    context: ToolExecutionContext,
    session_scope: str | None = None,
) -> tuple[str, str, str]:
    plugin: Any = None
    for candidate in (
        getattr(context.tool, "definition", None),
        getattr(context.tool, "_definition", None),
    ):
        if hasattr(candidate, "model_dump"):
            candidate = candidate.model_dump(mode="python")
        if isinstance(candidate, Mapping) and candidate.get("plugin"):
            plugin = candidate["plugin"]
            break
    if plugin is None:
        plugin = getattr(context.tool, "plugin", None)
    if plugin is None:
        plugin = getattr(getattr(context.tool, "toolkit", None), "name", None)
    toolkit = getattr(context.tool, "toolkit", None)
    session = context.metadata.get("session_id")
    if not isinstance(session, str) or not session:
        session = getattr(toolkit, "session_id", None)
    if not isinstance(session, str) or not session:
        session = session_scope
    if not isinstance(session, str) or not session:
        # An old in-process toolkit may not yet expose a session identifier.
        # Scoping to that toolkit instance preserves serialization inside the
        # current flow without coupling unrelated users process-wide.
        session = f"toolkit:{id(toolkit)}"
    return (
        _safe_text(session),
        _safe_text(plugin or "core"),
        _safe_text(context.tool_name),
    )


def _contract_value(context: ToolExecutionContext, key: str) -> Any:
    contract = _tool_execution_contract(context)
    return contract.get(key) if contract is not None else None


def _contract_string_set(
    context: ToolExecutionContext,
    key: str,
) -> tuple[frozenset[str] | None, bool]:
    contract = _tool_execution_contract(context)
    if contract is None or key not in contract:
        return None, True
    value = contract[key]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None, False
    try:
        return _normalized_identifiers(value, key), True
    except ValueError:
        return None, False


def _positive_finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return normalized


def _result_explicitly_failed(result: Any) -> bool:
    artifact = getattr(result, "artifact", None)
    candidate = artifact if artifact is not None else result
    if isinstance(candidate, Mapping):
        return candidate.get("success") is False
    return getattr(candidate, "success", None) is False


def _safe_text(value: Any, maximum: int = 160) -> str:
    text = "".join(
        character if character.isprintable() and character not in "\r\n" else "?"
        for character in str(value)
    )
    return text if len(text) <= maximum else f"{text[:maximum]}..."


def _safe_trace_attributes(
    attributes: Mapping[str, Any],
) -> Mapping[str, str | int | float | bool]:
    safe: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if key not in _TRACE_ATTRIBUTE_KEYS:
            continue
        identifier_namespace = _TRACE_IDENTIFIER_ATTRIBUTES.get(key)
        if identifier_namespace is not None:
            if value is not None and value != "":
                safe[key] = opaque_log_identifier(
                    value,
                    namespace=identifier_namespace,
                )
            continue
        if isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, int) or math.isfinite(value):
                safe[key] = value
        elif isinstance(value, str):
            pattern = _TRACE_ATTRIBUTE_PATTERNS.get(key)
            safe[key] = (
                value
                if pattern is not None and pattern.fullmatch(value)
                else opaque_log_identifier(value, namespace=key.replace("_", "-"))
            )
    return MappingProxyType(safe)


def _trace_event(
    context: ToolExecutionContext,
    phase: ToolTracePhase,
    attributes: Mapping[str, str | int | float | bool],
    *,
    error_code: str | None,
    error_type: str | None,
) -> ToolExecutionTraceEvent:
    started = context.metadata.get(StructuredToolTraceInterceptor._START_KEY)
    duration_ms = None
    if phase is not ToolTracePhase.STARTED and isinstance(started, (int, float)):
        duration_ms = round(max(0.0, (time.monotonic() - started) * 1000), 3)
    tool = context.tool
    toolkit_value = getattr(getattr(tool, "toolkit", None), "name", None)
    toolkit = _safe_trace_toolkit(toolkit_value)
    timeout = context.metadata.get("execution_timeout_seconds")
    timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else None
    cancellable_value = context.metadata.get("tool_cancellable")
    cancellable = cancellable_value if isinstance(cancellable_value, bool) else None
    effects = context.metadata.get("tool_effects", ())
    permissions = context.metadata.get("tool_permissions", ())
    invocation_id = context.metadata.get(StructuredToolTraceInterceptor._ID_KEY)
    if not isinstance(invocation_id, str):
        invocation_id = uuid.uuid4().hex
        context.metadata[StructuredToolTraceInterceptor._ID_KEY] = invocation_id
    return ToolExecutionTraceEvent(
        schema_version="tool-execution-trace/v1",
        invocation_id=invocation_id,
        phase=phase,
        occurred_at=datetime.now(UTC).isoformat(),
        tool_name=_safe_trace_tool_name(context),
        tool_call_id=opaque_log_identifier(context.tool_call_id, namespace="call"),
        toolkit=toolkit,
        argument_keys=tuple(_declared_trace_argument_keys(context)),
        duration_ms=duration_ms,
        timeout_seconds=timeout_seconds,
        cancellable=cancellable,
        effects=(
            tuple(effect for effect in effects if effect in KNOWN_TOOL_EFFECTS)
            + ((f"[unknown-effects:{sum(1 for effect in effects if effect not in KNOWN_TOOL_EFFECTS)}]",)
               if any(effect not in KNOWN_TOOL_EFFECTS for effect in effects)
               else ())
            if isinstance(effects, tuple)
            else ()
        ),
        permissions=(
            tuple(
                opaque_log_identifier(permission, namespace="permission")
                for permission in permissions
            )
            if isinstance(permissions, tuple)
            else ()
        ),
        policy_version=_optional_safe_metadata_text(context, "policy_snapshot_version"),
        policy_decision=_optional_safe_metadata_text(context, "policy_decision"),
        policy_reason=_optional_safe_metadata_text(context, "policy_reason"),
        policy_rule=_optional_safe_metadata_text(context, "policy_rule"),
        error_code=_safe_trace_error_code(error_code),
        error_type=_safe_trace_type(error_type),
        attributes=attributes,
    )


def _safe_trace_tool_name(context: ToolExecutionContext) -> str:
    """Keep validated catalog names readable; hash all fallback identities."""
    name = context.actual_tool_name
    if name and _TRACE_TOOL_NAME_PATTERN.fullmatch(name):
        return name
    return opaque_log_identifier(context.tool_name, namespace="tool")


def _safe_trace_toolkit(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _TRACE_TOOLKIT_PATTERN.fullmatch(value):
        return value
    return opaque_log_identifier(value, namespace="toolkit")


def _declared_trace_argument_keys(context: ToolExecutionContext) -> list[str]:
    """Trace declared field names and only a count for extension input."""
    tool = context.tool
    schema = context.metadata.get("input_schema")
    if not isinstance(schema, Mapping):
        definition = getattr(tool, "definition", None)
        if isinstance(definition, Mapping):
            schema = definition.get("parameters")
    if not isinstance(schema, Mapping):
        schema = getattr(tool, "input_schema", None)

    declared: set[str] = set()
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            declared.update(str(key) for key in properties)
    if not declared:
        args_schema = getattr(tool, "args_schema", None)
        model_fields = getattr(args_schema, "model_fields", None)
        if isinstance(model_fields, Mapping):
            declared.update(str(key) for key in model_fields)

    arguments = context.arguments
    known = {
        key: None
        for key in arguments
        if isinstance(key, str) and key in declared
    }
    result = summarize_argument_keys(known)
    undeclared_count = len(arguments) - len(known)
    if undeclared_count:
        if len(result) >= 20:
            result = result[:19]
        result.append(f"[undeclared-keys:{undeclared_count}]")
    return result


_KNOWN_TRACE_ERROR_CODES = frozenset({
    "tool_execution_cancelled",
    "tool_execution_contract_invalid",
    "tool_execution_denied",
    "tool_execution_failed",
    "tool_execution_timeout",
    "tool_result_failed",
})


def _safe_trace_error_code(value: str | None) -> str | None:
    if not value:
        return None
    if value in _KNOWN_TRACE_ERROR_CODES:
        return value
    return opaque_log_identifier(value, namespace="error-code")


def _safe_trace_type(value: str | None) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", value):
        return value
    return opaque_log_identifier(value, namespace="error-type")


def _optional_safe_metadata_text(
    context: ToolExecutionContext,
    key: str,
) -> str | None:
    value = context.metadata.get(key)
    return _safe_text(value) if isinstance(value, str) and value else None


__all__ = [
    "AuditServiceToolTraceSink",
    "CompositeToolTraceSink",
    "JsonLoggingToolTraceSink",
    "KNOWN_TOOL_EFFECTS",
    "StructuredToolTraceInterceptor",
    "ToolConcurrencyInterceptor",
    "ToolExecutionContractError",
    "ToolExecutionDeniedError",
    "ToolExecutionTimeoutError",
    "ToolExecutionTraceEvent",
    "ToolExecutionTraceSink",
    "ToolPolicyDecision",
    "ToolPolicyDefault",
    "ToolPolicyGuardInterceptor",
    "ToolPolicySnapshot",
    "ToolTimeoutInterceptor",
    "ToolTracePhase",
    "create_production_tool_interceptors",
    "evaluate_snapshot_policy",
]
