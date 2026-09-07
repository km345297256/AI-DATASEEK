from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain.messages import ToolMessage
from langchain.tools import tool

from app.domain.external.spill import SpillArtifactStore
from app.domain.models.spill import (
    SpillArtifactChunk,
    SpillArtifactNotice,
    SpillArtifactOwner,
    SpillArtifactRef,
    SpillArtifactSaveRequest,
    SpillArtifactSource,
)
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.pipeline import (
    ToolExecutionContext,
    ToolExecutionInterceptor,
    opaque_log_identifier,
)
from app.domain.services.tools.spill_projection import SPILL_PROJECTION_KEY


logger = logging.getLogger(__name__)

SPILL_READ_TOOL_NAME = "spill_artifact_read"


def _utf8_prefix(value: bytes, budget: int) -> str:
    return value[:budget].decode("utf-8", errors="ignore")


def _utf8_suffix(value: bytes, budget: int) -> str:
    return value[-budget:].decode("utf-8", errors="ignore") if budget else ""


def retain_utf8_head_tail(value: str, budget: int) -> tuple[str, int]:
    """Return a byte-bounded preview and the number of original bytes retained."""
    encoded = value.encode("utf-8")
    if budget <= 0:
        return "", 0
    if len(encoded) <= budget:
        return value, len(encoded)

    separator = "\n...[middle omitted; full output is in the spill artifact]...\n"
    separator_bytes = separator.encode("utf-8")
    if len(separator_bytes) >= budget:
        preview = _utf8_prefix(encoded, budget)
        return preview, len(preview.encode("utf-8"))

    available = budget - len(separator_bytes)
    head_budget = (available * 2) // 3
    tail_budget = available - head_budget
    head = _utf8_prefix(encoded, head_budget)
    tail = _utf8_suffix(encoded, tail_budget)
    retained = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
    return f"{head}{separator}{tail}", retained


class SpillArtifactInterceptor(ToolExecutionInterceptor):
    """Store oversized final ToolMessage text and replace every public projection."""

    def __init__(
        self,
        store: SpillArtifactStore,
        *,
        owner: SpillArtifactOwner,
        max_inline_bytes: int = 50_000,
        max_artifact_bytes: int = 32 * 1024 * 1024,
        preview_bytes: int = 12_000,
        store_timeout_seconds: float = 5.0,
        excluded_tools: set[str] | frozenset[str] = frozenset({SPILL_READ_TOOL_NAME}),
    ) -> None:
        if max_inline_bytes < 1024:
            raise ValueError("spill max inline bytes must be at least 1024")
        if max_artifact_bytes < max_inline_bytes:
            raise ValueError("spill max artifact bytes must cover the inline limit")
        if preview_bytes < 0:
            raise ValueError("spill preview bytes must not be negative")
        if store_timeout_seconds <= 0:
            raise ValueError("spill store timeout must be positive")
        self._store = store
        self._owner = owner
        self._max_inline_bytes = max_inline_bytes
        self._max_artifact_bytes = max_artifact_bytes
        self._preview_bytes = min(preview_bytes, max_inline_bytes)
        self._store_timeout_seconds = store_timeout_seconds
        self._excluded_tools = frozenset(excluded_tools)
        self._pending_saves: set[asyncio.Task[SpillArtifactRef]] = set()

    def _track_save(self, task: asyncio.Task[SpillArtifactRef]) -> None:
        """Keep a timed-out provider write alive until it commits or rolls back.

        Cancelling an ``asyncio.to_thread``-backed object-store upload only
        cancels its waiter; the blocking write may still finish and otherwise
        become an untracked object. A detached task lets the store finish its
        durable record/rollback protocol while the tool result remains bounded.
        """
        self._pending_saves.add(task)

        def consume(completed: asyncio.Task[SpillArtifactRef]) -> None:
            self._pending_saves.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                logger.warning("Detached spill save was cancelled during shutdown")
            except Exception as error:
                logger.warning(
                    "Detached spill save failed error_type=%s",
                    type(error).__name__,
                )

        task.add_done_callback(consume)

    async def drain_pending_saves(self) -> None:
        """Finish detached writes before the runner closes or deletes its owner.

        The publication timeout does not cancel storage side effects. Cleanup
        must join those writes too, including when cleanup itself is cancelled.
        """
        cancellation: asyncio.CancelledError | None = None
        while self._pending_saves:
            joined = asyncio.gather(*self._pending_saves, return_exceptions=True)
            while True:
                try:
                    await asyncio.shield(joined)
                    break
                except asyncio.CancelledError as error:
                    cancellation = error
        if cancellation is not None:
            raise cancellation

    async def result(self, context: ToolExecutionContext, result: Any) -> Any:
        if context.tool_name in self._excluded_tools or not isinstance(result, ToolMessage):
            return result
        content = result.content
        if not isinstance(content, str):
            return result
        original_bytes = len(content.encode("utf-8"))
        # The threshold is strict: an exactly-sized result remains inline.
        if original_bytes <= self._max_inline_bytes:
            return result

        reference: SpillArtifactRef | None = None
        status = "unavailable"
        if original_bytes > self._max_artifact_bytes:
            logger.warning(
                "Tool result exceeded spill artifact ceiling tool=%s call=%s bytes=%d",
                opaque_log_identifier(context.tool_name, namespace="tool"),
                opaque_log_identifier(context.tool_call_id, namespace="call"),
                original_bytes,
            )
        else:
            try:
                save_task = asyncio.create_task(self._store.save_text(
                    SpillArtifactSaveRequest(
                        owner=self._owner,
                        source=SpillArtifactSource(
                            tool_name=context.tool_name,
                            tool_call_id=str(context.tool_call_id or ""),
                        ),
                        content=content,
                    ),
                ))
                self._track_save(save_task)
                reference = await asyncio.wait_for(
                    asyncio.shield(save_task),
                    timeout=self._store_timeout_seconds,
                )
                status = "stored"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Unlike a raw-output fallback, this cannot overflow Redis/Mongo or
                # leak an arbitrarily large provider response into SSE. The tool's
                # success/failure bit remains unchanged, while retrieval is marked
                # unavailable explicitly.
                logger.warning(
                    "Tool result spill failed tool=%s call=%s error_type=%s",
                    opaque_log_identifier(context.tool_name, namespace="tool"),
                    opaque_log_identifier(context.tool_call_id, namespace="call"),
                    type(error).__name__,
                )

        transformed = self._bounded_replacement(
            result,
            original_content=content,
            original_bytes=original_bytes,
            status=status,
            reference=reference,
        )
        context.metadata.update({
            "spill_status": status,
            "spill_original_bytes": original_bytes,
            "spill_locator_ref": (
                opaque_log_identifier(reference.locator, namespace="spill")
                if reference is not None
                else ""
            ),
        })
        return transformed

    def _bounded_replacement(
        self,
        result: ToolMessage,
        *,
        original_content: str,
        original_bytes: int,
        status: str,
        reference: SpillArtifactRef | None,
    ) -> ToolMessage:
        artifact = getattr(result, "artifact", None)
        if isinstance(artifact, dict):
            succeeded = artifact.get("success") is not False
        else:
            succeeded = getattr(artifact, "success", True) is not False

        preview_budget = self._preview_bytes
        while True:
            preview, retained_bytes = retain_utf8_head_tail(
                original_content,
                preview_budget,
            )
            notice = SpillArtifactNotice(
                status=status,
                reference=reference,
                preview=preview,
                original_bytes=original_bytes,
                retained_bytes=retained_bytes,
                omitted_bytes=max(0, original_bytes - retained_bytes),
            )
            projected = ToolResult(
                success=succeeded,
                message=(
                    "Tool output exceeded the inline byte limit and was stored as a spill artifact."
                    if status == "stored"
                    else "Tool output exceeded the inline byte limit, but durable spill storage was unavailable."
                ),
                data={"spill": notice.model_dump(mode="python")},
            )
            serialized = projected.model_dump_json()
            serialized_bytes = len(serialized.encode("utf-8"))
            if serialized_bytes <= self._max_inline_bytes:
                additional_kwargs = dict(result.additional_kwargs or {})
                additional_kwargs[SPILL_PROJECTION_KEY] = projected
                return result.model_copy(update={
                    "content": serialized,
                    # Deterministic completion hooks keep the original value
                    # for this invocation. Callers must use
                    # ``projected_tool_artifact`` for persistence or SSE.
                    "artifact": result.artifact,
                    "additional_kwargs": additional_kwargs,
                })
            if preview_budget == 0:
                # Constructor validation guarantees the fixed notice fits sane
                # production budgets; keep this explicit for custom policies.
                raise RuntimeError("Spill notice does not fit the inline byte budget")
            preview_budget = max(
                0,
                preview_budget - (serialized_bytes - self._max_inline_bytes) - 64,
            )


class SpillArtifactToolkit(BaseToolkit):
    """Session-bound, paginated retrieval for opaque spill artifacts."""

    name: str = "spill"

    def __init__(
        self,
        store: SpillArtifactStore,
        *,
        owner: SpillArtifactOwner,
        max_inline_bytes: int = 50_000,
    ) -> None:
        if max_inline_bytes < 1024:
            raise ValueError("spill read inline budget must be at least 1024")
        super().__init__()
        self._store = store
        self._owner = owner
        self._max_inline_bytes = max_inline_bytes

    def _bounded_chunk_result(self, chunk: SpillArtifactChunk) -> ToolResult:
        """Fit a page after JSON escaping, not just before serialization.

        Control characters can expand to six bytes in JSON.  The read tool is
        excluded from recursive spilling, so its own result must enforce the
        same public byte ceiling explicitly.
        """
        encoded = chunk.content.encode("utf-8")

        def build(byte_budget: int) -> tuple[ToolResult, int]:
            content = encoded[:byte_budget].decode("utf-8", errors="ignore")
            consumed = len(content.encode("utf-8"))
            next_byte = chunk.start_byte + consumed
            bounded = chunk.model_copy(update={
                "content": content,
                "next_byte": next_byte,
                "eof": next_byte >= chunk.total_bytes,
            })
            return (
                ToolResult(success=True, data=bounded.model_dump(mode="python")),
                consumed,
            )

        complete, _ = build(len(encoded))
        if len(complete.model_dump_json().encode("utf-8")) <= self._max_inline_bytes:
            return complete

        empty, _ = build(0)
        if len(empty.model_dump_json().encode("utf-8")) > self._max_inline_bytes:
            raise RuntimeError("Spill read envelope does not fit the inline byte budget")

        best = empty
        best_consumed = 0
        lower = 1
        upper = len(encoded)
        while lower <= upper:
            midpoint = (lower + upper) // 2
            candidate, consumed = build(midpoint)
            if len(candidate.model_dump_json().encode("utf-8")) <= self._max_inline_bytes:
                if consumed >= best_consumed:
                    best = candidate
                    best_consumed = consumed
                lower = midpoint + 1
            else:
                upper = midpoint - 1
        if encoded and best_consumed == 0:
            raise RuntimeError("Spill read page cannot make progress within its byte budget")
        return best

    @tool(parse_docstring=True)
    async def spill_artifact_read(
        self,
        locator: str,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> ToolResult:
        """Read one bounded page from a spilled tool result in this session.

        Args:
            locator: Opaque spill://artifact/... locator returned by a tool.
            offset: UTF-8 byte offset; use the previous page's next_byte value.
            max_bytes: Optional requested page size, capped by server policy.
        """
        try:
            chunk = await self._store.read_text(
                locator,
                self._owner,
                offset=offset,
                max_bytes=max_bytes,
            )
            return self._bounded_chunk_result(chunk)
        except FileNotFoundError:
            return ToolResult(
                success=False,
                message="Spill artifact was not found or has expired.",
                data={"error": "spill_artifact_not_found"},
            )
        except PermissionError:
            return ToolResult(
                success=False,
                message="Spill artifact is not available in this session.",
                data={"error": "spill_artifact_access_denied"},
            )
        except ValueError as error:
            return ToolResult(
                success=False,
                message=str(error),
                data={"error": "spill_artifact_invalid_request"},
            )
        except Exception as error:
            logger.warning(
                "Spill artifact read failed locator=%s error_type=%s",
                opaque_log_identifier(locator, namespace="spill"),
                type(error).__name__,
            )
            return ToolResult(
                success=False,
                message="Spill artifact could not be read right now.",
                data={"error": "spill_artifact_read_failed"},
            )
