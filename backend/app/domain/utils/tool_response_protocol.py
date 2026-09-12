"""Recognize unmistakable textual tool envelopes, never decode their arguments.

Only a native model tool-call channel is authority for tool dispatch. Detection
is deliberately anchored: quoted examples, fenced code, JSON data and prose
mentioning these tokens are not interpreted as protocol errors.
"""
from __future__ import annotations

import re
from typing import Any


_FORMAT_PREAMBLE = re.compile(r'^\{\s*"type"\s*:\s*"json_object"\s*\}\s*')
_DSML_ENVELOPE = re.compile(
    r"^<\s*[|｜]{1,2}DSML[|｜]{1,2}\s*(?:calls|function_calls|tool_calls|invoke)\b",
    re.IGNORECASE,
)
_XML_TOOL_ENVELOPE = re.compile(
    r"^<(?:tool_calls?|function_calls?)(?=\s|>)|^<function\s*=",
    re.IGNORECASE,
)


def textual_tool_envelope_reason(content: Any) -> str | None:
    """Classify a top-level envelope without extracting names or arguments."""
    if isinstance(content, list):
        # Standard text content blocks are still assistant prose. Do not scan
        # arbitrary dict values, JSON payloads, images or reasoning blocks.
        content = "\n".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    if not isinstance(content, str):
        return None
    text = content.lstrip()
    text = _FORMAT_PREAMBLE.sub("", text, count=1)
    if _DSML_ENVELOPE.match(text):
        return "textual_tool_envelope"
    if _XML_TOOL_ENVELOPE.match(text):
        return "textual_tool_envelope"
    return None


def terminal_response_correction(reason: str) -> str:
    """Host-authored recovery instructions contain no model-supplied payload."""
    if reason == "invalid_execution_result":
        detail = (
            "The last response is not a valid completed analysis result. If work remains, "
            "continue it with the actual bound tools. If the work is complete, return the "
            "required JSON object with success, a substantive result, and attachments. "
            "Use only observed execution evidence and actual generated files; do not invent "
            "results, paths, attachments, or upgrade a failed task to success. "
        )
    else:
        detail = (
            "The last response put a tool invocation in assistant text instead of the native "
            "tool-call channel. That textual invocation was NOT executed and is not evidence "
            "of work or file creation. If the operation is still needed, submit a native tool "
            "call using the bound tool schema; never output DSML, XML, or a textual tool envelope. "
            "If no tool is needed, provide the requested final response from verified evidence. "
        )
    return detail + (
        "The original task, authorization and execution state are unchanged. Do not replay "
        "previously completed writes or operations with unconfirmed side effects. Only the "
        "normal tool registry and execution pipeline may execute a native tool request."
    )
