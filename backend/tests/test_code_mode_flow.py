"""Exercise the opt-in interpreter through the production Agent/tool wiring."""
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.event import ToolEvent
from app.domain.models.tool_result import ToolResult
from app.domain.services.code_mode import CodeModeToolCallError, CodeModeValidationError
from app.domain.services.model_runtime import ModelBudgetStopped
from app.domain.services.tools.pipeline import ToolExecutionInterceptor
from app.domain.services.tools.spill_projection import SPILL_PROJECTION_KEY
from test_tool_runtime_flow import flow_factory


def _profile(enabled=True):
    return {"tool_runtime": {"preset_id": "general", "selection_mode": "on_demand", "code_mode_enabled": enabled}}


@pytest.mark.asyncio
async def test_code_mode_requires_opt_in_load_and_each_inner_pipeline(flow_factory):
    disabled = flow_factory(_profile(False))
    assert disabled.executor.get_tool("code_mode_run") is None
    flow = flow_factory(_profile())
    flow.configure_tool_execution()
    flow.executor._dataset_fast_path_mode = True
    code_tool = flow.executor.get_tool("code_mode_run")
    assert code_tool is not None
    assert set(flow._code_mode_tool_names) == {
        "data_format_inspect", "hierarchical_store_inspect", "cf_semantics_validate", "workbook_inspect", "table_profile",
    }
    flow.plugin_toolkit.call_tool = AsyncMock(return_value=ToolResult(success=True, data={"rows": 3}))
    program = 'r = await tools.call("table_profile", {"input_path": "/home/ubuntu/datasets/test/table.csv"})\nr'
    with pytest.raises(CodeModeToolCallError):
        await code_tool.ainvoke({"id": "not-loaded", "args": {"code": program}})
    flow.plugin_toolkit.call_tool.assert_not_awaited()
    await flow.executor.get_tool("tool_catalog_load").ainvoke({
        "id": "load", "args": {"tool_names": ["table_profile"]},
    })
    calls = []

    class Observe(ToolExecutionInterceptor):
        async def guard(self, context):
            calls.append((context.tool_name, context.tool_call_id))

    flow._governed_tool_registry.register_interceptor(Observe())
    result = await code_tool.ainvoke({"id": "code-ok", "args": {"code": program}})
    assert [name for name, _ in calls] == ["code_mode_run", "table_profile"]
    assert result.artifact.data["call_count"] == 1
    assert result.artifact.data["output"]["data"] == {"rows": 3}
    assert result.artifact.data["steps"][0]["call_id"] == calls[1][1]
    flow.plugin_toolkit.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_code_mode_uses_spill_projection_instead_of_private_artifact(flow_factory):
    flow = flow_factory(_profile())
    flow.configure_tool_execution()
    flow.plugin_toolkit.call_tool = AsyncMock(return_value=ToolResult(success=True, data={"secret": "PRIVATE_RAW"}))

    class Project(ToolExecutionInterceptor):
        async def result(self, context, result):
            result.additional_kwargs[SPILL_PROJECTION_KEY] = ToolResult(success=True, data={"bounded": "public"})
            return result

    flow.plugin_toolkit.tool_execution_pipeline.register(Project())
    result = await flow.executor.get_tool("code_mode_run").ainvoke({"id": "projection", "args": {
        "code": 'r = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/test/table.csv"]})\nr',
    }})
    assert result.artifact.data["output"]["data"] == {"bounded": "public"}
    assert "PRIVATE_RAW" not in result.content


@pytest.mark.asyncio
async def test_code_mode_preserves_governance_cancellation_and_rejects_write_tools(flow_factory):
    flow = flow_factory(_profile())
    flow.configure_tool_execution()
    flow.plugin_toolkit.call_tool = AsyncMock()
    code_tool = flow.executor.get_tool("code_mode_run")
    with pytest.raises(CodeModeValidationError):
        await code_tool.ainvoke({"id": "forbidden", "args": {
            "code": 'r = await tools.call("data_format_inspect", {"input_paths": []})\nx = await tools.call("table_extract", {})\nx',
        }})
    flow.plugin_toolkit.call_tool.assert_not_awaited()
    signal = ModelBudgetStopped("task_call_budget_exceeded")

    class Stop(ToolExecutionInterceptor):
        async def guard(self, context):
            raise signal

    flow.plugin_toolkit.tool_execution_pipeline.register(Stop())
    with pytest.raises(ModelBudgetStopped) as caught:
        await code_tool.ainvoke({"id": "cancelled", "args": {
            "code": 'r = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/test/table.csv"]})\nr',
        }})
    assert caught.value is signal
    flow.plugin_toolkit.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_sse_retains_source_identity_but_never_source_program(flow_factory):
    flow = flow_factory(_profile())
    flow.configure_tool_execution()
    flow.plugin_toolkit.call_tool = AsyncMock(return_value=ToolResult(success=True, data={"ok": True}))
    source = 'private = "PRIVATE_DATASET_VALUE"\nr = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/test/table.csv"]})\nr'
    agent = flow.executor
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[{
        "name": "code_mode_run", "id": "code-event", "args": {"code": source},
    }]))
    agent.ask_with_messages = AsyncMock(return_value=AIMessage(content='{"success":true,"result":"done","attachments":[]}'))
    events = [event async for event in agent.execute("Inspect the fixture")]
    tool_events = [event for event in events if isinstance(event, ToolEvent)]
    assert len(tool_events) == 2
    assert all(event.function_args["mode"] == "restricted_code_mode" for event in tool_events)
    assert all(len(event.function_args["source_hmac"]) == 64 for event in tool_events)
    assert tool_events[0].function_args == tool_events[1].function_args
    assert all("PRIVATE_DATASET_VALUE" not in event.model_dump_json() for event in tool_events)
    flow.plugin_toolkit.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_code_mode_inner_calls_keep_individual_jobs_and_read_only_authorization(flow_factory, monkeypatch):
    from app.domain.models.analysis_job import AnalysisJobStatus
    from app.domain.services.analysis_job_service import AnalysisJobService, _tool_call_ref
    from app.domain.services.flows import plan_act
    from test_analysis_job_service import InMemoryAnalysisJobRepository
    from test_tool_authorization_integration import services

    settings = plan_act.get_settings().model_copy(update={"analysis_jobs_enabled": True})
    monkeypatch.setattr(plan_act, "get_settings", lambda: settings)
    flow = flow_factory(_profile())
    jobs = InMemoryAnalysisJobRepository()
    flow._analysis_job_service = AnalysisJobService(jobs)
    flow._tool_approval_service, flow._credential_service, approvals = services()
    flow._analysis_job_identity_provider = lambda: {
        "task_id": "code-task", "execution_snapshot_id": "code-task",
        "catalog_revision": flow.plugin_toolkit.catalog_revision,
    }
    published = []

    async def publish(view, context):
        published.append((context.tool_call_id, view.status))

    flow._analysis_job_event_sink = publish
    flow.configure_tool_execution()
    flow.plugin_toolkit.call_tool = AsyncMock(return_value=ToolResult(success=True, data={"ok": True}))
    result = await flow.executor.get_tool("code_mode_run").ainvoke({"id": "code-jobs", "args": {
        "code": 'first = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/test/table.csv"]})\nsecond = await tools.call("data_format_inspect", {"input_paths": ["/home/ubuntu/datasets/test/table.csv"]})\n{"first": first, "second": second}',
    }})
    records = list(jobs.records.values())
    assert len(records) == 2
    assert all(record.status == AnalysisJobStatus.SUCCEEDED for record in records)
    assert all(record.execution_snapshot_id == "code-task" for record in records)
    assert {record.tool_call_ref for record in records} == {
        _tool_call_ref(step["call_id"]) for step in result.artifact.data["steps"]
    }
    assert {call_id for call_id, _ in published} == {
        step["call_id"] for step in result.artifact.data["steps"]
    }
    assert not approvals.records  # The five admitted tools need no elevated consent.
    assert flow.plugin_toolkit.call_tool.await_count == 2


@pytest.mark.asyncio
async def test_code_mode_rejects_invalid_inner_arguments_before_admission(flow_factory):
    flow = flow_factory(_profile())
    flow.configure_tool_execution()
    flow.plugin_toolkit.call_tool = AsyncMock()
    admitted = []

    class Observe(ToolExecutionInterceptor):
        async def pre_execute(self, context):
            admitted.append(context.tool_name)

    flow.plugin_toolkit.tool_execution_pipeline.register(Observe())
    with pytest.raises(CodeModeToolCallError):
        await flow.executor.get_tool("code_mode_run").ainvoke({"id": "invalid-inner", "args": {
            "code": 'r = await tools.call("data_format_inspect", {"input_paths": []})\nr',
        }})
    flow.plugin_toolkit.call_tool.assert_not_awaited()
    assert admitted == []
