import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from app.domain.models.mcp_config import MCPConfig, MCPServerConfig, MCPTransport
from app.domain.models.spill import SpillArtifactOwner
from app.domain.services.tools import mcp as mcp_module
from app.domain.services.tools.mcp import MCPClientManager, MCPToolkit, _MCPToolWrapper
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.spill import SpillArtifactInterceptor
from app.domain.services.tools.spill_projection import spill_notice_from_result


COUNT_SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
}


@pytest.mark.asyncio
async def test_sdk_session_uses_one_typed_call_without_implicit_discovery_or_schema_fetch():
    session = mcp_module.ClientSession(None, None)
    result = CallToolResult(content=[], structuredContent={"count": 42})
    session.send_request = AsyncMock(return_value=result)
    session.list_tools = AsyncMock(side_effect=AssertionError("must not rediscover after execution"))
    session._tool_output_schemas["query"] = {"$ref": "https://not-accessed.invalid/schema.json"}

    assert await session.call_tool("query", {"limit": 2}) is result

    session.send_request.assert_awaited_once()
    request, response_type = session.send_request.await_args.args
    assert request.root.method == "tools/call"
    assert request.root.params.name == "query"
    assert request.root.params.arguments == {"limit": 2}
    assert response_type is CallToolResult
    session.list_tools.assert_not_awaited()


async def manager_with_result(result, *, schema=None, env=None):
    config = MCPServerConfig(transport=MCPTransport.STDIO, command="demo", env=env)
    manager = MCPClientManager(MCPConfig(mcpServers={"demo": config}))
    manager._tools_cache["demo"] = [Tool(
        name="query", description="Query", inputSchema={"type": "object"},
        outputSchema=copy.deepcopy(schema),
    )]
    await manager.get_all_tools()
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    manager._clients["demo"] = session
    return manager, session


@pytest.mark.asyncio
async def test_explicit_mcp_error_preserves_safe_details_and_does_not_validate_success_schema():
    result = CallToolResult(isError=True, content=[TextContent(type="text", text="No matching records")],
                            structuredContent={"reason": "not_found"})
    manager, session = await manager_with_result(result, schema=COUNT_SCHEMA)

    actual = await manager.call_tool("mcp_demo_query", {})

    assert actual.success is False
    assert actual.data["structuredContent"] == {"reason": "not_found"}
    assert actual.data["content"] == [{"type": "text", "text": "No matching records"}]
    session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_structured_result_is_preserved_without_promoting_returned_execution_metadata():
    payload = {"count": 42, "effects": ["sandbox_read"], "permissions": ["*"],
               "success": False, "artifact": {"verified": True}}
    manager, _ = await manager_with_result(CallToolResult(content=[], structuredContent=payload),
                                          schema=COUNT_SCHEMA)

    actual = await manager.call_tool("mcp_demo_query", {})

    assert actual.success is True
    assert actual.data == {"content": [], "structuredContent": payload}
    assert not hasattr(actual, "effects")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {"count": "wrong /Users/alice/private"}])
async def test_output_schema_mismatch_is_quarantined_without_retry_or_echo(payload):
    manager, session = await manager_with_result(CallToolResult(
        content=[TextContent(type="text", text="untrusted rejected output")],
        structuredContent=payload,
    ), schema=COUNT_SCHEMA)

    actual = await manager.call_tool("mcp_demo_query", {})

    assert actual.success is False
    assert actual.data == {"error": "mcp_tool_output_invalid"}
    assert "untrusted" not in actual.model_dump_json()
    assert "/Users/alice" not in actual.model_dump_json()
    session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_output_schema_is_pinned_and_remote_refs_are_not_retrieved():
    manager, _ = await manager_with_result(CallToolResult(content=[], structuredContent={"count": 42}),
                                          schema=COUNT_SCHEMA)
    manager._tools_cache["demo"][0].outputSchema["properties"]["count"]["type"] = "string"
    assert (await manager.call_tool("mcp_demo_query", {})).success is True

    manager, _ = await manager_with_result(CallToolResult(content=[], structuredContent={"count": 42}),
                                          schema={"$ref": "https://not-accessed.invalid/schema.json"})
    assert (await manager.call_tool("mcp_demo_query", {})).data == {"error": "mcp_tool_output_invalid"}


@pytest.mark.asyncio
async def test_text_result_contract_stays_compatible():
    manager, _ = await manager_with_result(CallToolResult(content=[
        TextContent(type="text", text="first"), TextContent(type="text", text="second"),
    ]))
    actual = await manager.call_tool("mcp_demo_query", {})
    assert actual.success is True
    assert actual.data == "first\nsecond"


@pytest.mark.asyncio
async def test_error_and_structured_outputs_redact_credentials_and_host_paths(caplog):
    raw = {"api_key": "server-key", "location": "/Users/alice/private/data.csv",
           "echo": "known-secret", "nested": {"Authorization": "Bearer other-secret"},
           "sandbox": "/home/ubuntu/data.csv"}
    manager, _ = await manager_with_result(CallToolResult(isError=True, content=[
        TextContent(type="text", text="Bearer other-secret at /Users/alice/private/data.csv"),
    ], structuredContent=raw), env={"CUSTOM_SECRET": "known-secret"})
    actual = await manager.call_tool("mcp_demo_query", {})
    rendered = actual.model_dump_json()
    for forbidden in ("server-key", "/Users/alice", "known-secret", "other-secret"):
        assert forbidden not in rendered
        assert forbidden not in caplog.text
    assert actual.data["structuredContent"]["sandbox"] == "/home/ubuntu/data.csv"
    assert raw["echo"] == "known-secret"


@pytest.mark.asyncio
async def test_non_secret_runtime_environment_does_not_corrupt_tool_data():
    manager, _ = await manager_with_result(CallToolResult(
        content=[TextContent(type="text", text="1 product")],
        structuredContent={"count": 1, "description": "product"},
    ), env={"TIMEOUT": "1", "ENVIRONMENT": "prod", "PATH": "/bin"})
    actual = await manager.call_tool("mcp_demo_query", {})
    assert actual.data == {
        "content": [{"type": "text", "text": "1 product"}],
        "structuredContent": {"count": 1, "description": "product"},
    }


@pytest.mark.asyncio
async def test_non_text_blocks_never_stringify_binary_metadata_or_fetch_resources():
    result = CallToolResult.model_validate({"content": [
        {"type": "image", "mimeType": "image/png", "data": "cHJpdmF0ZQ=="},
        {"type": "audio", "mimeType": "audio/wav", "data": "cHJpdmF0ZQ=="},
        {"type": "resource", "resource": {"uri": "file:///Users/alice/file.txt", "text": "safe text"}},
        {"type": "resource_link", "name": "private", "uri": "file:///Users/alice/private"},
    ]})
    manager, session = await manager_with_result(result)
    actual = await manager.call_tool("mcp_demo_query", {})
    assert actual.success is True
    assert actual.data["content"][2] == {"type": "resource", "text": "safe text"}
    assert all(actual.data["content"][index]["omitted"] for index in (0, 1, 3))
    assert "cHJpdmF0ZQ==" not in actual.model_dump_json()
    assert "/Users/alice" not in actual.model_dump_json()
    session.call_tool.assert_awaited_once()


def catalog_tool(name):
    return Tool(name=name, inputSchema={"type": "object"})


@pytest.mark.asyncio
async def test_tool_discovery_reads_all_pages_and_publishes_a_complete_snapshot():
    manager = MCPClientManager()
    first = catalog_tool("first")
    session = SimpleNamespace(list_tools=AsyncMock(side_effect=[
        SimpleNamespace(tools=[first], nextCursor="second-page"),
        SimpleNamespace(tools=[catalog_tool("second")], nextCursor=None),
    ]))
    await manager._cache_server_tools("demo", session)
    assert [tool.name for tool in manager._tools_cache["demo"]] == ["first", "second"]
    assert session.list_tools.await_args_list == [call(), call(cursor="second-page")]
    first.name = "mutated"
    assert manager._tools_cache["demo"][0].name == "first"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["loop", "duplicate", "page_limit", "tool_limit", "transport"])
async def test_invalid_catalog_never_publishes_a_partial_generation(failure, monkeypatch, caplog):
    monkeypatch.setattr(mcp_module, "_MAX_TOOL_DISCOVERY_PAGES", 2)
    monkeypatch.setattr(mcp_module, "_MAX_SERVER_TOOLS", 2)
    first = SimpleNamespace(tools=[catalog_tool("first")], nextCursor="sensitive-cursor")
    next_page = SimpleNamespace(tools=[catalog_tool("second")], nextCursor=None)
    if failure == "loop":
        next_page.nextCursor = "sensitive-cursor"
    elif failure == "duplicate":
        next_page.tools = [catalog_tool("first")]
    elif failure == "page_limit":
        next_page.nextCursor = "third-page"
    elif failure == "tool_limit":
        next_page.tools.append(catalog_tool("third"))
    else:
        next_page = RuntimeError("provider-secret")
    session = SimpleNamespace(list_tools=AsyncMock(side_effect=[first, next_page]))
    manager = MCPClientManager()
    previous = [catalog_tool("previous")]
    manager._tools_cache["demo"] = previous

    await manager._cache_server_tools("demo", session)

    assert manager._tools_cache["demo"] is previous
    assert session.list_tools.await_count == 2
    assert "sensitive-cursor" not in caplog.text
    assert "provider-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [MCPTransport.SSE, MCPTransport.STREAMABLE_HTTP])
async def test_http_headers_are_used_for_both_discovery_and_single_invocation(transport, monkeypatch):
    config = MCPServerConfig(transport=transport, url="https://not-accessed.invalid/mcp",
                             headers={"Authorization": "Bearer configured-secret"})
    calls = []
    sessions = []

    @asynccontextmanager
    async def connection(url, **kwargs):
        calls.append((url, kwargs))
        yield (object(), object())

    @asynccontextmanager
    async def client_session(*_):
        session = SimpleNamespace(
            initialize=AsyncMock(),
            list_tools=AsyncMock(return_value=SimpleNamespace(tools=[catalog_tool("query")], nextCursor=None)),
            call_tool=AsyncMock(return_value=CallToolResult(content=[])),
        )
        sessions.append(session)
        yield session

    monkeypatch.setattr(mcp_module, "sse_client", connection)
    monkeypatch.setattr(mcp_module, "streamablehttp_client", connection)
    monkeypatch.setattr(mcp_module, "ClientSession", client_session)
    manager = MCPClientManager(MCPConfig(mcpServers={"demo": config}))
    await manager.initialize()
    await manager.get_all_tools()
    assert (await manager.call_tool("mcp_demo_query", {})).success is True
    assert calls == [(config.url, {"headers": config.headers})] * 2
    sessions[0].call_tool.assert_not_awaited()
    sessions[1].call_tool.assert_awaited_once_with("query", {})
    await manager.cleanup()


@pytest.mark.asyncio
async def test_call_and_discovery_cancellation_are_propagated():
    manager, session = await manager_with_result(None)
    session.call_tool.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await manager.call_tool("mcp_demo_query", {})
    session.list_tools = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await manager._cache_server_tools("demo", session)


@pytest.mark.asyncio
async def test_large_structured_output_reaches_existing_spill_without_preview_truncation():
    payload = {"rows": [{"value": index} for index in range(3000)]}
    manager, _ = await manager_with_result(CallToolResult(content=[], structuredContent=payload))
    captured = []

    async def save_text(request):
        captured.append(request)
        # Exercise the durable-store-unavailable bounded projection, too.
        raise RuntimeError("fake store unavailable")

    toolkit = MCPToolkit()
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([SpillArtifactInterceptor(
        SimpleNamespace(save_text=save_text),
        owner=SpillArtifactOwner(user_id="user-1", session_id="session-1"),
        max_inline_bytes=2048, preview_bytes=100,
    )])
    wrapper = _MCPToolWrapper("mcp_demo_query", manager, toolkit)

    message = await wrapper.ainvoke({"id": "call-1", "args": {}})

    assert len(captured) == 1
    assert json.loads(captured[0].content)["data"]["structuredContent"] == payload
    assert len(message.content.encode()) <= 2048
    assert spill_notice_from_result(message).status == "unavailable"
