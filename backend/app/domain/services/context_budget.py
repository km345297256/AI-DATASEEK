from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, Mapping, Sequence

from langchain.messages import AIMessage, AnyMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.services.execution_identity import private_identity_hmac


TOKEN_ESTIMATOR_VERSION = "utf8_bytes_div3_v1"
TOKEN_COUNTS_ARE_ESTIMATES = True

_MESSAGE_OVERHEAD_TOKENS = 8
_TOOL_SCHEMA_OVERHEAD_TOKENS = 16
_RESPONSE_FORMAT_OVERHEAD_TOKENS = 8
_UNKNOWN_BLOCK_MAX_BYTES = 1_024
_MAX_RECORDS_PER_KIND = 128
_SPILL_LOCATOR = re.compile(r"spill://artifact/[0-9a-f]{32}")
_HMAC_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_KNOWN_MESSAGE_TYPES = frozenset({
    "ai",
    "chat",
    "developer",
    "function",
    "human",
    "system",
    "tool",
})


class ContextBudgetFailure(str, Enum):
    FIXED_CONTEXT_TOO_LARGE = "fixed_context_too_large"
    INVALID_MESSAGE = "invalid_message"
    PRIVATE_DIGEST_UNAVAILABLE = "private_digest_unavailable"
    UNKNOWN_CONTENT_BLOCK = "unknown_content_block"


class ContextBudgetExceeded(RuntimeError):
    """A model request cannot be made without dropping protected context."""

    code = "context_budget_exceeded"
    retryable = False

    def __init__(self, reason: ContextBudgetFailure) -> None:
        self.reason = ContextBudgetFailure(reason)
        super().__init__(f"{self.code}: {self.reason.value}")


class CompactionRecord(BaseModel):
    """Private-content-free audit metadata for one context transformation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["tool_text_compacted", "completed_exchange_omitted"]
    start_message_index: int = Field(strict=True, ge=0)
    end_message_index: int = Field(strict=True, ge=1)
    message_count: int = Field(strict=True, ge=1)
    unit_count: int = Field(strict=True, ge=1)
    input_tokens_before: int = Field(strict=True, ge=0)
    input_tokens_after: int = Field(strict=True, ge=0)
    content_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_range(self) -> "CompactionRecord":
        if self.end_message_index <= self.start_message_index:
            raise ValueError("context compaction range must be non-empty")
        if self.message_count > self.end_message_index - self.start_message_index:
            raise ValueError("context compaction count exceeds its source range")
        return self


@dataclass(frozen=True, slots=True)
class PreparedContext:
    """One provider-ready copy with local, explicitly estimated token counts."""

    messages: list[AnyMessage]
    input_tokens_before: int
    input_tokens_after: int
    tool_tokens: int
    input_limit: int
    records: tuple[CompactionRecord, ...]
    estimator_version: str = TOKEN_ESTIMATOR_VERSION
    is_estimate: Literal[True] = True


@dataclass(frozen=True, slots=True)
class _Exchange:
    start: int
    end: int
    tool_indices: tuple[int, ...]


def _ceil_div3(byte_count: int) -> int:
    return (byte_count + 2) // 3


def _utf8_tokens(value: str) -> int:
    return _ceil_div3(len(value.encode("utf-8", errors="strict")))


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise ContextBudgetExceeded(ContextBudgetFailure.UNKNOWN_CONTENT_BLOCK) from None


def _estimate_unknown_block(value: Any) -> int:
    encoded = _canonical_json_bytes(value)
    if len(encoded) > _UNKNOWN_BLOCK_MAX_BYTES:
        raise ContextBudgetExceeded(ContextBudgetFailure.UNKNOWN_CONTENT_BLOCK)
    # Unknown provider blocks receive a steeper reserve than recognized text.
    return (len(encoded) + 1) // 2 + 8


def _image_metadata(block: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key, value in block.items():
        if key in {"data", "base64", "image", "image_url", "source", "url"}:
            continue
        metadata[str(key)] = value
    return metadata


def _estimate_content(content: Any, *, image_tokens: int, image_estimator: Callable | None = None) -> int:
    if isinstance(content, str):
        return _utf8_tokens(content)
    if content is None:
        return 0
    if isinstance(content, Mapping):
        blocks: Sequence[Any] = (content,)
    elif isinstance(content, (list, tuple)):
        blocks = content
    else:
        return _estimate_unknown_block(content)

    total = 0
    for block in blocks:
        if isinstance(block, str):
            total += _utf8_tokens(block)
            continue
        if not isinstance(block, Mapping):
            total += _estimate_unknown_block(block)
            continue
        block_type = block.get("type")
        if block_type in _TEXT_BLOCK_TYPES and isinstance(block.get("text"), str):
            total += _utf8_tokens(block["text"]) + 4
            metadata = {key: value for key, value in block.items() if key != "text"}
            total += _ceil_div3(len(_canonical_json_bytes(metadata)))
            continue
        if block_type in _IMAGE_BLOCK_TYPES:
            # Image payload bytes and URLs are not text-tokenized. Each image has
            # its own conservative reservation, plus only its small metadata.
            total += _require_positive_int(image_estimator(block) if image_estimator else image_tokens, "image_tokens") + 8
            total += _ceil_div3(len(_canonical_json_bytes(_image_metadata(block))))
            continue
        total += _estimate_unknown_block(block)
    return total


def _message_type(message: Any) -> str:
    value = getattr(message, "type", None)
    if not isinstance(value, str) or value not in _KNOWN_MESSAGE_TYPES:
        raise ContextBudgetExceeded(ContextBudgetFailure.INVALID_MESSAGE)
    return value


def _tool_calls(message: Any) -> tuple[Mapping[str, Any], ...]:
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return ()
    if not isinstance(calls, (list, tuple)) or any(
        not isinstance(call, Mapping) for call in calls
    ):
        raise ContextBudgetExceeded(ContextBudgetFailure.INVALID_MESSAGE)
    return tuple(calls)


def _estimate_message(message: Any, *, image_tokens: int, image_estimator: Callable | None = None) -> int:
    message_type = _message_type(message)
    total = _MESSAGE_OVERHEAD_TOKENS + _utf8_tokens(message_type)
    total += _estimate_content(getattr(message, "content", None), image_tokens=image_tokens, image_estimator=image_estimator)

    name = getattr(message, "name", None)
    if isinstance(name, str):
        total += _utf8_tokens(name) + 2
    tool_call_id = getattr(message, "tool_call_id", None)
    if isinstance(tool_call_id, str):
        total += _utf8_tokens(tool_call_id) + 2

    calls = _tool_calls(message)
    for call in calls:
        total += _ceil_div3(len(_canonical_json_bytes(call))) + 12

    additional = getattr(message, "additional_kwargs", None)
    if additional:
        total += _ceil_div3(len(_canonical_json_bytes(additional))) + 4
    return total


def _normalize_tool_schema(tool: Any) -> Any:
    if isinstance(tool, Mapping):
        return tool
    if isinstance(tool, BaseModel):
        return tool.model_dump(mode="json")

    name = getattr(tool, "name", None)
    description = getattr(tool, "description", None)
    args_schema = getattr(tool, "args_schema", None)
    if isinstance(name, str) and args_schema is not None:
        schema_factory = getattr(args_schema, "model_json_schema", None)
        if callable(schema_factory):
            parameters = schema_factory()
        elif isinstance(args_schema, Mapping):
            parameters = args_schema
        else:
            raise ContextBudgetExceeded(ContextBudgetFailure.UNKNOWN_CONTENT_BLOCK)
        return {
            "name": name,
            "description": description if isinstance(description, str) else "",
            "parameters": parameters,
        }

    dump = getattr(tool, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:
            raise ContextBudgetExceeded(
                ContextBudgetFailure.UNKNOWN_CONTENT_BLOCK
            ) from None
    raise ContextBudgetExceeded(ContextBudgetFailure.UNKNOWN_CONTENT_BLOCK)


def estimate_tool_tokens(tool_schemas: Sequence[Any]) -> int:
    """Estimate serialized provider tool definitions without a network tokenizer."""

    total = 0
    for tool in tool_schemas:
        encoded = _canonical_json_bytes(_normalize_tool_schema(tool))
        total += _ceil_div3(len(encoded)) + _TOOL_SCHEMA_OVERHEAD_TOKENS
    return total


def _estimate_response_format(response_format: Any) -> int:
    if response_format is None:
        return 0
    if isinstance(response_format, str):
        return _utf8_tokens(response_format) + _RESPONSE_FORMAT_OVERHEAD_TOKENS
    return (
        _ceil_div3(len(_canonical_json_bytes(response_format)))
        + _RESPONSE_FORMAT_OVERHEAD_TOKENS
    )


def estimate_context_tokens(
    messages: Sequence[AnyMessage],
    *,
    tool_schemas: Sequence[Any] = (),
    response_format: Any = None,
    image_tokens: int = 1_024,
    image_estimator: Callable | None = None,
) -> tuple[int, int]:
    """Return ``(total_input_estimate, tool_schema_estimate)`` locally."""

    _require_positive_int(image_tokens, "image_tokens")
    tool_tokens = estimate_tool_tokens(tool_schemas)
    message_tokens = 3 + sum(
        _estimate_message(message, image_tokens=image_tokens, image_estimator=image_estimator) for message in messages
    )
    return message_tokens + tool_tokens + _estimate_response_format(response_format), tool_tokens


def _copy_message(message: Any) -> AnyMessage:
    copier = getattr(message, "model_copy", None)
    if not callable(copier):
        raise ContextBudgetExceeded(ContextBudgetFailure.INVALID_MESSAGE)
    try:
        return copier(deep=True)
    except Exception:
        raise ContextBudgetExceeded(ContextBudgetFailure.INVALID_MESSAGE) from None


def _complete_exchanges(messages: Sequence[AnyMessage]) -> tuple[_Exchange, ...]:
    exchanges: list[_Exchange] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if _message_type(message) != "ai":
            index += 1
            continue
        calls = _tool_calls(message)
        if not calls:
            index += 1
            continue
        expected_ids = [call.get("id") for call in calls]
        if (
            any(not isinstance(call_id, str) or not call_id for call_id in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)
        ):
            index += 1
            continue

        cursor = index + 1
        tool_indices: list[int] = []
        reply_ids: list[str] = []
        while cursor < len(messages) and _message_type(messages[cursor]) == "tool":
            reply_id = getattr(messages[cursor], "tool_call_id", None)
            if not isinstance(reply_id, str) or reply_id not in expected_ids:
                break
            tool_indices.append(cursor)
            reply_ids.append(reply_id)
            cursor += 1
        if len(reply_ids) == len(expected_ids) and set(reply_ids) == set(expected_ids):
            exchanges.append(_Exchange(
                start=index,
                end=cursor,
                tool_indices=tuple(tool_indices),
            ))
            index = cursor
            continue
        index += 1
    return tuple(exchanges)


def _private_digest(value: Any) -> str:
    try:
        digest = private_identity_hmac(value)
    except Exception:
        raise ContextBudgetExceeded(
            ContextBudgetFailure.PRIVATE_DIGEST_UNAVAILABLE
        ) from None
    if not isinstance(digest, str) or _HMAC_PATTERN.fullmatch(digest) is None:
        raise ContextBudgetExceeded(ContextBudgetFailure.PRIVATE_DIGEST_UNAVAILABLE)
    return digest


def _truncate_utf8(encoded: bytes, limit: int, *, tail: bool = False) -> str:
    if limit <= 0:
        return ""
    selected = encoded[-limit:] if tail else encoded[:limit]
    return selected.decode("utf-8", errors="ignore")


def _compact_tool_text(content: str, *, max_tokens: int) -> tuple[str, str]:
    encoded = content.encode("utf-8", errors="strict")
    digest = _private_digest({
        "purpose": "context-budget/tool-text/v1",
        "content": content,
    })
    references = tuple(dict.fromkeys(_SPILL_LOCATOR.findall(content)))
    marker = (
        "[Historical tool output compacted for the model context; "
        f"original_utf8_bytes={len(encoded)}; content_hmac={digest}]\n"
    )
    reference_text = ""
    if references:
        reference_text = "Preserved spill references:\n" + "\n".join(
            f"- {reference}" for reference in references
        ) + "\n"
    omitted = "\n...[historical tool output omitted]...\n"
    target_bytes = max_tokens * 3
    fixed_bytes = len((marker + reference_text + omitted).encode("utf-8"))
    if fixed_bytes >= target_bytes:
        raise ContextBudgetExceeded(ContextBudgetFailure.FIXED_CONTEXT_TOO_LARGE)
    available = target_bytes - fixed_bytes
    head_bytes = (available * 2) // 3
    tail_bytes = available - head_bytes
    compacted = (
        marker
        + reference_text
        + _truncate_utf8(encoded, head_bytes)
        + omitted
        + _truncate_utf8(encoded, tail_bytes, tail=True)
    )
    if len(compacted.encode("utf-8")) >= len(encoded):
        raise ContextBudgetExceeded(ContextBudgetFailure.FIXED_CONTEXT_TOO_LARGE)
    return compacted, digest


def _message_private_payload(message: Any) -> dict[str, Any]:
    return {
        "type": _message_type(message),
        "content": getattr(message, "content", None),
        "name": getattr(message, "name", None),
        "tool_call_id": getattr(message, "tool_call_id", None),
        "tool_calls": list(_tool_calls(message)),
        "additional_kwargs": getattr(message, "additional_kwargs", None),
    }


def _omission_marker(exchange: _Exchange, digest: str) -> AIMessage:
    return AIMessage(content=(
        "[Historical completed assistant/tool exchange omitted from this model "
        f"context; messages={exchange.end - exchange.start}; "
        f"content_hmac={digest}. This bookkeeping marker has no system authority.]"
    ))


def _append_record(
    records: list[CompactionRecord],
    record: CompactionRecord,
) -> None:
    matching = [
        index for index, existing in enumerate(records) if existing.kind == record.kind
    ]
    if len(matching) < _MAX_RECORDS_PER_KIND:
        records.append(record)
        return
    index = matching[-1]
    existing = records[index]
    folded_hmac = _private_digest({
        "purpose": "context-budget/compaction-record-fold/v1",
        "kind": record.kind,
        "digests": [existing.content_hmac, record.content_hmac],
    })
    records[index] = CompactionRecord(
        kind=record.kind,
        start_message_index=min(
            existing.start_message_index,
            record.start_message_index,
        ),
        end_message_index=max(
            existing.end_message_index,
            record.end_message_index,
        ),
        message_count=existing.message_count + record.message_count,
        unit_count=existing.unit_count + record.unit_count,
        input_tokens_before=existing.input_tokens_before,
        input_tokens_after=record.input_tokens_after,
        content_hmac=folded_hmac,
    )


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def prepare_context(
    messages: Sequence[AnyMessage],
    *,
    tool_schemas: Sequence[Any] = (),
    response_format: Any = None,
    capacity_tokens: int = 65_536,
    max_output_tokens: int = 8_192,
    safety_tokens: int = 2_048,
    image_tokens: int = 1_024,
    image_estimator: Callable | None = None,
    max_tool_text_tokens: int = 2_048,
) -> PreparedContext:
    """Prepare a paired, provider-ready context without mutating its source.

    Counts are deliberately named estimates. The local versioned estimator uses
    ``ceil(UTF-8 bytes / 3)`` plus structural overhead, fixed image reservations,
    and the caller-provided safety reserve. It never calls a model or tokenizer.
    """

    capacity_tokens = _require_positive_int(capacity_tokens, "capacity_tokens")
    max_output_tokens = _require_positive_int(
        max_output_tokens,
        "max_output_tokens",
    )
    safety_tokens = _require_non_negative_int(safety_tokens, "safety_tokens")
    image_tokens = _require_positive_int(image_tokens, "image_tokens")
    max_tool_text_tokens = _require_positive_int(
        max_tool_text_tokens,
        "max_tool_text_tokens",
    )
    input_limit = capacity_tokens - max_output_tokens - safety_tokens
    if input_limit < 1:
        raise ContextBudgetExceeded(ContextBudgetFailure.FIXED_CONTEXT_TOO_LARGE)

    schemas = tuple(tool_schemas)
    source_messages = tuple(messages)
    copied_messages = [_copy_message(message) for message in source_messages]
    # The versioned estimator is additive. Keep request-local contributions,
    # not a global content cache: mutable provider messages, credentials and
    # catalog revisions must never reuse stale estimates across requests.
    tool_tokens = estimate_tool_tokens(schemas)
    message_tokens = [
        _estimate_message(message, image_tokens=image_tokens, image_estimator=image_estimator)
        for message in copied_messages
    ]
    input_tokens_before = (
        3 + sum(message_tokens) + tool_tokens
        + _estimate_response_format(response_format)
    )
    current_tokens = input_tokens_before
    records: list[CompactionRecord] = []
    if current_tokens <= input_limit:
        return PreparedContext(
            messages=copied_messages,
            input_tokens_before=input_tokens_before,
            input_tokens_after=current_tokens,
            tool_tokens=tool_tokens,
            input_limit=input_limit,
            records=(),
        )

    exchanges = _complete_exchanges(copied_messages)

    # Tool text may be compacted only when its assistant call and every reply
    # form a complete unit. This is also allowed within the current user turn.
    compressible_indices = [
        index for exchange in exchanges for index in exchange.tool_indices
    ]
    for original_index in compressible_indices:
        if current_tokens <= input_limit:
            break
        original_message = copied_messages[original_index]
        content = getattr(original_message, "content", None)
        if not isinstance(content, str) or _utf8_tokens(content) <= max_tool_text_tokens:
            continue
        compacted, digest = _compact_tool_text(
            content,
            max_tokens=max_tool_text_tokens,
        )
        before = current_tokens
        candidate_message = original_message.model_copy(
            update={"content": compacted},
            deep=True,
        )
        candidate_tokens = _estimate_message(candidate_message, image_tokens=image_tokens, image_estimator=image_estimator)
        current_tokens += candidate_tokens - message_tokens[original_index]
        if current_tokens >= before:
            # A pathological marker/reference set must not make context larger.
            current_tokens = before
            continue
        copied_messages[original_index] = candidate_message
        message_tokens[original_index] = candidate_tokens
        _append_record(records, CompactionRecord(
            kind="tool_text_compacted",
            start_message_index=original_index,
            end_message_index=original_index + 1,
            message_count=1,
            unit_count=1,
            input_tokens_before=before,
            input_tokens_after=current_tokens,
            content_hmac=digest,
        ))

    # Old completed exchanges may be omitted as indivisible units, including
    # earlier exchanges after the latest user request. Always retain the newest
    # complete exchange as evidence and never touch pending/unpaired calls.
    latest_exchange = exchanges[-1] if exchanges else None
    omitted_indices: set[int] = set()
    omission_markers: dict[int, AnyMessage] = {}
    for exchange in exchanges:
        if current_tokens <= input_limit:
            break
        if exchange is latest_exchange:
            continue
        # _complete_exchanges returns disjoint ranges in source order. Retain
        # those indices until final assembly so omissions need no repeated
        # whole-history scan, copy or token/schema re-estimation.
        unit_messages = copied_messages[exchange.start:exchange.end]
        digest = _private_digest({
            "purpose": "context-budget/completed-exchange/v1",
            "messages": [
                _message_private_payload(message) for message in unit_messages
            ],
        })
        marker = _omission_marker(exchange, digest)
        candidate_tokens = (
            current_tokens - sum(message_tokens[exchange.start:exchange.end])
            + _estimate_message(marker, image_tokens=image_tokens, image_estimator=image_estimator)
        )
        if candidate_tokens >= current_tokens:
            continue
        before = current_tokens
        omitted_indices.update(range(exchange.start, exchange.end))
        omission_markers[exchange.start] = marker
        current_tokens = candidate_tokens
        _append_record(records, CompactionRecord(
            kind="completed_exchange_omitted",
            start_message_index=exchange.start,
            end_message_index=exchange.end,
            message_count=exchange.end - exchange.start,
            unit_count=1,
            input_tokens_before=before,
            input_tokens_after=current_tokens,
            content_hmac=digest,
        ))

    if current_tokens > input_limit:
        raise ContextBudgetExceeded(ContextBudgetFailure.FIXED_CONTEXT_TOO_LARGE)

    return PreparedContext(
        messages=[
            omission_markers[index] if index in omission_markers else message
            for index, message in enumerate(copied_messages)
            if index not in omitted_indices or index in omission_markers
        ],
        input_tokens_before=input_tokens_before,
        input_tokens_after=current_tokens,
        tool_tokens=tool_tokens,
        input_limit=input_limit,
        records=tuple(records),
    )


__all__ = [
    "CompactionRecord",
    "ContextBudgetExceeded",
    "ContextBudgetFailure",
    "PreparedContext",
    "TOKEN_COUNTS_ARE_ESTIMATES",
    "TOKEN_ESTIMATOR_VERSION",
    "estimate_context_tokens",
    "estimate_tool_tokens",
    "prepare_context",
]
