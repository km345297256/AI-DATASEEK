"""Reject host memory receipts used as the entire body of a core file write.

This narrow guard does not interpret ordinary summaries, logs or embedded
examples. A compaction receipt describes an earlier write; it is not its bytes.
"""
from __future__ import annotations

import re

from app.domain.services.analysis_protocol_recovery import _core_file_method


_RECEIPT_ONLY = re.compile(
    r"\s*(?:\[(?:content persisted successfully|replacement text persisted); "
    r"(?:0|[1-9][0-9]*) bytes; sha256:[0-9a-f]{16}\]\s*)+"
)


def compacted_content_write_reason(tool, call: dict) -> str | None:
    """Use resolved core identity; same-named plugins have their own contract."""
    name = call.get("name")
    if not _core_file_method(tool, name):
        return None
    args = call.get("args")
    if not isinstance(args, dict):
        return None
    # A literal old_str may be needed to remove a previously corrupted marker.
    # Only the replacement value is new content, and empty deletion is valid.
    value = args.get("content" if name == "file_write" else "new_str")
    if not isinstance(value, str) or _RECEIPT_ONLY.fullmatch(value) is None:
        return None
    return (
        "This write was NOT executed. Its entire new content consists of host memory compaction receipts, "
        "not the original file bytes. Supply the actual complete text to write or replace; if needed, "
        "read the existing authorized source and use its real content. Do not copy or invent receipt "
        "markers, repeatedly append them, or replay an already successful operation. Preserve existing outputs."
    )
