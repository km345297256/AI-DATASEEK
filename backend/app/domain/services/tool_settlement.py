"""Task-local cleanup for tool generators abandoned by their event consumer.

Closing an async generator cannot yield an event. The runner therefore owns a
small cleanup scope which closes registered producers before leaving the live
delivery context and persists their terminal events through its normal sink.
This is host control flow, not a tool retry or an alternative execution path.
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import asyncio
import logging

from app.domain.models.event import ToolEvent

logger = logging.getLogger(__name__)
Cleanup = Callable[[], Awaitable[list[ToolEvent]]]
_active_cleanups: ContextVar[list[Cleanup] | None] = ContextVar("tool_settlement_cleanups", default=None)


@dataclass
class ToolCallSettlement:
    call: dict[str, Any]
    tool: Any = None
    display_args: dict[str, Any] | None = None
    presentation: dict[str, Any] | None = None
    invocation_entered: bool = False
    result: Any = None
    memory_response: Any = None
    persistence_attempted: bool = False
    persisted: bool = False
    event: ToolEvent | None = None
    event_delivered: bool = False


def register_tool_settlement(cleanup: Cleanup) -> Callable[[], None]:
    cleanups = _active_cleanups.get()
    if cleanups is None:
        return lambda: None
    cleanups.append(cleanup)

    def dispose() -> None:
        if cleanup in cleanups:
            cleanups.remove(cleanup)
    return dispose


@asynccontextmanager
async def tool_settlement_scope(publish: Callable[[ToolEvent], Awaitable[None]]):
    cleanups: list[Cleanup] = []
    token = _active_cleanups.set(cleanups)
    primary_error = None

    async def drain() -> None:
        for cleanup in reversed(tuple(cleanups)):
            events = await cleanup()
            for event in events:
                # Reuse the producer event identity if delivery failed
                # after commit. The normal repository deduplicates it.
                await publish(event)
            if cleanup in cleanups:
                cleanups.remove(cleanup)

    try:
        # The runner also drains before publishing a terminal input event:
        # publishing WAIT/DONE relinquishes the owner lease needed by cleanup.
        yield drain
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            try:
                await drain()
            except BaseException as error:
                if primary_error is None:
                    # New cancellation during otherwise normal cleanup
                    # must not turn a stopped task into successful return.
                    raise
                # Preserve the original delivery/cancellation exception.
                # Never include result payloads or exception text here.
                logger.warning("tool_settlement_delivery_failed error_type=%s", type(error).__name__)
        finally:
            _active_cleanups.reset(token)
