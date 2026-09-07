"""Public, bounded schema for declarative tool-result presentation.

The descriptor is data only.  It deliberately cannot name a Vue component,
contain HTML, or carry executable JavaScript.  Tool manifests and tool results
are extension input, so the API boundary normalizes them before they reach an
SSE client.
"""

from __future__ import annotations

import json
from itertools import islice
import math
import re
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field


ToolPresentationKind = Literal[
    "auto",
    "generic",
    "table",
    "chart",
    "map",
    "image",
    "artifact",
    "log",
]
ToolChartType = Literal["line", "bar", "area", "scatter"]
ToolLogLevel = Literal["debug", "info", "warning", "error"]

_MAX_DATA_DEPTH = 7
_MAX_COLLECTION_ITEMS = 200
_MAX_OBJECT_FIELDS = 80
_MAX_STRING_LENGTH = 20_000
_MAX_DATA_NODES = 4_000
# This is a byte limit for the complete compact-JSON representation of
# ``ToolPresentation.data``.  Counting only string values is insufficient:
# repeated object keys, quotes, escapes, commas and brackets can otherwise
# make the SSE payload substantially larger than the advertised limit.
MAX_TOOL_PRESENTATION_DATA_BYTES = 256_000
_MAX_COLUMNS = 32
_MAX_SERIES = 16

_SIZE_LIMIT_MARKER = "[content omitted: size limit]"
_NESTING_LIMIT_MARKER = "[content omitted: nesting limit]"

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
    r"(?i)(\b(?:api[-_]?key|access[-_]?key|secret(?:[-_]?key)?|password|"
    r"passwd|(?:[a-z][a-z0-9]*[-_]?)?token|(?:[a-z][a-z0-9]*[-_]?)?credential|"
    r"cookie|(?:[a-z][a-z0-9]*[-_]?)?secret|client[-_]?secret|"
    r"(?:[a-z][a-z0-9]*[-_]?)?password|(?:[a-z][a-z0-9]*[-_]?)?api[-_]?key|"
    r"private[-_]?key|signature)"
    r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
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
_WEB_SCHEME_MARKER = "\ue000dataseek-web-scheme\ue000"


class ToolPresentationColumn(BaseModel):
    """One safe table column selected by a result object key."""

    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=160)
    align: Literal["left", "center", "right"] | None = None


class ToolPresentationSeries(BaseModel):
    """One numeric series selected by a result object key."""

    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None, max_length=160)
    color: str | None = Field(default=None, max_length=32)


class ToolPresentation(BaseModel):
    """Version-one declarative presentation DSL exposed over the existing SSE."""

    model_config = ConfigDict(extra="ignore")

    kind: ToolPresentationKind = "auto"
    title: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=1_000)
    data: Any | None = None
    columns: list[ToolPresentationColumn] | None = None
    series: list[ToolPresentationSeries] | None = None
    x_key: str | None = Field(default=None, max_length=120)
    chart_type: ToolChartType | None = None
    url: str | None = Field(default=None, max_length=2_048)
    mime_type: str | None = Field(default=None, max_length=160)
    filename: str | None = Field(default=None, max_length=255)
    level: ToolLogLevel | None = None


def _safe_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    if _safe_resource_url(text) is not None:
        return text[:limit]
    text = _URL_CREDENTIALS.sub(r"\1[redacted credential]@", text)
    text = _AUTHORIZATION_VALUE.sub(r"\1\2 [redacted credential]", text)
    # A bounded head/tail preview can begin in the middle of an Authorization
    # header, so redact a standalone scheme/value pair as well.
    text = _BARE_AUTH_VALUE.sub(r"\1 [redacted credential]", text)
    text = _CREDENTIAL_VALUE.sub(r"\1[redacted credential]", text)
    # Preserve web URL separators while still redacting filesystem paths in
    # surrounding text and URL query values (for example ?path=/Users/...).
    text = text.replace(_WEB_SCHEME_MARKER, "?")
    for scheme in ("https://", "http://", "wss://", "ws://"):
        text = text.replace(scheme, f"{scheme[:-2]}{_WEB_SCHEME_MARKER}")
    text = _HOST_PATH.sub("[protected path]", text)
    text = text.replace(_WEB_SCHEME_MARKER, "//")
    return text[:limit]


def _safe_key(value: Any) -> str | None:
    text = _safe_text(value, limit=120)
    if text is None:
        return None
    normalized = re.sub(r"[^a-z0-9]", "", text.casefold())
    if normalized in _SECRET_NORMALIZED_KEYS or normalized.endswith(
        _SECRET_NORMALIZED_SUFFIXES
    ):
        return None
    return text


def _compact_json_size(value: Any) -> int:
    """Return the exact UTF-8 size used by the public compact JSON contract."""

    return len(json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8"))


def _fit_json_value(value: Any, max_bytes: int) -> tuple[Any, int] | None:
    """Fit one already-safe scalar, falling back to the size marker."""

    try:
        size = _compact_json_size(value)
    except (TypeError, ValueError, OverflowError):
        value = None
        size = _compact_json_size(value)
    if size <= max_bytes:
        return value, size
    marker_size = _compact_json_size(_SIZE_LIMIT_MARKER)
    if marker_size <= max_bytes:
        return _SIZE_LIMIT_MARKER, marker_size
    return None


def _fit_json_string(value: str, max_bytes: int) -> tuple[Any, int] | None:
    """Return the longest code-point prefix whose JSON encoding fits."""

    full_size = _compact_json_size(value)
    if full_size <= max_bytes:
        return value, full_size
    if _compact_json_size("") > max_bytes:
        return None

    low = 0
    high = len(value)
    while low < high:
        midpoint = (low + high + 1) // 2
        if _compact_json_size(value[:midpoint]) <= max_bytes:
            low = midpoint
        else:
            high = midpoint - 1
    truncated = value[:low]
    return truncated, _compact_json_size(truncated)


def _safe_data_with_size(
    value: Any,
    *,
    depth: int,
    nodes: list[int],
    max_bytes: int,
) -> tuple[Any, int] | None:
    """Sanitize one value and account for every serialized JSON byte."""

    if nodes[0] <= 0:
        return _fit_json_value(_SIZE_LIMIT_MARKER, max_bytes)
    nodes[0] -= 1

    if value is None or isinstance(value, (bool, int)):
        return _fit_json_value(value, max_bytes)
    if isinstance(value, float):
        return _fit_json_value(value if math.isfinite(value) else None, max_bytes)
    if isinstance(value, str):
        safe = _safe_text(value, limit=_MAX_STRING_LENGTH)
        if safe is None:
            return _fit_json_value(None, max_bytes)
        return _fit_json_string(safe, max_bytes)
    if depth >= _MAX_DATA_DEPTH:
        return _fit_json_value(_NESTING_LIMIT_MARKER, max_bytes)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        # The opening and closing brackets are part of the aggregate budget.
        result: list[Any] = []
        serialized_size = 2
        if serialized_size > max_bytes:
            return None
        for item in value[:_MAX_COLLECTION_ITEMS]:
            if nodes[0] <= 0:
                break
            separator_size = 1 if result else 0
            child_budget = max_bytes - serialized_size - separator_size
            child = _safe_data_with_size(
                item,
                depth=depth + 1,
                nodes=nodes,
                max_bytes=child_budget,
            )
            if child is None:
                break
            child_value, child_size = child
            result.append(child_value)
            serialized_size += separator_size + child_size
        return result, serialized_size
    if isinstance(value, dict):
        # The opening/closing braces, serialized keys, colon and commas all
        # count.  This closes the long/repeated-key amplification gap.
        result: dict[str, Any] = {}
        serialized_size = 2
        if serialized_size > max_bytes:
            return None
        for raw_key, item in islice(value.items(), _MAX_OBJECT_FIELDS):
            key = _safe_key(raw_key)
            if key is None or key in result:
                continue
            if nodes[0] <= 0:
                break
            key_size = _compact_json_size(key)
            separator_size = 1 if result else 0
            entry_overhead = separator_size + key_size + 1  # comma + key + colon
            child_budget = max_bytes - serialized_size - entry_overhead
            if child_budget < 0:
                break
            child = _safe_data_with_size(
                item,
                depth=depth + 1,
                nodes=nodes,
                max_bytes=child_budget,
            )
            if child is None:
                break
            child_value, child_size = child
            result[key] = child_value
            serialized_size += entry_overhead + child_size
        return result, serialized_size

    safe = _safe_text(value, limit=_MAX_STRING_LENGTH)
    if safe is None:
        return _fit_json_value(None, max_bytes)
    return _fit_json_string(safe, max_bytes)


def _safe_data(value: Any) -> Any:
    # A single node counter and byte ceiling are shared across the full tree,
    # avoiding exponential expansion and guaranteeing the serialized boundary.
    fitted = _safe_data_with_size(
        value,
        depth=0,
        nodes=[_MAX_DATA_NODES],
        max_bytes=MAX_TOOL_PRESENTATION_DATA_BYTES,
    )
    if fitted is None:
        return _SIZE_LIMIT_MARKER
    safe_value, serialized_size = fitted
    # Keep this assertion next to the encoder contract so later schema changes
    # cannot silently reintroduce character-based accounting.
    assert serialized_size == _compact_json_size(safe_value)
    assert serialized_size <= MAX_TOOL_PRESENTATION_DATA_BYTES
    return safe_value


def sanitize_tool_public_data(value: Any) -> Any:
    """Return one bounded, credential/path-safe value for an SSE payload."""
    return _safe_data(value)


def _safe_resource_url(value: Any) -> str | None:
    """Allow only same-application signed file URLs.

    Relative paths keep the browser on the DataSeek origin.  Scheme-relative,
    external, data, blob and javascript URLs are rejected at the API boundary.
    """

    if not isinstance(value, str) or len(value) > 2_048:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    if not re.fullmatch(r"/api/v1/files/[A-Za-z0-9._-]{1,255}", parsed.path):
        return None
    if parsed.query:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"signature", "expires"}:
            return None
        if len(query["signature"]) != 1 or not re.fullmatch(
            r"[a-fA-F0-9]{64}", query["signature"][0]
        ):
            return None
        if len(query["expires"]) != 1 or not re.fullmatch(
            r"[0-9]{1,12}", query["expires"][0]
        ):
            return None
    return value.strip()


def _safe_columns(value: Any) -> list[ToolPresentationColumn] | None:
    if not isinstance(value, list):
        return None
    columns: list[ToolPresentationColumn] = []
    for raw in value[:_MAX_COLUMNS]:
        if isinstance(raw, str):
            raw = {"key": raw}
        if not isinstance(raw, dict):
            continue
        key = _safe_key(raw.get("key"))
        if key is None:
            continue
        align = raw.get("align")
        columns.append(ToolPresentationColumn(
            key=key,
            label=_safe_text(raw.get("label"), limit=160),
            align=(
                align
                if isinstance(align, str) and align in {"left", "center", "right"}
                else None
            ),
        ))
    return columns or None


def _safe_series(value: Any) -> list[ToolPresentationSeries] | None:
    if not isinstance(value, list):
        return None
    series: list[ToolPresentationSeries] = []
    for raw in value[:_MAX_SERIES]:
        if isinstance(raw, str):
            raw = {"key": raw}
        if not isinstance(raw, dict):
            continue
        key = _safe_key(raw.get("key"))
        if key is None:
            continue
        color = _safe_text(raw.get("color"), limit=32)
        if color and not re.fullmatch(
            r"#[0-9a-fA-F]{3,8}|[a-zA-Z]{1,20}|(?:rgb|hsl)a?\([0-9.,% ]+\)",
            color,
        ):
            color = None
        series.append(ToolPresentationSeries(
            key=key,
            label=_safe_text(raw.get("label"), limit=160),
            color=color,
        ))
    return series or None


def normalize_tool_presentation(value: Any) -> ToolPresentation | None:
    """Validate and bound an extension-provided presentation descriptor."""

    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if not isinstance(value, dict):
        return None

    kind = value.get("kind", "auto")
    if not isinstance(kind, str) or kind not in {
        "auto", "generic", "table", "chart", "map", "image", "artifact", "log",
    }:
        return None

    chart_type = value.get("chart_type")
    level = value.get("level")
    return ToolPresentation(
        kind=kind,
        title=_safe_text(value.get("title"), limit=160),
        description=_safe_text(value.get("description"), limit=1_000),
        data=_safe_data(value.get("data")) if "data" in value else None,
        columns=_safe_columns(value.get("columns")),
        series=_safe_series(value.get("series")),
        x_key=_safe_key(value.get("x_key")),
        chart_type=(
            chart_type
            if isinstance(chart_type, str)
            and chart_type in {"line", "bar", "area", "scatter"}
            else None
        ),
        url=_safe_resource_url(value.get("url")),
        mime_type=_safe_text(value.get("mime_type"), limit=160),
        filename=_safe_text(value.get("filename"), limit=255),
        level=(
            level
            if isinstance(level, str)
            and level in {"debug", "info", "warning", "error"}
            else None
        ),
    )


def _result_payload(value: Any) -> Any:
    """Select the useful public result without exposing runner bookkeeping."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if isinstance(value, dict) and "data" in value:
        value = value.get("data")
    if (
        isinstance(value, dict)
        and value.get("status") == "contract_rejected"
    ):
        return None
    if isinstance(value, dict) and "result" in value:
        return value.get("result")
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        output = value["output"].strip()
        if output and len(output.encode("utf-8")) <= MAX_TOOL_PRESENTATION_DATA_BYTES * 2:
            try:
                return json.loads(output)
            except (TypeError, ValueError, json.JSONDecodeError):
                return output
    return value


def _contract_rejected(value: Any) -> bool:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if isinstance(value, dict) and "data" in value:
        value = value.get("data")
    return isinstance(value, dict) and value.get("status") == "contract_rejected"


def populate_tool_presentation_data(
    presentation: ToolPresentation | None,
    function_result: Any,
) -> ToolPresentation | None:
    """Attach bounded runtime data only when a descriptor asks for a card."""

    if (
        presentation is None
        or presentation.kind == "auto"
        or presentation.data is not None
        or function_result is None
    ):
        return presentation
    if _contract_rejected(function_result):
        return None
    return presentation.model_copy(update={
        "data": _safe_data(_result_payload(function_result)),
    })
