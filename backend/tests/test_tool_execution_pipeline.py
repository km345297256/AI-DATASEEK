import asyncio
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, ToolMessage
from langchain.tools import tool

from app.domain.models.event import ToolEvent
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.pipeline import (
    ToolExecutionInterceptor,
    ToolExecutionPipeline,
    summarize_argument_keys,
)
from app.domain.services.tools.registry import ToolRegistry


class _RecordingInterceptor(ToolExecutionInterceptor):
    def __init__(self, label: str, events: list[str]) -> None:
        self.label = label
        self.events = events

    async def pre_execute(self, context) -> None:
        self.events.append(f"{self.label}:pre:{context.tool_name}")

    async def guard(self, context) -> None:
        self.events.append(f"{self.label}:guard:{context.tool_call_id}")

    async def execute(self, context, call_next):
        self.events.append(f"{self.label}:execute:before")
        value = await call_next()
        self.events.append(f"{self.label}:execute:after")
        return value

    async def post_execute(self, context, result) -> None:
        self.events.append(f"{self.label}:post:{result}")

    async def result(self, context, result):
        self.events.append(f"{self.label}:result:{result}")
        return f"{result}|{self.label}"


class _EchoToolkit(BaseToolkit):
    name: str = "echo"

    @tool
    async def echo_value(self, value: str) -> ToolResult:
        """Return one value unchanged."""
        return ToolResult(success=True, data={"value": value})


@pytest.mark.asyncio
async def test_tool_execution_pipeline_runs_stages_without_rewriting_call():
    events: list[str] = []
    pipeline = ToolExecutionPipeline([
        _RecordingInterceptor("outer", events),
        _RecordingInterceptor("inner", events),
    ])
    arguments = {"value": "unchanged"}
    tool_call = {"id": "call-1", "name": "echo_value", "args": arguments}

    async def execute(context):
        assert context.tool_call is tool_call
        assert context.arguments is arguments
        events.append("execute")
        return "raw"

    result = await pipeline.invoke(
        tool=SimpleNamespace(name="echo_value"),
        tool_call=tool_call,
        execute=execute,
    )

    assert result == "raw|inner|outer"
    assert tool_call["args"] is arguments
    assert events == [
        "outer:pre:echo_value",
        "inner:pre:echo_value",
        "outer:guard:call-1",
        "inner:guard:call-1",
        "outer:execute:before",
        "inner:execute:before",
        "execute",
        "inner:execute:after",
        "outer:execute:after",
        "inner:post:raw",
        "outer:post:raw",
        "inner:result:raw",
        "outer:result:raw|inner",
    ]


@pytest.mark.asyncio
async def test_pipeline_registration_is_reversible_and_invocation_uses_snapshot():
    events: list[str] = []
    pipeline = ToolExecutionPipeline()
    dispose = None

    class SelfRemovingInterceptor(ToolExecutionInterceptor):
        async def pre_execute(self, context) -> None:
            events.append("pre")
            dispose()

        async def guard(self, context) -> None:
            events.append("guard")

        async def execute(self, context, call_next):
            events.append("execute")
            return await call_next()

        async def post_execute(self, context, result) -> None:
            events.append("post")

        async def result(self, context, result):
            events.append("result")
            return result

    dispose = pipeline.register(SelfRemovingInterceptor())

    async def execute(_context):
        events.append("tool")
        return "ok"

    await pipeline.invoke(tool=SimpleNamespace(name="echo"), tool_call={}, execute=execute)
    await pipeline.invoke(tool=SimpleNamespace(name="echo"), tool_call={}, execute=execute)
    dispose()

    assert events == ["pre", "guard", "execute", "tool", "post", "result", "tool"]


def test_argument_key_summary_is_bounded_and_log_safe():
    arguments = {f"key-{index}": "secret" for index in range(24)}
    arguments["line\nbreak"] = "secret"
    arguments["x" * 200] = "secret"

    summary = summarize_argument_keys(arguments)

    assert len(summary) == 20
    assert summary[-1].startswith("...(+")
    assert all(re.fullmatch(r"arg:sha256:[0-9a-f]{12}", key) for key in summary[:-1])
    assert "secret" not in repr(summary)
    assert "line" not in repr(summary)


@pytest.mark.asyncio
async def test_base_tool_wrapper_uses_pipeline_and_preserves_tool_message_contract():
    toolkit = _EchoToolkit()
    events: list[str] = []

    class CaptureInterceptor(ToolExecutionInterceptor):
        async def pre_execute(self, context) -> None:
            events.append(f"pre:{context.tool_name}")

        async def guard(self, context) -> None:
            events.append(f"guard:{context.tool_call_id}")

    toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        CaptureInterceptor(),
    ])

    message = await toolkit.get_tool("echo_value").ainvoke({
        "id": "call-echo",
        "args": {"value": "sample"},
    })

    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call-echo"
    assert message.name == "echo_value"
    assert message.artifact == ToolResult(success=True, data={"value": "sample"})
    assert events == ["pre:echo_value", "guard:call-echo"]


def test_tool_registry_preserves_order_and_live_toolkit_state():
    first_tool = SimpleNamespace(name="first")
    duplicate_from_first = SimpleNamespace(name="duplicate-first")
    duplicate_from_second = SimpleNamespace(name="duplicate-second")

    class Toolkit:
        def __init__(self, tools):
            self.tools = tools
            self.enabled = True

        def get_tools(self):
            return list(self.tools) if self.enabled else []

        def get_tool(self, name):
            if not self.enabled:
                return None
            return next((item for item in self.tools if item[0] == name), (None, None))[1]

    first = Toolkit([("first", first_tool), ("duplicate", duplicate_from_first)])
    second = Toolkit([("duplicate", duplicate_from_second)])
    registry = ToolRegistry([first, second])

    assert registry.get_tool("duplicate") is duplicate_from_first
    assert registry.get_tools() == [
        ("first", first_tool),
        ("duplicate", duplicate_from_first),
        ("duplicate", duplicate_from_second),
    ]

    first.enabled = False
    assert registry.get_tool("duplicate") is duplicate_from_second
    assert registry.get_tools() == [("duplicate", duplicate_from_second)]


def test_tool_registry_rejects_ambiguous_model_facing_names():
    class Toolkit:
        def __init__(self, names):
            self.names = names

        def get_tools(self):
            return [
                {
                    "type": "function",
                    "function": {"name": name, "parameters": {"type": "object"}},
                }
                for name in self.names
            ]

    registry = ToolRegistry([
        Toolkit(["unique", "mcp_list_tools"]),
        Toolkit(["mcp_list_tools"]),
    ])

    with pytest.raises(ValueError, match="Agent tool names are ambiguous: mcp_list_tools"):
        registry.assert_unique_tool_names()


def test_tool_registry_rejects_exposed_toolkit_without_execution_pipeline():
    toolkit = SimpleNamespace(
        name="unintercepted-domain",
        get_tools=lambda: [{
            "type": "function",
            "function": {"name": "unsafe_tool", "parameters": {"type": "object"}},
        }],
    )
    registry = ToolRegistry([toolkit])

    with pytest.raises(ValueError, match="unintercepted-domain"):
        registry.register_interceptors([_RecordingInterceptor("x", [])])


@pytest.mark.asyncio
async def test_registry_registers_and_disposes_interceptor_across_toolkits():
    toolkits = [_EchoToolkit(), _EchoToolkit()]
    registry = ToolRegistry(toolkits)
    observed: list[str] = []

    class CaptureInterceptor(ToolExecutionInterceptor):
        async def pre_execute(self, context) -> None:
            observed.append(context.tool_call_id)

    dispose = registry.register_interceptor(CaptureInterceptor())
    await toolkits[0].get_tool("echo_value").ainvoke({
        "id": "first",
        "args": {"value": "one"},
    })
    await toolkits[1].get_tool("echo_value").ainvoke({
        "id": "second",
        "args": {"value": "two"},
    })
    dispose()
    dispose()
    await toolkits[0].get_tool("echo_value").ainvoke({
        "id": "after-dispose",
        "args": {"value": "three"},
    })

    assert observed == ["first", "second"]


def test_base_agent_registry_tracks_replaced_toolkit_collection():
    agent = object.__new__(BaseAgent)
    first = SimpleNamespace(
        get_tool=lambda _name: "first",
        get_tools=lambda: ["first"],
    )
    second = SimpleNamespace(
        get_tool=lambda _name: "second",
        get_tools=lambda: ["second"],
    )

    agent.toolkits = [first]
    assert agent.get_tool("anything") == "first"
    assert agent.get_tools() == ["first"]

    agent.toolkits = [second]
    assert agent.get_tool("anything") == "second"
    assert agent.get_tools() == ["second"]


@pytest.mark.asyncio
async def test_agent_tool_events_keep_original_function_arguments():
    agent = object.__new__(BaseAgent)
    agent.name = "test-agent"
    agent.max_iterations = 2
    agent.max_retries = 0
    agent.retry_interval = 0
    agent.toolkits = [_EchoToolkit()]
    arguments = {"value": "visible-to-existing-ui"}
    first = AIMessage(content="", tool_calls=[{
        "name": "echo_value",
        "args": arguments,
        "id": "call-event",
    }])
    agent.ask = AsyncMock(return_value=first)
    agent.ask_with_messages = AsyncMock(return_value=AIMessage(content="done"))

    events = [event async for event in agent.execute("run")]
    tool_events = [event for event in events if isinstance(event, ToolEvent)]

    assert len(tool_events) == 2
    assert all(event.function_args == arguments for event in tool_events)
    assert all(event.tool_call_id == "call-event" for event in tool_events)


@pytest.mark.asyncio
async def test_agent_failure_log_contains_keys_but_no_argument_or_exception_values(caplog):
    agent = object.__new__(BaseAgent)
    agent.max_retries = 0
    agent.retry_interval = 0
    tool = SimpleNamespace(
        name="private_tool",
        ainvoke=AsyncMock(side_effect=RuntimeError("exception-secret-value")),
    )
    tool_call = {
        "id": "call-private",
        "name": "private_tool",
        "args": {
            "api_key": "argument-secret-value",
            "query": "private-query-value",
        },
    }

    with caplog.at_level(logging.ERROR):
        result = await agent.invoke_tool(tool, tool_call)

    assert result.tool_call_id == "call-private"
    assert result.content == "exception-secret-value"
    assert "tool=private_tool" in caplog.text
    assert "call_id=call:sha256:" in caplog.text
    assert "call-private" not in caplog.text
    assert len(re.findall(r"arg:sha256:[0-9a-f]{12}", caplog.text)) == 2
    assert "api_key" not in caplog.text
    assert "query" not in caplog.text
    assert "argument-secret-value" not in caplog.text
    assert "private-query-value" not in caplog.text
    assert "exception-secret-value" not in caplog.text


def test_registry_rejects_empty_dynamic_toolkit_without_execution_pipeline():
    class DynamicToolkit:
        name = "dynamic"

        def __init__(self):
            self.tools = []

        def get_tools(self):
            return list(self.tools)

        def get_tool(self, name):
            return next((tool for tool in self.tools if tool.name == name), None)

    toolkit = DynamicToolkit()
    registry = ToolRegistry([toolkit])

    with pytest.raises(ValueError, match="dynamic"):
        registry.register_interceptor(ToolExecutionInterceptor())

    # A tool discovered later must not become executable under an Agent that
    # was configured without policy/timeout/trace coverage.
    toolkit.tools.append(SimpleNamespace(name="late_tool"))
    assert registry.get_tool("late_tool") is not None


@pytest.mark.asyncio
async def test_agent_does_not_retry_explicit_non_retryable_tool_failure():
    class NonRetryableFailure(RuntimeError):
        retryable = False

    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    agent.retry_interval = 0
    tool = SimpleNamespace(
        name="write_once",
        ainvoke=AsyncMock(side_effect=NonRetryableFailure("denied")),
    )

    result = await agent.invoke_tool(tool, {
        "id": "call-write",
        "name": "write_once",
        "args": {},
    })

    assert tool.ainvoke.await_count == 1
    assert result.content == "denied"


@pytest.mark.asyncio
async def test_agent_retries_ordinary_retryable_tool_failure():
    agent = object.__new__(BaseAgent)
    agent.max_retries = 2
    agent.retry_interval = 0
    expected = ToolMessage(tool_call_id="call-read", name="read", content="ok")
    tool = SimpleNamespace(
        name="read",
        ainvoke=AsyncMock(side_effect=[RuntimeError("temporary"), expected]),
    )

    result = await agent.invoke_tool(tool, {
        "id": "call-read",
        "name": "read",
        "args": {},
    })

    assert tool.ainvoke.await_count == 2
    assert result is expected


@pytest.mark.asyncio
async def test_agent_propagates_tool_cancellation_without_retry():
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    agent.retry_interval = 0
    tool = SimpleNamespace(
        name="long_read",
        ainvoke=AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await agent.invoke_tool(tool, {
            "id": "call-cancelled",
            "name": "long_read",
            "args": {},
        })

    assert tool.ainvoke.await_count == 1
