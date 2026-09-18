"""All custom fallbacks use the governed loop, never an unchecked compiler."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.dataset import DatasetFile, MountedDataset
from app.domain.models.event import MessageEvent, ToolEvent, ToolStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.agents.execution import ExecutionAgent


def dataset():
    return MountedDataset(dataset_id="fixture", name="Synthetic", data_center_id="fixture",
        data_center_name="Synthetic", sandbox_path="/home/ubuntu/datasets/fixture",
        files=[DatasetFile(path="data/a.csv", size=8), DatasetFile(path="data/b.csv", size=8),
               DatasetFile(path="data/unrelated.csv", size=8)])


def agent_with_capture():
    agent = object.__new__(ExecutionAgent)
    captured = []
    agent.toolkits = []
    agent.get_tool = lambda name: None
    agent.invoke_tool = AsyncMock(side_effect=AssertionError("Fallback adapter cannot dispatch a one-shot program"))
    agent.ask_with_messages = AsyncMock(side_effect=AssertionError("No separate compiler"))

    async def execute(request, **kwargs):
        captured.append((request, kwargs, agent._dataset_fast_path_mode, agent._dataset_intent,
                         agent._dataset_fast_path_tool_names()))
        yield MessageEvent(message='{"success":true,"result":"Supported result","attachments":[]}')

    agent.execute = execute
    return agent, captured


@pytest.mark.asyncio
async def test_authoritative_multi_file_scope_reaches_normal_agent_without_unrelated_inventory():
    agent, captured = agent_with_capture()
    current = Step(id="current", description="Compare selected data", status=ExecutionStatus.RUNNING,
        inputs={"target_files": ["data/a.csv", "data/b.csv"], "artifact_policy": "required",
                "requested_dimensions": ["comparison", "visualization"], "execution_guidance": "Use observed fields."})
    agent._current_plan = Plan(language="en", goal="Compare the selected data", steps=[current])
    original_inputs = dict(current.inputs)
    result = [event async for event in agent._execute_dataset_general_analysis(
        "Continue selected analysis", message=Message(message="Compare these two files", datasets=[dataset()]))]

    assert len(result) == len(captured) == 1
    prompt, kwargs, fast_path, _intent, tools = captured[0]
    assert kwargs == {} and fast_path and "program_run" in tools
    assert "data/a.csv" in prompt and "data/b.csv" in prompt and "unrelated.csv" not in prompt
    assert '"required_dimension_checklist":["comparison","visualization"]' in prompt
    assert "Use observed fields." in prompt and "parser-only" in prompt
    assert "do not ask for another inspection turn" not in prompt
    assert current.inputs == original_inputs and not agent._authoritative_target_files
    agent.invoke_tool.assert_not_awaited()
    agent.ask_with_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_unregistered_selected_target_does_not_broaden_to_whole_dataset():
    agent, captured = agent_with_capture()
    events = [event async for event in agent._execute_dataset_general_analysis("Analyze selected",
        message=Message(message="Analyze selected", datasets=[dataset()]), target_files=["missing.csv"])]
    assert captured == []
    assert json.loads(events[0].message)["success"] is False
    assert "missing.csv" not in events[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["catalog_metadata", "catalog_description", "inventory", "quicklook"])
async def test_each_fallback_enters_shared_tool_scope_without_recursing_routing(route):
    agent, captured = agent_with_capture()
    message = Message(message="Inspect the requested data", datasets=[dataset()])
    if route == "catalog_metadata":
        execution = agent._execute_catalog_metadata(message.message, message=message, language="en", artifact_policy="required")
    elif route == "catalog_description":
        execution = agent._execute_catalog_description(message.message, message=message, language="en", artifact_policy="required")
    elif route == "inventory":
        execution = agent._execute_preferred_inventory(message.message, message=message, language="en", artifact_policy="required")
    else:
        execution = agent._execute_preferred_quicklook(message.message, message=message,
            dataset_intent="analysis", allow_terminal_quicklook=False)
    events = [event async for event in execution]
    assert len(events) == len(captured) == 1
    assert json.loads(events[0].message)["result"] == "Supported result"
    prompt, kwargs, fast_path, intent, tools = captured[0]
    assert kwargs == {} and fast_path is True
    assert "program_run" in tools and "validate uncertain parsing" in prompt
    if route == "quicklook":
        assert "dataset_quicklook" not in tools
        assert not agent._disable_quicklook_retry
    else:
        assert '"artifact_policy":"required"' in prompt
    assert not agent._dataset_fast_path_mode
    agent.invoke_tool.assert_not_awaited()
    agent.ask_with_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_inventory_fallback_loop_blocks_masked_script_before_invocation(monkeypatch):
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings",
        lambda: SimpleNamespace(execution_snapshot_identity_key="synthetic-test-key" * 3))
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()
    agent.toolkits = []
    agent.get_tool = lambda name: SimpleNamespace(name=name, toolkit=SimpleNamespace(name="shell"))
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[{
        "name": "shell_run", "id": "masked", "args": {"id": "masked-session",
        "exec_dir": "/home/ubuntu/output", "command": "python3 inspect.py 2>&1 | tail -80 && ls"}}]))
    agent.ask_with_messages = AsyncMock(return_value=AIMessage(content=json.dumps({"success": False,
        "result": "The masked invocation was not executed; no verified analysis is claimed.", "attachments": []})))
    agent.invoke_tool = AsyncMock(side_effect=AssertionError("Masked script must be intercepted"))
    step = Step(id="inventory", description="Export the selected file inventory", inputs={
        "dataset_intent": "file_structure", "execution_mode": "dataset_fast_path", "artifact_policy": "required",
        "target_files": ["data/a.csv"], "requested_dimensions": ["file_structure"]})
    events = [event async for event in agent.execute_step(Plan(language="en", steps=[step]), step,
        Message(message="Export the selected file inventory", datasets=[dataset()]))]
    blocked = [event for event in events if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED]
    assert len(blocked) == 1 and blocked[0].function_result["blocked_by_policy"] == "program_execution_required"
    assert "data/a.csv" in agent.ask.await_args.args[0]
    assert "data/unrelated.csv" not in agent.ask.await_args.args[0]
    assert step.success is False and "not executed" in step.result
    assert agent.last_execution_outcome["tool_batch_limit"] is None
    agent.invoke_tool.assert_not_awaited()


def test_obsolete_compiler_is_not_an_alternate_execution_capability():
    assert not hasattr(ExecutionAgent, "_compile_dataset_analysis_program")
    assert not hasattr(ExecutionAgent, "_execute_compiled_dataset_analysis")
