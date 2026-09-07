import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.mcp_config import MCPConfig, MCPServerConfig, MCPTransport
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools import mcp as mcp_module
from app.domain.services.tools.mcp import (
    MCPClientManager,
    MCPToolkit,
    _MCPToolWrapper,
    _build_stdio_environment,
    _model_tool_name,
)
from app.domain.services.tools.pipeline import (
    ToolExecutionInterceptor,
    ToolExecutionPipeline,
    summarize_argument_keys,
)


def test_stdio_environment_inherits_only_runtime_values_and_explicit_server_env(monkeypatch):
    monkeypatch.setenv("PATH", "/backend/bin")
    monkeypatch.setenv("HOME", "/backend/home")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "backend-model-secret")
    monkeypatch.setenv("MONGODB_URL", "mongodb://backend-secret")
    monkeypatch.setenv("REDIS_URL", "redis://backend-secret")
    monkeypatch.setenv("LC_SECRET", "not-a-locale-variable")
    monkeypatch.setenv("PYTHONPATH", "/backend/injected-python")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/backend/injected-node.js")

    environment = _build_stdio_environment({
        "PATH": "/mcp/bin",
        "MCP_API_TOKEN": "explicit-mcp-secret",
    })

    assert environment["PATH"] == "/mcp/bin"
    assert environment["HOME"] == "/backend/home"
    assert environment["LANG"] == "zh_CN.UTF-8"
    assert environment["LC_ALL"] == "zh_CN.UTF-8"
    assert environment["MCP_API_TOKEN"] == "explicit-mcp-secret"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "MONGODB_URL" not in environment
    assert "REDIS_URL" not in environment
    assert "LC_SECRET" not in environment
    assert "PYTHONPATH" not in environment
    assert "NODE_OPTIONS" not in environment


@pytest.mark.asyncio
async def test_stdio_connection_uses_sanitized_environment(monkeypatch):
    captured = {}

    def fake_server_parameters(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    class StopConnection(Exception):
        pass

    def fake_stdio_client(server_params):
        captured["server_params"] = server_params
        raise StopConnection

    monkeypatch.setattr(mcp_module, "StdioServerParameters", fake_server_parameters)
    monkeypatch.setattr(mcp_module, "stdio_client", fake_stdio_client)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("PATH", "/backend/bin")

    server = MCPServerConfig(
        transport=MCPTransport.STDIO,
        command="demo-mcp",
        args=["--stdio"],
        env={"SERVER_TOKEN": "explicit-token"},
    )
    manager = MCPClientManager(MCPConfig(mcpServers={"demo": server}))

    with pytest.raises(StopConnection):
        await manager._connect_stdio_server("demo", server)

    assert captured["command"] == "demo-mcp"
    assert captured["args"] == ["--stdio"]
    assert captured["env"]["PATH"] == "/backend/bin"
    assert captured["env"]["SERVER_TOKEN"] == "explicit-token"
    assert "DEEPSEEK_API_KEY" not in captured["env"]


@pytest.mark.asyncio
async def test_mcp_tool_info_log_contains_only_hashed_argument_keys(caplog):
    server = MCPServerConfig(
        transport=MCPTransport.STDIO,
        command="demo-mcp",
    )
    manager = MCPClientManager(MCPConfig(mcpServers={"demo": server}))
    session = SimpleNamespace(
        call_tool=lambda *_args, **_kwargs: None,
    )

    async def call_tool(*_args, **_kwargs):
        return SimpleNamespace(content=[])

    session.call_tool = call_tool
    manager._clients["demo"] = session
    manager._tools_cache["demo"] = [
        SimpleNamespace(name="query", description="Query", inputSchema={})
    ]
    await manager.get_all_tools()

    with caplog.at_level(logging.INFO, logger=mcp_module.__name__):
        result = await manager.call_tool(
            "mcp_demo_query",
            {
                "password": "plain-text-password",
                "query": "select * from private_records",
                "token": "plain-text-token",
            },
        )

    assert result.success is True
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "query" not in messages
    assert "password" not in messages
    assert "token" not in messages
    assert "arg:sha256:" in messages
    assert "tool:sha256:" in messages
    assert "plain-text-password" not in messages
    assert "select * from private_records" not in messages
    assert "plain-text-token" not in messages


@pytest.mark.asyncio
async def test_mcp_failure_does_not_echo_provider_exception_to_log_or_result(caplog):
    server = MCPServerConfig(
        transport=MCPTransport.STDIO,
        command="demo-mcp",
    )
    manager = MCPClientManager(MCPConfig(mcpServers={"demo": server}))
    manager._tools_cache["demo"] = [
        SimpleNamespace(name="query", description="Query", inputSchema={})
    ]
    await manager.get_all_tools()
    manager._clients["demo"] = SimpleNamespace(
        call_tool=AsyncMock(
            side_effect=RuntimeError(
                "Bearer provider-secret at /Users/alice/private/config.json"
            )
        )
    )

    with caplog.at_level(logging.ERROR, logger=mcp_module.__name__):
        result = await manager.call_tool("mcp_demo_query", {})

    rendered = result.model_dump_json()
    assert result.success is False
    assert result.message == "MCP 工具调用失败"
    assert result.data == {"error": "mcp_tool_call_failed"}
    assert "provider-secret" not in rendered
    assert "/Users/alice" not in rendered
    assert "provider-secret" not in caplog.text
    assert "/Users/alice" not in caplog.text


@pytest.mark.asyncio
async def test_mcp_tool_routing_uses_exact_discovered_name_for_overlapping_servers():
    server = MCPServerConfig(
        transport=MCPTransport.STDIO,
        command="demo-mcp",
    )
    manager = MCPClientManager(
        MCPConfig(mcpServers={"foo": server, "foo_bar": server})
    )
    manager._tools_cache = {
        "foo": [SimpleNamespace(name="query", description="Query", inputSchema={})],
        "foo_bar": [SimpleNamespace(name="query", description="Query", inputSchema={})],
    }
    await manager.get_all_tools()

    foo_session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(content=[]))
    )
    foo_bar_session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(content=[]))
    )
    manager._clients = {"foo": foo_session, "foo_bar": foo_bar_session}

    result = await manager.call_tool("mcp_foo_bar_query", {"value": 1})

    assert result.success is True
    foo_session.call_tool.assert_not_awaited()
    foo_bar_session.call_tool.assert_awaited_once_with("query", {"value": 1})


@pytest.mark.asyncio
async def test_mcp_tool_discovery_rejects_normalized_emitted_name_collision():
    server = MCPServerConfig(
        transport=MCPTransport.STDIO,
        command="demo-mcp",
    )
    manager = MCPClientManager(
        MCPConfig(mcpServers={"foo-bar": server, "foo_bar": server})
    )
    manager._tools_cache = {
        "foo-bar": [SimpleNamespace(name="query", description="Query", inputSchema={})],
        "foo_bar": [SimpleNamespace(name="query", description="Query", inputSchema={})],
    }

    with pytest.raises(ValueError, match="MCP 工具名冲突: mcp_foo_bar_query"):
        await manager.get_all_tools()

    assert manager._tool_routes == {}


def test_mcp_tool_names_are_provider_safe_stable_and_backward_compatible():
    assert _model_tool_name("demo-server", "query_data") == "mcp_demo_server_query_data"

    encoded = _model_tool_name("气候 服务/一", "读取 数据/年度" * 10)
    assert len(encoded) <= 64
    assert encoded.startswith("mcp_")
    assert all(character.isascii() and (character.isalnum() or character in "_-") for character in encoded)
    assert encoded == _model_tool_name("气候 服务/一", "读取 数据/年度" * 10)
    assert encoded != _model_tool_name("气候 服务/二", "读取 数据/年度" * 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["initialize", "get_all_tools"])
async def test_mcp_toolkit_cleans_and_resets_after_initialization_failure(
    monkeypatch,
    failure_stage,
):
    class FailingManager:
        def __init__(self):
            self.cleanup = AsyncMock()

        async def initialize(self):
            if failure_stage == "initialize":
                raise RuntimeError("initialize failed")

        async def get_all_tools(self):
            if failure_stage == "get_all_tools":
                raise RuntimeError("discovery failed")
            return []

    manager = FailingManager()
    monkeypatch.setattr(mcp_module, "MCPClientManager", lambda _config: manager)
    toolkit = MCPToolkit()
    config = MCPConfig(mcpServers={})

    with pytest.raises(RuntimeError):
        await toolkit.initialized(config)

    manager.cleanup.assert_awaited_once_with()
    assert toolkit.manager is None
    assert toolkit._initialized is False
    assert toolkit._tools == []
    assert toolkit._config is None


def test_logged_argument_key_summary_is_bounded_and_escapes_control_characters():
    arguments = {
        "line\nbreak": "secret",
        "x" * 100: "secret",
        **{f"field_{index:02d}": "secret" for index in range(25)},
    }

    summary = summarize_argument_keys(arguments)

    assert len(summary) == 20
    assert summary[-1].startswith("...(+")
    assert all(re.fullmatch(r"arg:sha256:[0-9a-f]{12}", key) for key in summary[:-1])
    assert "line" not in repr(summary)


@pytest.mark.asyncio
async def test_mcp_tool_wrapper_uses_execution_pipeline_without_changing_result_contract():
    observed = []

    class CaptureInterceptor(ToolExecutionInterceptor):
        async def pre_execute(self, context):
            observed.append((context.tool_name, context.tool_call_id, context.arguments))

    toolkit = MCPToolkit()
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([CaptureInterceptor()])
    tool_result = ToolResult(success=True, data={"answer": 42})
    manager = SimpleNamespace(call_tool=AsyncMock(return_value=tool_result))
    wrapper = _MCPToolWrapper("mcp_demo_query", manager, toolkit)
    arguments = {"query": "answer"}

    message = await wrapper.ainvoke({
        "id": "call-mcp-pipeline",
        "args": arguments,
    })

    manager.call_tool.assert_awaited_once_with("mcp_demo_query", arguments)
    assert observed == [("mcp_demo_query", "call-mcp-pipeline", arguments)]
    assert message.tool_call_id == "call-mcp-pipeline"
    assert message.name == "mcp_demo_query"
    assert message.artifact is tool_result
    assert message.content == tool_result.model_dump_json()
