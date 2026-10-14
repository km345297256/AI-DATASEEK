"""Decode result objects without interpreting assistant text as tool calls."""
import json
from typing import Any

from app.domain.utils.robust_json_parser import parse_json_lenient
from app.domain.utils.tool_response_protocol import textual_tool_envelope_reason


def parse_execution_response(raw: str, *, summary: bool = False) -> Any:
    if textual_tool_envelope_reason(raw):
        raise ValueError("Textual tool envelopes are not execution results")
    try:
        return parse_json_lenient(raw)
    except Exception:
        # Some providers prepend narration to an otherwise complete JSON
        # receipt. Decode exactly one complete trailing object. Never repair
        # truncated JSON, select among multiple objects, or extract tool args.
        start = raw.find('{')
        if start <= 0 or any(char in raw[:start] for char in '[]`<>'):
            raise ValueError("No unambiguous trailing result object") from None
        parsed, end = json.JSONDecoder().raw_decode(raw, start)
        required = {'message', 'attachments'} if summary else {'success', 'result', 'attachments'}
        if (raw[end:].strip() or not isinstance(parsed, dict)
                or not required.issubset(parsed)
                or {'tool_calls', 'function_call', 'arguments'} & parsed.keys()):
            raise ValueError("Not a complete terminal result object") from None
        return parsed
