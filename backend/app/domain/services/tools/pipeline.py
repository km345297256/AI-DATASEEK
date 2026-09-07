from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import logging
from typing import Any, Awaitable, Callable, Iterable, Mapping


ToolExecutionNext = Callable[[], Awaitable[Any]]
ToolExecutionHandler = Callable[["ToolExecutionContext"], Awaitable[Any]]
ToolExecutionDisposer = Callable[[], None]
ToolCancellationCallback = Callable[[str], Awaitable[None] | None]

logger = logging.getLogger(__name__)

_MAX_LOGGED_ARGUMENT_KEYS = 20


def opaque_log_identifier(value: Any, *, namespace: str = "ref") -> str:
    """Return a stable correlation token without logging attacker text."""
    if value is None or value == "":
        return ""
    digest = hashlib.sha256(
        str(value).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"{namespace}:sha256:{digest}"


def summarize_argument_keys(arguments: Any) -> list[str]:
    """Return bounded, stable hashes for argument names without raw text."""
    if not isinstance(arguments, Mapping):
        return []

    keys = [opaque_log_identifier(key, namespace="arg") for key in arguments]

    keys.sort()
    if len(keys) > _MAX_LOGGED_ARGUMENT_KEYS:
        omitted = len(keys) - (_MAX_LOGGED_ARGUMENT_KEYS - 1)
        keys = keys[:_MAX_LOGGED_ARGUMENT_KEYS - 1] + [f"...(+{omitted})"]
    return keys


@dataclass(slots=True)
class ToolExecutionContext:
    """Stable metadata shared by each stage of one tool invocation.

    ``tool_call`` deliberately keeps the caller-owned object.  With no custom
    interceptor the pipeline does not rewrite arguments, call ids, or return
    values, keeping the existing LangChain and UI event contracts unchanged.
    """

    tool: Any
    tool_call: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    _cancellation_callbacks: list[ToolCancellationCallback] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _cancellation_reason: str | None = field(default=None, init=False, repr=False)

    @property
    def requested_tool_name(self) -> str:
        if isinstance(self.tool_call, dict):
            name = self.tool_call.get("name")
            if isinstance(name, str) and name:
                return name
        return ""

    @property
    def actual_tool_name(self) -> str:
        name = getattr(self.tool, "name", "")
        return name if isinstance(name, str) and name else ""

    @property
    def tool_name(self) -> str:
        """Return the executable identity, falling back only for legacy tools."""
        return self.actual_tool_name or self.requested_tool_name

    @property
    def tool_call_id(self) -> Any:
        if not isinstance(self.tool_call, dict):
            return ""
        return self.tool_call.get("id", "")

    @property
    def arguments(self) -> dict[str, Any]:
        if not isinstance(self.tool_call, dict):
            return {}
        arguments = self.tool_call.get("args", {})
        return arguments if isinstance(arguments, dict) else {}

    @property
    def cancellation_reason(self) -> str | None:
        return self._cancellation_reason

    def register_cancellation_callback(
        self,
        callback: ToolCancellationCallback,
    ) -> ToolExecutionDisposer:
        """Register cooperative cleanup for an in-flight tool operation."""
        if self._cancellation_reason is not None:
            raise RuntimeError("tool invocation is already cancelling")
        self._cancellation_callbacks.append(callback)
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            self._cancellation_callbacks = [
                registered
                for registered in self._cancellation_callbacks
                if registered is not callback
            ]

        return dispose

    async def notify_cancellation(self, reason: str) -> None:
        """Run registered cleanup once, in reverse acquisition order."""
        if self._cancellation_reason is not None:
            return
        self._cancellation_reason = reason
        callbacks = tuple(reversed(self._cancellation_callbacks))
        self._cancellation_callbacks.clear()
        for callback in callbacks:
            try:
                value = callback(reason)
                if inspect.isawaitable(value):
                    await value
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Cleanup is advisory and may run while the owning asyncio task
                # is already being cancelled. Never replace the primary signal.
                logger.warning(
                    "Tool cancellation callback failed callback=%s error_type=%s",
                    opaque_log_identifier(
                        getattr(callback, "__name__", type(callback).__name__),
                        namespace="callback",
                    ),
                    type(error).__name__,
                )


class ToolExecutionInterceptor:
    """Optional no-op hooks around tool execution.

    Subclasses can implement only the stages they need.  Raising from ``guard``
    prevents execution and is left to the existing Agent error/retry contract.
    ``execute`` is around-style middleware, while ``post_execute`` and
    ``result`` unwind in reverse registration order.  A custom ``result`` hook
    may explicitly replace the value returned to the caller.
    """

    async def pre_execute(self, context: ToolExecutionContext) -> None:
        return None

    async def guard(self, context: ToolExecutionContext) -> None:
        return None

    async def execute(
        self,
        context: ToolExecutionContext,
        call_next: ToolExecutionNext,
    ) -> Any:
        return await call_next()

    async def post_execute(
        self,
        context: ToolExecutionContext,
        result: Any,
    ) -> None:
        return None

    async def result(
        self,
        context: ToolExecutionContext,
        result: Any,
    ) -> Any:
        return result

    async def complete(
        self,
        context: ToolExecutionContext,
        result: Any,
    ) -> None:
        """Observe the final value after every result transformer has run."""
        return None

    async def on_error(
        self,
        context: ToolExecutionContext,
        error: BaseException,
    ) -> None:
        """Observe a failed or cancelled invocation without replacing its error."""
        return None


class ToolExecutionPipeline:
    """Run the compatible pre/guard/execute/post/result tool lifecycle."""

    def __init__(
        self,
        interceptors: Iterable[ToolExecutionInterceptor] = (),
    ) -> None:
        self._registrations = [
            (object(), interceptor)
            for interceptor in interceptors
        ]

    @property
    def interceptors(self) -> tuple[ToolExecutionInterceptor, ...]:
        return tuple(interceptor for _, interceptor in self._registrations)

    def register(
        self,
        interceptor: ToolExecutionInterceptor,
    ) -> ToolExecutionDisposer:
        """Register one interceptor and return an idempotent disposer."""
        marker = object()
        self._registrations.append((marker, interceptor))
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            self._registrations = [
                entry for entry in self._registrations
                if entry[0] is not marker
            ]

        return dispose

    async def invoke(
        self,
        *,
        tool: Any,
        tool_call: Any,
        execute: ToolExecutionHandler,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        context = ToolExecutionContext(
            tool=tool,
            tool_call=tool_call,
            metadata=dict(metadata or {}),
        )
        # One invocation sees one stable interceptor tree. Registration changes
        # take effect on the next invocation, never halfway through a tool call.
        interceptors = self.interceptors

        try:
            for interceptor in interceptors:
                await interceptor.pre_execute(context)
            for interceptor in interceptors:
                await interceptor.guard(context)

            async def execute_at(index: int) -> Any:
                if index >= len(interceptors):
                    return await execute(context)
                interceptor = interceptors[index]
                return await interceptor.execute(
                    context,
                    lambda: execute_at(index + 1),
                )

            value = await execute_at(0)
            for interceptor in reversed(interceptors):
                await interceptor.post_execute(context, value)
            for interceptor in reversed(interceptors):
                value = await interceptor.result(context, value)
            for interceptor in reversed(interceptors):
                await interceptor.complete(context, value)
            return value
        except BaseException as error:
            # Error observers are best-effort cleanup/telemetry hooks. The
            # original exception (especially ``CancelledError``) must retain
            # its identity and traceback even if an observer itself fails.
            for interceptor in reversed(interceptors):
                try:
                    await interceptor.on_error(context, error)
                except BaseException as observer_error:
                    # Exception messages and tracebacks can contain tool
                    # arguments or provider payloads. Keep this diagnostic
                    # type-only, matching the normal tool failure log policy.
                    logger.warning(
                        "Tool execution error observer failed interceptor=%s tool=%s "
                        "observer_error_type=%s primary_error_type=%s",
                        type(interceptor).__name__,
                        opaque_log_identifier(context.tool_name, namespace="tool"),
                        type(observer_error).__name__,
                        type(error).__name__,
                    )
            raise
