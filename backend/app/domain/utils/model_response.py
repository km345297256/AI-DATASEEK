"""Pure model-response projections; raw messages remain authoritative history.

Transport content and a user-facing answer are different contracts. Reasoning,
refusals, images and native calls are not empty transport responses, but none of
their internal fields may become answer text through ``str(content)``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ModelResponseValidationError(ValueError):
    """A completed physical request returned an unusable response protocol."""


class ModelEmptyResponseError(ModelResponseValidationError):
    """A normal completion has no content; the existing bounded retry may run."""

    def __init__(self) -> None:
        super().__init__("Model returned an empty completed response")


class ModelIncompleteResponseError(ModelResponseValidationError):
    """An empty non-normal finish is not a retryable empty completion."""

    def __init__(self) -> None:
        super().__init__("Model response ended without a usable completion")


class ModelRefusedResponseError(ModelResponseValidationError):
    """An explicit provider refusal is never a transient empty completion."""

    def __init__(self) -> None:
        super().__init__("Model declined to provide a completion")


def model_answer_text(content: Any) -> str:
    """Extract only explicit answer text, preserving block order and fragments.

    Concatenation (not inserted whitespace) preserves JSON split across text
    blocks. Never recursively stringify reasoning, tool arguments, signatures,
    multimodal data or unknown extension blocks into an answer.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    return "".join(
        block if isinstance(block, str) else block["text"]
        for block in content
        if isinstance(block, str) or (
            isinstance(block, Mapping) and block.get("type") in ("text", "output_text")
            and isinstance(block.get("text"), str)
        )
    )


def model_finish_reason(message: Any) -> str | None:
    metadata = getattr(message, "response_metadata", None) or {}
    for key in ("finish_reason", "stop_reason", "status"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value.casefold()
    return None


_NORMAL_FINISHES = {None, "stop", "end_turn", "stop_sequence", "completed"}


def model_response_is_refusal(message: Any) -> bool:
    if model_finish_reason(message) in {"content_filter", "refusal", "blocked"}:
        return True
    extra = getattr(message, "additional_kwargs", None) or {}
    if extra.get("refusal"):
        return True
    content = getattr(message, "content", None)
    return isinstance(content, (list, tuple)) and any(
        isinstance(block, Mapping) and block.get("type") == "refusal"
        for block in content
    )


def validate_model_response(message: Any) -> None:
    """Reject empty completions without confusing calls or non-text content.

    This runs after physical usage settlement. Native malformed calls still
    belong to the tool parser; refusal, reasoning and image responses retain
    their own semantics. An empty length/error finish must never be retried as
    an ordinary empty stop. No provider payload enters exception text.
    """
    if model_response_is_refusal(message):
        raise ModelRefusedResponseError()
    if getattr(message, "tool_calls", None) or getattr(message, "invalid_tool_calls", None):
        return
    extra = getattr(message, "additional_kwargs", None) or {}
    if any(extra.get(key) for key in ("reasoning_content", "reasoning", "audio", "function_call")):
        return
    content = getattr(message, "content", None)
    if model_answer_text(content).strip():
        return
    if isinstance(content, (list, tuple)) and any(
        isinstance(block, Mapping) and block and block.get("type") not in ("text", "output_text")
        for block in content
    ):
        return
    if model_finish_reason(message) not in _NORMAL_FINISHES:
        raise ModelIncompleteResponseError()
    raise ModelEmptyResponseError()


def has_complete_answer_text(message: Any) -> bool:
    """A visual final answer needs text, not a call, refusal or partial finish."""
    return (
        not getattr(message, "tool_calls", None)
        and not getattr(message, "invalid_tool_calls", None)
        and not model_response_is_refusal(message)
        and model_finish_reason(message) in _NORMAL_FINISHES
        and bool(model_answer_text(getattr(message, "content", None)).strip())
    )
