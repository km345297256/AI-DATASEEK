from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.tools import tool

from app.domain.models.mcp_config import MCPConfig
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.interceptors import ToolExecutionDeniedError
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.registry import ToolRegistry


class _RuntimeToolkit(BaseToolkit):
    name: str = "runtime"

    @tool
    async def runtime_read(self) -> ToolResult:
        """Read one runtime fixture."""
        return ToolResult(success=True, data={"ok": True})


class _CatalogToolkit(BaseToolkit):
    name: str = "plugin"
    catalog_revision: str = "catalog-revision"


def _minimal_flow(*toolkits) -> PlanActFlow:
    flow = object.__new__(PlanActFlow)
    flow._agent_id = "agent-1"
    flow._session_id = "session-1"
    flow._user_id = "user-1"
    flow.plugin_toolkit = _CatalogToolkit()
    flow._non_plugin_toolkits = list(toolkits)
    flow._tool_registry = ToolRegistry([flow.plugin_toolkit, *toolkits])
    flow._tool_execution_disposer = None
    return flow


@pytest.mark.asyncio
async def test_plan_act_installs_one_production_bundle_and_pins_tool_policy():
    toolkit = _RuntimeToolkit()
    flow = _minimal_flow(toolkit)

    flow.configure_tool_execution()

    names = [
        type(interceptor).__name__
        for interceptor in toolkit.tool_execution_pipeline.interceptors
    ]
    assert names == [
        "StructuredToolTraceInterceptor",
        "ToolPolicyGuardInterceptor",
        "ToolTimeoutInterceptor",
        "ToolConcurrencyInterceptor",
    ]

    registered = await toolkit.get_tool("runtime_read").ainvoke({
        "name": "runtime_read",
        "id": "registered-call",
        "args": {},
    })
    assert registered.artifact.success is True
    assert registered.artifact.data == {"ok": True}

    executed = False

    async def execute(_context):
        nonlocal executed
        executed = True
        return "unexpected"

    with pytest.raises(ToolExecutionDeniedError):
        await toolkit.tool_execution_pipeline.invoke(
            tool=SimpleNamespace(name="late_unregistered_tool"),
            tool_call={
                "name": "late_unregistered_tool",
                "id": "late-call",
                "args": {},
            },
            execute=execute,
        )
    assert executed is False

    # Reconfiguration replaces the previous immutable snapshot/bundle rather
    # than stacking duplicate middleware on shared toolkit instances.
    flow.configure_tool_execution()
    assert len(toolkit.tool_execution_pipeline.interceptors) == 4


@pytest.mark.asyncio
async def test_reconfiguration_pins_newly_discovered_tools_without_stacking():
    toolkit = _RuntimeToolkit()
    toolkit.set_enabled(False)
    flow = _minimal_flow(toolkit)
    flow.configure_tool_execution()

    toolkit.set_enabled(True)
    runtime_tool = toolkit.get_tool("runtime_read")
    assert runtime_tool is not None
    with pytest.raises(ToolExecutionDeniedError):
        await runtime_tool.ainvoke({
            "name": "runtime_read",
            "id": "before-discovery-refresh",
            "args": {},
        })

    flow.configure_tool_execution()
    result = await runtime_tool.ainvoke({
        "name": "runtime_read",
        "id": "after-discovery-refresh",
        "args": {},
    })

    assert result.artifact.success is True
    assert len(toolkit.tool_execution_pipeline.interceptors) == 4


def test_plan_act_fails_closed_for_toolkit_without_execution_pipeline():
    unsupported = SimpleNamespace(
        name="future-domain",
        get_tools=lambda: [{
            "type": "function",
            "function": {
                "name": "future_domain_read",
                "parameters": {"type": "object"},
            },
        }],
        get_tool=lambda _name: None,
    )
    flow = _minimal_flow(unsupported)

    with pytest.raises(ValueError, match="future-domain"):
        flow.configure_tool_execution()


@pytest.mark.asyncio
async def test_mcp_initialization_configures_execution_after_dynamic_discovery():
    calls = Mock()
    flow = SimpleNamespace(
        validate_plugin_tool_names=Mock(side_effect=lambda: calls("validate")),
        configure_tool_execution=Mock(
            side_effect=lambda **kwargs: calls("configure", **kwargs)
        ),
    )
    mcp_tool = SimpleNamespace(
        cleanup=AsyncMock(),
        initialized=AsyncMock(
            side_effect=lambda *_args, **_kwargs: calls("initialized")
        ),
    )
    repository = SimpleNamespace(
        get_mcp_config=AsyncMock(return_value=MCPConfig(mcpServers={})),
    )
    trace_sink = object()
    runner = object.__new__(AgentTaskRunner)
    runner._user_id = "user-1"
    runner._mcp_tool = mcp_tool
    runner._mcp_repository = repository
    runner._flow = flow
    runner._tool_trace_sink = trace_sink

    await runner._initialize_mcp_tool([])

    assert [entry.args[0] for entry in calls.call_args_list] == [
        "initialized",
        "validate",
        "configure",
    ]
    flow.configure_tool_execution.assert_called_once_with(trace_sink=trace_sink)
