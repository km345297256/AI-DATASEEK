"""Bounded, read-only recovery for deterministic stdout protocol consumers."""
import asyncio
import re
from typing import Any


MAX_CONTRACT_OUTPUT_BYTES = 2 * 1024 * 1024


class ShellOutputUnavailable(ValueError):
    def __init__(self, code: str = "shell_output_unavailable"):
        self.code = code
        super().__init__("Complete command output is unavailable; the command was not repeated")


async def recover_shell_output(sandbox, session_id: str, data: dict[str, Any]) -> Any:
    """Recover only a completed protocol result, never ordinary model polling.

    Legacy adapters have no output metadata and keep their previous behavior.
    The byte ceiling applies before the first read and after every page. Every
    request pins both the shell's existing operation receipt and output identity.
    """
    output = data.get("output", "")
    metadata = data.get("output_metadata")
    if metadata is None:
        return output
    if not isinstance(metadata, dict) or metadata.get("stream_complete") is not True:
        raise ShellOutputUnavailable()
    total = metadata.get("total_bytes")
    if type(total) is not int or total < 0:
        raise ShellOutputUnavailable()
    if total > MAX_CONTRACT_OUTPUT_BYTES:
        raise ShellOutputUnavailable("tool_output_size_limit")
    if metadata.get("preview_truncated") is False:
        if not isinstance(output, str) or len(output.encode("utf-8")) != total:
            raise ShellOutputUnavailable()
        return output
    output_id = metadata.get("output_id")
    if (metadata.get("log_status") != "available" or not isinstance(output_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", output_id) is None):
        raise ShellOutputUnavailable()
    cursor, parts = 0, []
    # One result-observation deadline, never a total task runtime budget.
    async with asyncio.timeout(30):
        for _ in range(512):
            result = await sandbox.view_shell(session_id, output_id=output_id, cursor=cursor, max_bytes=16384)
            value = result.data if isinstance(result.data, dict) else {}
            page, current = value.get("output_page"), value.get("output_metadata")
            text = value.get("output")
            if (not result.success or not isinstance(page, dict) or not isinstance(current, dict)
                    or not isinstance(text, str) or len(text.encode("utf-8")) > 16384
                    or current.get("output_id") != output_id or current.get("total_bytes") != total
                    or current.get("log_status") != "available" or current.get("stream_complete") is not True
                    or page.get("lossy") is not False or page.get("source") != "spool"
                    or type(page.get("start")) is not int or page["start"] != cursor
                    or type(page.get("cursor")) is not int or page["cursor"] != cursor):
                raise ShellOutputUnavailable()
            expected = cursor + len(text.encode("utf-8"))
            if type(page.get("next_cursor")) is not int or page["next_cursor"] != expected or not cursor < expected <= total:
                raise ShellOutputUnavailable()
            parts.append(text)
            cursor = expected
            if cursor == total:
                if page.get("eof") is not True:
                    raise ShellOutputUnavailable()
                return "".join(parts)
            if page.get("eof") is not False:
                raise ShellOutputUnavailable()
    raise ShellOutputUnavailable()
