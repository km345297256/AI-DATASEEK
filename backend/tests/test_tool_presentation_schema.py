import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import BrowserToolContent, ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.interfaces.schemas.event import EventMapper, ToolSSEEvent
from app.interfaces.schemas.tool_presentation import (
    MAX_TOOL_PRESENTATION_DATA_BYTES,
    normalize_tool_presentation,
)


def _compact_json_bytes(value):
    return len(json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8"))


def test_tool_presentation_is_bounded_and_redacts_extension_data():
    presentation = normalize_tool_presentation({
        "kind": "table",
        "title": "Results from /Users/alice/private/data.csv",
        "columns": [
            {"key": "station", "label": "Station", "align": "left"},
            {"key": "api_key", "label": "Secret"},
        ],
        "data": [{
            "station": "A",
            "password": "do-not-render",
            "apiKey": "camel-api-key",
            "openaiApiKey": "provider-api-key",
            "accessToken": "camel-access-token",
            "authToken": "camel-auth-token",
            "dbPassword": "database-password",
            "apiSecret": "provider-secret",
            "note": "token=secret-value",
            "camelNote": "clientSecret=secret-client-value",
            "providerNote": "openaiApiKey=provider-key-value",
        }],
        "component": "ArbitraryVueComponent",
        "html": "<script>alert(1)</script>",
    })

    assert presentation is not None
    assert presentation.kind == "table"
    assert presentation.title == "Results from [protected path]"
    assert [column.key for column in presentation.columns or []] == ["station"]
    assert presentation.data == [{
        "station": "A",
        "note": "token=[redacted credential]",
        "camelNote": "clientSecret=[redacted credential]",
        "providerNote": "openaiApiKey=[redacted credential]",
    }]
    assert "component" not in presentation.model_dump()
    assert "html" not in presentation.model_dump()


@pytest.mark.parametrize(
    "url",
    [
        "https://tracker.example/image.png",
        "//tracker.example/image.png",
        "javascript:alert(1)",
        "data:image/svg+xml,<svg onload=alert(1) />",
        "/plugins/runtime/arbitrary-resource",
        "/api/v1/files/../auth/me",
        "/api/v1/files/file-1?token=secret",
        "/api/v1/files/file-1?signature=not-hex&expires=123",
    ],
)
def test_tool_presentation_rejects_non_file_resource_urls(url):
    presentation = normalize_tool_presentation({"kind": "image", "url": url})

    assert presentation is not None
    assert presentation.url is None


def test_tool_presentation_accepts_relative_signed_file_url():
    presentation = normalize_tool_presentation({
        "kind": "artifact",
        "url": f"/api/v1/files/file-1?signature={'a' * 64}&expires=123",
        "filename": "result.csv",
    })

    assert presentation is not None
    assert presentation.url == (
        f"/api/v1/files/file-1?signature={'a' * 64}&expires=123"
    )


@pytest.mark.asyncio
async def test_browser_screenshot_signed_url_survives_sse_sanitization(monkeypatch):
    signed_url = f"/api/v1/files/screenshot-1?signature={'b' * 64}&expires=123"
    file_service = SimpleNamespace(
        create_signed_url=AsyncMock(return_value=signed_url),
    )
    monkeypatch.setattr(
        "app.interfaces.dependencies.get_file_service",
        lambda: file_service,
    )
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="browser-call",
        tool_name="browser",
        function_name="browser_view",
        function_args={},
        tool_content=BrowserToolContent(screenshot="internal-file-id"),
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, ToolSSEEvent)
    assert mapped.data.content.screenshot == signed_url


def test_tool_presentation_redacts_authorization_and_url_credentials():
    presentation = normalize_tool_presentation({
        "kind": "log",
        "data": (
            "Authorization: Bearer sk-live-secret "
            "https://alice:password@example.test/result"
        ),
    })

    assert presentation is not None
    assert isinstance(presentation.data, str)
    assert "sk-live-secret" not in presentation.data
    assert "alice" not in presentation.data
    assert "password" not in presentation.data
    assert presentation.data == (
        "Authorization: Bearer [redacted credential] "
        "https://[redacted credential]@example.test/result"
    )


def test_tool_presentation_redacts_bare_auth_scheme_from_truncated_preview():
    presentation = normalize_tool_presentation({
        "kind": "log",
        "data": "...[earlier bytes omitted]... Bearer partial-secret /Users/alice/data.csv",
    })

    assert presentation is not None
    assert "partial-secret" not in presentation.data
    assert "/Users/alice" not in presentation.data
    assert "Bearer [redacted credential]" in presentation.data


def test_tool_presentation_enforces_aggregate_serialized_byte_budget():
    def long_key(row: int, column: int) -> str:
        prefix = f"field-{row:03d}-{column:03d}-"
        # Quotes make each key considerably larger after JSON escaping.
        return prefix + ('"' * (120 - len(prefix)))

    hostile_rows = [
        {
            "api_key": "must-not-reach-sse",
            "source": "/Users/alice/private/data.nc token=must-not-reach-sse",
            **{long_key(row, column): "x" for column in range(78)},
        }
        for row in range(200)
    ]

    first = normalize_tool_presentation({"kind": "table", "data": hostile_rows})
    second = normalize_tool_presentation({"kind": "table", "data": hostile_rows})

    assert first is not None
    assert second is not None
    assert first.data == second.data
    serialized = json.dumps(
        first.data,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    assert len(serialized.encode("utf-8")) <= MAX_TOOL_PRESENTATION_DATA_BYTES
    assert len(serialized.encode("utf-8")) > 250_000
    assert "api_key" not in serialized
    assert "must-not-reach-sse" not in serialized
    assert "/Users/alice" not in serialized


def test_tool_presentation_budget_counts_multibyte_utf8_data():
    presentation = normalize_tool_presentation({
        "kind": "log",
        "data": ["界" * 20_000 for _ in range(20)],
    })

    assert presentation is not None
    assert _compact_json_bytes(presentation.data) <= MAX_TOOL_PRESENTATION_DATA_BYTES
    # A character-based 256k limit would allow far more than 256k UTF-8 bytes.
    assert sum(len(item) for item in presentation.data) < 100_000


def test_tool_presentation_keeps_existing_item_node_and_depth_limits():
    item_limited = normalize_tool_presentation({
        "kind": "generic",
        "data": list(range(250)),
    })
    assert item_limited is not None
    assert len(item_limited.data) == 200

    node_limited = normalize_tool_presentation({
        "kind": "generic",
        "data": [
            {f"field_{column}": column for column in range(80)}
            for _ in range(200)
        ],
    })
    assert node_limited is not None
    assert len(node_limited.data) < 200
    assert _compact_json_bytes(node_limited.data) <= MAX_TOOL_PRESENTATION_DATA_BYTES

    nested = "leaf"
    for _ in range(12):
        nested = {"next": nested}
    depth_limited = normalize_tool_presentation({"kind": "generic", "data": nested})
    assert depth_limited is not None
    assert "[content omitted: nesting limit]" in json.dumps(depth_limited.data)


@pytest.mark.asyncio
async def test_tool_event_mapper_adds_optional_presentation_without_changing_event_name():
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-1",
        tool_name="plugin",
        function_name="station_summary",
        function_args={},
        presentation={
            "kind": "chart",
            "title": "Temperature",
            "chart_type": "line",
            "series": [{"key": "temperature", "color": "#2b7659"}],
            "entry": "must-not-reach-the-browser",
        },
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, ToolSSEEvent)
    assert mapped.event == "tool"
    assert mapped.data.tool_call_id == "call-1"
    assert mapped.data.presentation is not None
    assert mapped.data.presentation.kind == "chart"
    assert mapped.data.presentation.series[0].key == "temperature"
    assert "entry" not in mapped.data.presentation.model_dump()


@pytest.mark.asyncio
async def test_historical_tool_event_without_descriptor_preserves_legacy_shape():
    event = ToolEvent(
        status=ToolStatus.CALLING,
        tool_call_id="legacy-call",
        tool_name="file",
        function_name="file_read",
        function_args={"file": "/home/ubuntu/datasets/example.csv"},
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, ToolSSEEvent)
    assert mapped.event == "tool"
    assert mapped.data.tool_call_id == "legacy-call"
    assert mapped.data.name == "file"
    assert mapped.data.function == "file_read"
    assert mapped.data.presentation is None


@pytest.mark.asyncio
async def test_tool_event_mapper_sanitizes_public_arguments_at_sse_boundary():
    event = ToolEvent(
        status=ToolStatus.CALLING,
        tool_call_id="private-call",
        tool_name="plugin",
        function_name="private_tool",
        function_args={
            "api_key": "top-secret",
            "input_path": "/Users/alice/private/data.nc",
            "sandbox_path": "/home/ubuntu/datasets/data.nc",
        },
        presentation={"kind": "generic"},
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, ToolSSEEvent)
    assert "api_key" not in mapped.data.args
    assert mapped.data.args["input_path"] == "[protected path]"
    assert mapped.data.args["sandbox_path"] == "/home/ubuntu/datasets/data.nc"
    assert mapped.data.presentation is not None
    assert mapped.data.presentation.data is None


@pytest.mark.asyncio
async def test_tool_event_mapper_projects_exact_json_output_without_runner_bookkeeping():
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-table",
        tool_name="plugin",
        function_name="station_table",
        function_args={},
        presentation={"kind": "table", "columns": [{"key": "station"}]},
        function_result=ToolResult(
            success=True,
            data={
                "session_id": "must-not-leak",
                "command": "ai-dataseek-tool run station_table secret-payload",
                "status": "completed",
                "returncode": 0,
                "output": '{"rows":[{"station":"A","value":12.5}]}',
            },
        ),
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, ToolSSEEvent)
    assert mapped.data.presentation is not None
    assert mapped.data.presentation.data == {
        "rows": [{"station": "A", "value": 12.5}],
    }
    rendered = mapped.data.presentation.model_dump_json()
    assert "session_id" not in rendered
    assert "ai-dataseek-tool" not in rendered
    assert "secret-payload" not in rendered


@pytest.mark.asyncio
async def test_tool_event_mapper_bounds_and_redacts_non_json_output():
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-log",
        tool_name="plugin",
        function_name="bounded_log",
        function_args={},
        presentation={"kind": "log", "level": "info"},
        function_result=ToolResult(
            success=True,
            data={
                "status": "completed",
                "output": "token=do-not-render /Users/alice/private/data.nc " + ("x" * 30_000),
            },
        ),
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, ToolSSEEvent)
    assert mapped.data.presentation is not None
    assert isinstance(mapped.data.presentation.data, str)
    assert len(mapped.data.presentation.data) <= 20_000
    assert "do-not-render" not in mapped.data.presentation.data
    assert "/Users/alice" not in mapped.data.presentation.data
