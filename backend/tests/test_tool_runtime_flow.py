"""Offline integration of real Cordis, PlanActFlow, selection and model budget."""

import asyncio
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.core.config import Settings
from app.domain.external.model_driver import ModelCapabilities, ModelIdentity
from app.domain.models.event import DoneEvent, MessageEvent, PlanEvent, PlanStatus
from app.domain.models.memory import Memory
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.session import SessionStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services import model_runtime
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.context_budget import estimate_context_tokens
from app.domain.services.domain_presets import get_domain_preset
from app.domain.services.execution_environment import create_agent_execution_snapshot
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.services.tools.pipeline import ToolExecutionInterceptor
from app.domain.services.tools.plugin import default_plugin_directory
from app.domain.services.tools.registry import ToolRegistry
from app.infrastructure.external.llm.chat_model import LangChainModelDriver
from app.infrastructure.external.plugins import NodePluginRuntime
from app.infrastructure.external.plugins.node_runtime import (
    default_execution_contract_directory, default_plugin_host_path,
)
from test_model_driver import FakeClient
from test_model_runtime import TraceStore


class SchemaClient(FakeClient):
    def bind_tools(self, tools, **kwargs):
        # The real provider SDK converts BaseTool instances before handing
        # normalized schemas to ModelDriver; retain that part of the boundary.
        return self.bind(tools=[convert_to_openai_tool(value) for value in tools], **kwargs)


class MemoryRepository:
    def __init__(self):
        self.memories = {}

    async def get_memory(self, agent_id, name):
        return self.memories.get((agent_id, name), Memory()).model_copy(deep=True)

    async def save_memory(self, agent_id, name, memory):
        self.memories[(agent_id, name)] = memory.model_copy(deep=True)


@pytest.fixture
async def flow_factory(monkeypatch):
    host = default_plugin_host_path()
    node = shutil.which("node")
    if not host.is_file() or not node:
        pytest.skip("The built Cordis host and Node are required for this integration")
    settings = Settings(
        _env_file=None, api_key="offline-model-runtime-key", model_provider="openai",
        model_name="offline-model", skills_enabled=False, spill_enabled=False,
        analysis_jobs_enabled=False, tool_selection_mode="on_demand", tool_preset_id="general",
        code_mode_enabled=False, domain_subagents_enabled=False, max_tokens=512,
        execution_snapshot_identity_key="offline-execution-snapshot-key-0123456789abcdef",
    )
    for module in (
        "app.domain.services.agents.base", "app.domain.services.flows.plan_act",
        "app.domain.services.tool_runtime_config", "app.domain.services.execution_identity",
        "app.domain.services.execution_environment", "app.domain.services.model_runtime",
    ):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    monkeypatch.setattr(model_runtime.TokenUsageService, "record_from_message", AsyncMock())

    def model_factory(*_args, **_kwargs):
        return LangChainModelDriver(
            client=SchemaClient(cache=False),
            identity=ModelIdentity(provider="openai", model_name="offline-model"),
            capabilities=ModelCapabilities(tool_calling="adapter"), max_output_tokens=512,
        )

    monkeypatch.setattr("app.domain.services.agents.base.create_chat_model", model_factory)
    runtime = NodePluginRuntime(
        host_path=host, tools_dir=default_plugin_directory(),
        execution_contract_dir=default_execution_contract_directory(),
        node_executable=node, startup_timeout_seconds=15, request_timeout_seconds=5,
        shutdown_timeout_seconds=2,
    )
    await runtime.start()
    flows = []

    def factory(profile=None):
        repository = MemoryRepository()
        flow = PlanActFlow(
            agent_id="offline-agent", user_id="offline-user", agent_repository=repository,
            session_id="offline-session", session_repository=SimpleNamespace(
                find_by_id=AsyncMock(return_value=SimpleNamespace(status=SessionStatus.PENDING)),
                update_status=AsyncMock(), get_events=AsyncMock(return_value=[]),
            ),
            sandbox=SimpleNamespace(), browser=SimpleNamespace(), mcp_tool=MCPToolkit(),
            plugin_runtime=runtime,
            llm_overrides={"agent_profile": profile} if profile is not None else None,
        )
        flows.append(flow)
        return flow

    try:
        yield factory
    finally:
        for flow in flows:
            if flow._tool_execution_disposer:
                flow._tool_execution_disposer()
            await flow.drain_spill_saves()
        await runtime.shutdown()


def _trial_profile(**kwargs):
    return {"name": "Offline profile", "tool_runtime": {
        "preset_id": "general", "selection_mode": "on_demand", **kwargs,
    }}


def _schemas(agent):
    # Real ModelDriver conversion, as used at every BaseAgent model bind.
    return agent._model.bind_tools(agent.get_tools()).kwargs["tools"]


@pytest.mark.asyncio
async def test_default_flow_is_small_but_legacy_profile_retains_full_catalog(flow_factory):
    small = flow_factory()
    legacy = flow_factory({"name": "Pre-Phase-7 profile"})
    assert small.plugin_toolkit.catalog_snapshot.engine == "cordis"
    assert len(small.plugin_toolkit.get_tools()) == 280
    assert len(small._plugin_view.get_tools()) == len(get_domain_preset("general").initial_tools)
    assert len(legacy._plugin_view.get_tools()) == 280
    assert legacy.tool_runtime.selection_mode == "all"
    assert not legacy.tool_runtime.code_mode_enabled and not legacy.tool_runtime.domain_subagents_enabled
    initial, complete = _schemas(small.executor), _schemas(legacy.executor)
    assert len(initial) < 60 < len(complete)
    _, initial_tokens = estimate_context_tokens([], tool_schemas=initial)
    _, complete_tokens = estimate_context_tokens([], tool_schemas=complete)
    assert initial_tokens < complete_tokens / 3
    assert {"dataset_quicklook", "dataset_unpack", "shell_exec"} <= set(ToolRegistry(small.executor.toolkits).tool_names())
    assert small._tool_registry.get_tool("space_ground_track") is not None
    assert small.executor.get_tool("space_ground_track") is None


@pytest.mark.asyncio
async def test_load_in_dataset_fast_path_adds_next_model_schema_without_bypassing_governance(flow_factory):
    flow = flow_factory()
    flow.configure_tool_execution()
    agent = flow.executor
    agent._dataset_fast_path_mode = True
    assert agent.get_tool("tool_catalog_search") is not None
    assert agent.get_tool("tool_catalog_load") is not None
    assert agent.get_tool("table_profile") is None
    await agent.ask_with_messages([HumanMessage(content="Inspect available tools")])
    before = agent._model._client._requests[-1]["tools"]
    assert "table_profile" not in {schema["function"]["name"] for schema in before}
    result = await agent.get_tool("tool_catalog_load").ainvoke({
        "id": "load-one", "name": "tool_catalog_load", "args": {"tool_names": ["table_profile"]},
    })
    assert result.artifact.success
    await agent.ask_with_messages([HumanMessage(content="Use the newly loaded schema")])
    after = agent._model._client._requests[-1]["tools"]
    assert "table_profile" in {schema["function"]["name"] for schema in after}
    assert agent.get_tool("table_profile") is not None
    calls = []

    class Guard(ToolExecutionInterceptor):
        async def guard(self, context):
            calls.append(context.tool_name)

    flow._governed_tool_registry.register_interceptor(Guard())
    flow.plugin_toolkit.call_tool = AsyncMock(return_value=ToolResult(success=True, data={"rows": 3}))
    await agent.get_tool("table_profile").ainvoke({
        "id": "profile-one", "name": "table_profile", "args": {"input_path": "/home/ubuntu/table.csv"},
    })
    assert calls == ["table_profile"]
    flow.plugin_toolkit.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_domain_agents_have_independent_views_memories_and_bounded_shared_ledger(flow_factory):
    flow = flow_factory(_trial_profile(domain_subagents_enabled=True))
    assert set(flow._domain_agents) == {"domain_tabular", "domain_geoscience"}
    table, geo = flow._domain_agents.values()
    assert isinstance(table, ExecutionAgent) and isinstance(geo, ExecutionAgent)
    assert table is not geo and table is not flow.executor
    assert len({table.name, geo.name, flow.executor.name}) == 3
    assert table.max_iterations is None and geo.max_iterations is None
    assert all("domain_" in agent.name for agent in (table, geo))
    for agent in (table, geo):
        names = ToolRegistry(agent.toolkits).tool_names()
        assert not any(name.startswith(("shell_", "file_", "browser_", "mcp_", "message_", "code_mode_", "skill_")) for name in names)
        assert {"tool_catalog_search", "tool_catalog_load", "inspect_dataset_catalog"} <= set(names)
        assert agent.get_tool("dataset_quicklook") is None
    assert flow._domain_tool_views["domain_tabular"].load(["table_pivot"]).success
    assert table.get_tool("table_pivot") is not None
    assert geo.get_tool("table_pivot") is None and flow.executor.get_tool("table_pivot") is None
    store = TraceStore()
    with model_runtime.model_execution_scope(
        user_id="offline-user", session_id="offline-session", task_id="domain-task", store=store, call_limit=2,
    ) as scope:
        await table.ask_with_messages([HumanMessage(content="TABLE_PRIVATE_CONTEXT")])
        await asyncio.create_task(geo.ask_with_messages([HumanMessage(content="GEO_PRIVATE_CONTEXT")]))
        assert scope.ledger.calls == 2 and scope.ledger.charged_tokens == 20
        with pytest.raises(model_runtime.ModelBudgetStopped):
            await flow.executor.ask_with_messages([HumanMessage(content="Parent must share the limit")])
    assert not flow.executor._model._client._requests
    assert {record.task_id for record in store.records.values()} == {"domain-task"}
    roles = {record.role for record in store.records.values() if record.kind == "model_request" and record.status == "succeeded"}
    assert roles == {table.name, geo.name}
    stored_table = flow._repository.memories[("offline-agent", table.name)].model_dump_json()
    stored_geo = flow._repository.memories[("offline-agent", geo.name)].model_dump_json()
    assert "TABLE_PRIVATE_CONTEXT" in stored_table and "GEO_PRIVATE_CONTEXT" not in stored_table
    assert "GEO_PRIVATE_CONTEXT" in stored_geo and "TABLE_PRIVATE_CONTEXT" not in stored_geo


@pytest.mark.asyncio
async def test_flow_routes_real_domain_instances_and_summary_receives_every_step(flow_factory, monkeypatch):
    flow = flow_factory(_trial_profile(domain_subagents_enabled=True))
    plan = Plan(goal="Two domain analyses", steps=[
        Step(id="table", agent="domain_tabular", description="Profile the table"),
        Step(id="geo", agent="domain_geoscience", description="Inspect geographic coordinates"),
    ])
    seen = []

    async def create_plan(_message):
        yield PlanEvent(status=PlanStatus.CREATED, plan=plan)

    def execute_domain(key):
        async def execute(plan_arg, step, _message):
            assert plan_arg is plan
            seen.append((key, step.id))
            step.status, step.success, step.result = ExecutionStatus.COMPLETED, True, f"{key}-evidence"
            if False:
                yield
        return execute

    async def execute_summary(prompt, *args, **kwargs):
        assert flow.executor._current_plan is plan
        assert "domain_tabular-evidence" in prompt and "domain_geoscience-evidence" in prompt
        seen.append(("summary", "complete-plan"))
        yield MessageEvent(message=json.dumps({"message": "Combined evidence", "attachments": []}))

    monkeypatch.setattr(flow.planner, "create_plan", create_plan)
    for key, agent in flow._domain_agents.items():
        monkeypatch.setattr(agent, "execute_step", execute_domain(key))
        monkeypatch.setattr(agent, "compact_memory", AsyncMock())
    monkeypatch.setattr(flow.executor, "execute", execute_summary)
    # summarize() itself remains production code; it renders the full plan and
    # forwards it through the original executor, without a new delegation loop.
    events = [event async for event in flow.run(Message(message="Compare table and geographic evidence"))]
    assert seen == [("domain_tabular", "table"), ("domain_geoscience", "geo"), ("summary", "complete-plan")]
    assert any(isinstance(event, MessageEvent) and event.message == "Combined evidence" for event in events)
    assert isinstance(events[-1], DoneEvent)
    assert plan.status == ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_snapshot_preserves_complete_catalog_models_and_task_initial_selection(flow_factory):
    flow = flow_factory(_trial_profile(domain_subagents_enabled=True))
    flow.configure_tool_execution()
    flow.prepare_execution_environment(Message(message="A new task"))

    def snapshot():
        return create_agent_execution_snapshot(
            task_id="task", session_id="offline-session", flow=flow,
            sandbox=SimpleNamespace(), dataset_ids=[], resolution=None, llm_overrides=None,
            requested_mcp_servers=[], requested_skill_count=0,
        )

    first = snapshot()
    assert first.catalog.tool_count == 280 and first.catalog.plugin_count == 15
    assert first.toolset.tool_count > 280
    assert {agent.name for agent in flow._domain_agents.values()} <= {model.role for model in first.models}
    assert first.toolset.selection.initial_tool_count == len(get_domain_preset("general").initial_tools)
    assert first.toolset.selection.domain_agent_count == 2
    assert first.toolset.selection.domain_subagents_enabled
    assert flow._plugin_view.load(["space_ground_track"]).success
    assert flow._domain_tool_views["domain_tabular"].load(["table_pivot"]).success
    assert snapshot().fingerprint == first.fingerprint  # immutable initial provenance
    flow.prepare_execution_environment(Message(message="Next task"))
    assert flow.executor.get_tool("space_ground_track") is None
    assert flow._domain_agents["domain_tabular"].get_tool("table_pivot") is None
    assert snapshot().fingerprint == first.fingerprint
    legacy = flow_factory({"name": "legacy"})
    legacy.configure_tool_execution()
    legacy.prepare_execution_environment(Message(message="A new task"))
    other = create_agent_execution_snapshot(
        task_id="task", session_id="offline-session", flow=legacy,
        sandbox=SimpleNamespace(), dataset_ids=[], resolution=None, llm_overrides=None,
        requested_mcp_servers=[], requested_skill_count=0,
    )
    assert other.fingerprint != first.fingerprint
    assert other.catalog.revision == first.catalog.revision
