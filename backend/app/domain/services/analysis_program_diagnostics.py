"""Content-only identities for trusted, read-only saved-code diagnostics."""
import posixpath

from app.domain.models.tool_result import ToolResult
from app.domain.services.execution_identity import private_identity_hmac


def _trusted_read_content(tool, call: dict, result) -> str | None:
    if call.get("name") != "file_read":
        return None
    from app.domain.services.tools.base import Tool
    from app.domain.services.tools.file import FileToolkit
    if (type(tool) is not Tool or type(getattr(tool, "toolkit", None)) is not FileToolkit
            or tool._tool is not FileToolkit.file_read
            or not any(candidate is tool for candidate in tool.toolkit.tools)
            or getattr(result, "tool_call_id", None) != call.get("id")):
        return None
    artifact = getattr(result, "artifact", None)
    args = call.get("args") or {}
    path = args.get("file")
    if (not isinstance(artifact, ToolResult) or artifact.success is not True
            or getattr(result, "status", None) == "error"
            or not isinstance(path, str) or args.get("sudo") not in (None, False)):
        return None
    data = artifact.data if isinstance(artifact.data, dict) else {}
    content, observed_path = data.get("content"), data.get("file")
    if (not isinstance(content, str) or not content.strip() or not isinstance(observed_path, str)
            or posixpath.normpath(observed_path) != posixpath.normpath(path)):
        return None
    return content


def program_diagnostic_read_digest(tool, call: dict, result) -> str | None:
    content = _trusted_read_content(tool, call, result)
    if content is None:
        return None
    # Neither call IDs nor requested line ranges create novelty when the
    # returned contents are identical. No source content enters guard state.
    return private_identity_hmac({"purpose": "program-diagnostic-read/v1", "content": content})


def program_diagnostic_read_lines(tool, call: dict, result) -> dict | None:
    """Private content-only line identities from a checked core file_read.

    These enable a diagnosis of the exact failed line, never a claim that code
    executed or that an input/result is correct. Negative slicing and truncated
    reads cannot be placed reliably and provide no line-level proof.
    """
    content = _trusted_read_content(tool, call, result)
    if content is None or content.endswith("(truncated)"):
        return None
    args = call.get("args") or {}
    start, end = args.get("start_line"), args.get("end_line")
    if any(value is not None and (type(value) is not int or value < 0) for value in (start, end)):
        return None
    return {"start_line": start or 0, "line_digests": [
        private_identity_hmac({"purpose": "program-diagnostic-line/v1", "content": line})
        for line in content.splitlines()]}
