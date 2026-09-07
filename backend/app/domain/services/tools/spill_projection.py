from __future__ import annotations

import re
from typing import Any

from app.domain.models.spill import SpillArtifactChunk, SpillArtifactNotice
from app.domain.models.tool_result import ToolResult


SPILL_PROJECTION_KEY = "__ai_dataseek_spill_projection_v1"
SPILL_DURABLE_CONTENT_KEY = "__ai_dataseek_spill_durable_content_v1"

_SECRET_NORMALIZED_KEYS = frozenset({
    "apikey",
    "accesskey",
    "secret",
    "secretkey",
    "clientsecret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "cookie",
    "privatekey",
    "signature",
})
_SECRET_NORMALIZED_SUFFIXES = (
    "apikey",
    "accesskey",
    "secret",
    "secretkey",
    "clientsecret",
    "password",
    "passwd",
    "token",
    "credential",
    "cookie",
    "privatekey",
    "signature",
)
_HOST_PATH = re.compile(
    r"(?<![\w])(?:/(?!home/ubuntu(?:/|\b))[^\s\"'`|;&<>)]*|"
    r"[A-Za-z]:\\[^\s\"'`|;&<>)]*|\\\\[^\s\"'`|;&<>)]*)"
)
_CREDENTIAL_VALUE = re.compile(
    r"(?i)(\b(?:[a-z][a-z0-9]*[-_])*"
    r"(?:api[-_]?key|access[-_]?key|secret(?:[-_]?key)?|password|passwd|"
    r"[a-z0-9]*token|[a-z0-9]*credential|cookie|[a-z0-9]*secret|"
    r"[a-z0-9]*password|[a-z0-9]*apikey|private[-_]?key|signature|authorization)"
    r"[\"']?\s*[:=]\s*)(?:\"(?:\\.|[^\"\\])*\"?|'(?:\\.|[^'\\])*'?|[^\s,}\]]+)"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(\bAuthorization\s*[:=]\s*)(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
)
_BARE_AUTH_VALUE = re.compile(
    r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
)
_URL_CREDENTIALS = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/@:'\"]+):([^\s/@'\"]+)@"
)
_SCHEME_MARKER = "\ue000dataseek-spill-scheme\ue000"


def projected_tool_artifact(value: Any) -> Any:
    """Select the bounded public projection while keeping raw data process-local."""
    additional_kwargs = getattr(value, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and SPILL_PROJECTION_KEY in additional_kwargs:
        return additional_kwargs[SPILL_PROJECTION_KEY]
    return getattr(value, "artifact", value)


def spill_notice_from_result(value: Any) -> SpillArtifactNotice | None:
    """Return a validated notice from a transformed ToolResult-like value."""
    artifact = projected_tool_artifact(value)
    if hasattr(artifact, "model_dump"):
        artifact = artifact.model_dump(mode="python")
    if not isinstance(artifact, dict):
        return None
    data = artifact.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("spill"), dict):
        return None
    try:
        return SpillArtifactNotice.model_validate(data["spill"])
    except Exception:
        return None


def sanitize_spill_public_text(value: str) -> str:
    """Redact credentials and host paths from one durable spill preview.

    The raw output remains available only through the session-bound artifact
    capability.  A bounded preview is useful in a persisted event, but it must
    not turn Redis/Mongo into a second copy of host paths or credentials.
    Sandbox paths are deliberately retained because they are stable virtual
    paths rather than host filesystem locations.
    """

    text = _URL_CREDENTIALS.sub(r"\1[redacted credential]@", value)
    text = _AUTHORIZATION_VALUE.sub(
        r"\1\2 [redacted credential]",
        text,
    )
    # Head/tail previews can begin in the middle of an Authorization header.
    text = _BARE_AUTH_VALUE.sub(r"\1 [redacted credential]", text)
    text = _CREDENTIAL_VALUE.sub(r"\1[redacted credential]", text)
    # Protect URL separators from the filesystem-path expression.  The spill
    # locator is also a typed capability and must survive unchanged.
    for scheme in ("https://", "http://", "wss://", "ws://", "spill://"):
        text = text.replace(scheme, f"{scheme[:-2]}{_SCHEME_MARKER}")
    text = _HOST_PATH.sub("[protected path]", text)
    return text.replace(_SCHEME_MARKER, "//")


def sanitize_spill_public_data(value: Any) -> Any:
    """Recursively sanitize spill-derived content before durable persistence."""

    remaining = 2048

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining
        if depth >= 16 or remaining <= 0:
            return "[nested content omitted]"
        remaining -= 1
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="python")
        if isinstance(item, dict):
            sanitized: dict[str, Any] = {}
            for key, child in item.items():
                if remaining <= 0:
                    sanitized["__omitted__"] = "[additional content omitted]"
                    break
                key = str(key)
                normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
                safe_key = sanitize_spill_public_text(key)
                if normalized in _SECRET_NORMALIZED_KEYS or normalized.endswith(
                    _SECRET_NORMALIZED_SUFFIXES
                ):
                    remaining -= 1
                    sanitized[safe_key] = "[redacted credential]"
                else:
                    sanitized[safe_key] = visit(child, depth + 1)
            return sanitized
        if isinstance(item, (list, tuple)):
            sanitized_items = []
            for child in item:
                if remaining <= 0:
                    sanitized_items.append("[additional content omitted]")
                    break
                sanitized_items.append(visit(child, depth + 1))
            return sanitized_items
        if isinstance(item, str):
            return sanitize_spill_public_text(item)
        return item

    return visit(value, 0)


def durable_spill_read_projection(value: Any) -> ToolResult:
    """Persist page provenance only; raw page bytes belong to the current call."""

    artifact = projected_tool_artifact(value)
    if isinstance(artifact, ToolResult):
        succeeded, data = artifact.success, artifact.data
    elif isinstance(artifact, dict):
        succeeded, data = artifact.get("success") is True, artifact.get("data")
    else:
        succeeded, data = False, None
    if succeeded and isinstance(data, dict):
        try:
            page = SpillArtifactChunk.model_validate(data)
        except (ValueError, TypeError):
            pass
        else:
            metadata = page.model_dump(exclude={"content"})
            metadata["media_type"] = sanitize_spill_public_text(page.media_type)
            return ToolResult(
                success=True,
                message="Spill page delivered to the current model invocation; content omitted from history.",
                data=metadata,
            )
    return ToolResult(success=False, message="Spill artifact page could not be read.")


def durable_spill_memory_message(message: Any) -> Any:
    """Select a safe persistence copy without modifying active model context."""
    metadata = getattr(message, "additional_kwargs", {})
    content = metadata.get(SPILL_DURABLE_CONTENT_KEY) if isinstance(metadata, dict) else None
    if getattr(message, "type", None) != "tool" or not isinstance(content, str):
        return message
    return message.model_copy(update={
        "content": content, "artifact": None, "additional_kwargs": {},
        "response_metadata": {},
    })


def sanitize_spill_notice(notice: SpillArtifactNotice) -> SpillArtifactNotice:
    """Return a typed notice whose untrusted display strings are safe."""

    reference = notice.reference
    if reference is not None:
        reference = reference.model_copy(update={
            "media_type": sanitize_spill_public_text(reference.media_type),
            "retrieval_hint": sanitize_spill_public_text(
                reference.retrieval_hint
            ),
        })
    return notice.model_copy(update={
        "preview": sanitize_spill_public_text(notice.preview),
        "reference": reference,
    })


def durable_spill_result_projection(value: Any) -> Any:
    """Return the bounded spill projection with a persistence-safe preview.

    ``ToolMessage`` keeps the raw result process-local for deterministic
    completion hooks.  This boundary intentionally normalizes it to the public
    projection so the original artifact cannot be serialized accidentally.
    """

    artifact = projected_tool_artifact(value)
    notice = spill_notice_from_result(artifact)
    if notice is None:
        return artifact
    safe_notice = sanitize_spill_notice(notice)

    if isinstance(artifact, ToolResult):
        data = dict(artifact.data) if isinstance(artifact.data, dict) else {}
        data["spill"] = safe_notice.model_dump(mode="python")
        return artifact.model_copy(update={"data": data})

    if hasattr(artifact, "model_dump"):
        artifact = artifact.model_dump(mode="python")
    if not isinstance(artifact, dict):
        return artifact
    projected = dict(artifact)
    data = dict(projected.get("data") or {})
    data["spill"] = safe_notice.model_dump(mode="python")
    projected["data"] = data
    return projected
