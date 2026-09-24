"""Cross-layer recovery, adaptive grants and progress-message regressions."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import json

import pytest
from langchain.messages import AIMessage, ToolMessage

from app.domain.models.event import MessageEvent, StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent, _is_retryable_llm_error
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.analysis_budget import AnalysisBudgetService, BudgetPolicy, BudgetEvidence
from app.domain.services.analysis_recovery import AnalysisRecoveryContext, analysis_recovery_scope
from app.domain.services.execution_evidence import register_shell_attempt, consume_shell_receipt
from app.domain.services.model_runtime import analysis_budget_scope, current_analysis_budget
from test_analysis_budget import MemoryBudgetRepository


def receipt(attempt, code):
    return {"version": 1, "operation_id": attempt.operation_id, "command_digest": attempt.command_digest,
            "server_instance_id": "a" * 32, "state": "exited", "returncode": code,
            "process_tree_quiescent": True}


@pytest.mark.asyncio
async def test_confirmed_failed_operation_does_not_poison_later_completed_analysis():
    agent = object.__new__(BaseAgent)
    agent.max_retries = 1
    sandbox = SimpleNamespace(id="box", supports_execution_receipts=True,
        exec_command_tracked=AsyncMock(), shell_operation_status=AsyncMock())

    async def operation(call):
        attempt = register_shell_attempt(sandbox, "same-shell", "/home/ubuntu", "bounded command " + call["id"])
        code = 1 if call["id"] == "first" else 0
        result = consume_shell_receipt(ToolResult(success=code == 0,
            data={"execution_receipt": receipt(attempt, code), "returncode": code}), attempt)
        return ToolMessage(tool_call_id=call["id"], content=result.model_dump_json(), artifact=result)

    tool = SimpleNamespace(name="trusted-operation", ainvoke=operation)
    first = await agent.invoke_tool(tool, {"id": "first", "args": {}})
    assert first.status == "error"
    assert agent.last_execution_outcome["side_effect_state"] == "confirmed_terminal"
    assert not agent.last_execution_outcome["has_unconfirmed_tool_execution"]
    second = await agent.invoke_tool(tool, {"id": "second", "args": {}})
    assert second.status == "success"
    assert not agent.last_execution_outcome["has_unconfirmed_tool_execution"]
    assert not agent.last_execution_outcome["execution_evidence"]["replay_safe"]


@pytest.mark.asyncio
async def test_original_unknown_is_resolved_by_query_not_reexecution_or_unrelated_success():
    agent = object.__new__(BaseAgent)
    sandbox = SimpleNamespace(id="box", supports_execution_receipts=True,
        exec_command_tracked=AsyncMock(), shell_operation_status=AsyncMock())
    operations = []

    async def disconnected(call):
        attempt = register_shell_attempt(sandbox, "shell", "/home/ubuntu", "command")
        operations.append(attempt)
        raise OSError("lost response")

    tool = SimpleNamespace(name="trusted-operation", ainvoke=AsyncMock(side_effect=disconnected))
    await agent.invoke_tool(tool, {"id": "first", "args": {}})
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"]
    sandbox.shell_operation_status.return_value = ToolResult(success=True, data=receipt(operations[0], 0))
    await agent._reconcile_execution()
    assert not agent.last_execution_outcome["has_unconfirmed_tool_execution"]
    assert agent.last_execution_outcome["side_effect_state"] == "confirmed_terminal"
    tool.ainvoke.assert_awaited_once()
    sandbox.shell_operation_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_host_progress_is_not_parsed_as_terminal_execution_result():
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()
    agent._parse_json = AsyncMock(side_effect=json.loads)
    agent._repair_execution_result = AsyncMock(side_effect=AssertionError("progress is not broken JSON"))

    async def fake_execute(_message):
        yield MessageEvent(message="正在核验执行状态…", metadata={"analysis_progress": {"stage": "verifying_execution"}})
        yield MessageEvent(message='{"success":true,"result":"Real completed analysis","attachments":[]}')

    agent.execute = fake_execute
    step = Step(description="Analyze")
    events = [event async for event in agent.execute_step(Plan(steps=[step]), step, Message(message="Analyze"))]
    terminal = [event for event in events if isinstance(event, StepEvent) and event.status != StepStatus.STARTED]
    assert len(terminal) == 1 and terminal[0].step.success
    assert any(isinstance(event, MessageEvent) and event.metadata.get("analysis_progress") for event in events)
    agent._repair_execution_result.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_real_agent_budget_boundary_continues_in_place_or_stops_before_dispatch(approve):
    repository = MemoryBudgetRepository()
    service = AnalysisBudgetService(repository, policy=BudgetPolicy(initial_batches=2, hard_batches=4, max_grants=1))
    handle = await service.open(user_id="owner", session_id="session", origin_input_id="1", scope_digest="a" * 64)
    agent = object.__new__(BaseAgent)
    agent.max_iterations = 2
    agent.max_retries = 0
    calls = []

    async def invoke(call):
        calls.append(call["id"])
        return ToolMessage(tool_call_id=call["id"], name="operation", content="evidence",
                           artifact=ToolResult(success=True, data={"value": len(calls)}))

    tool = SimpleNamespace(name="operation", toolkit=SimpleNamespace(name="test"), ainvoke=invoke)
    agent.get_tool = lambda _: tool
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    loops = [AIMessage(content="", tool_calls=[{"name": "operation", "args": {}, "id": str(index)}]) for index in range(3)]
    agent.ask = AsyncMock(return_value=loops[0])
    agent.ask_with_messages = AsyncMock(side_effect=[loops[1], loops[2], AIMessage(content="verified result")])
    reviews = []

    async def review(next_calls, *, review_needed):
        reviews.append(review_needed)
        return BudgetEvidence(scope_digest="a" * 64, confirmed_progress_units=int(approve),
            progress_digest="b" * 64, next_action_bounded=True)

    with analysis_budget_scope(handle), analysis_recovery_scope(AnalysisRecoveryContext(review=review)):
        events = [event async for event in agent.execute("Complete the original task")]
    assert calls == (["0", "1", "2"] if approve else ["0", "1"])
    assert reviews == [False, False, True]
    assert (await handle.snapshot()).grant_count == int(approve)
    if approve:
        assert agent.last_execution_outcome["code"] == "completed"
        assert not any(isinstance(event, MessageEvent) and "额度" in event.message for event in events)
    else:
        assert agent.last_execution_outcome["code"] == "budget_no_confirmed_progress"
        assert agent.ask_with_messages.await_args.kwargs["allow_tools"] is False
        not_executed = agent.ask_with_messages.await_args.args[0][0]
        assert not_executed.tool_call_id == "2" and "NOT executed" in not_executed.content
    assert current_analysis_budget() is None


@pytest.mark.parametrize("code,expected", [("insufficient_quota", False), ("insufficient_balance", False),
    ("billing_hard_limit_reached", False), ("rate_limit_exceeded", True)])
def test_provider_quota_is_not_treated_as_local_budget_or_rate_limit(code, expected):
    import httpx
    from openai import RateLimitError
    error = RateLimitError("provider error", response=httpx.Response(429, request=httpx.Request("POST", "https://example.invalid")),
                           body={"error": {"code": code}})
    assert _is_retryable_llm_error(error) is expected


@pytest.mark.asyncio
async def test_runtime_scope_is_private_restored_and_bound_to_original_input():
    from app.domain.services.agent_task_runner import AgentTaskRunner
    from app.domain.services.analysis_recovery import current_analysis_recovery
    from app.domain.services.input_delivery import InputLeaseLost
    repository = MemoryBudgetRepository()
    runner = object.__new__(AgentTaskRunner)
    runner._session_id, runner._user_id = "session", "owner"
    runner._sandbox = SimpleNamespace(id="sandbox")
    runner._session_repository = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=SimpleNamespace(llm_overrides={})))
    runner._analysis_source_fingerprints = None
    runner._analysis_budget_service = AnalysisBudgetService(repository, policy=BudgetPolicy())
    runner._input_delivery = SimpleNamespace(_require_live=AsyncMock(), mark_analysis_started=AsyncMock())
    runner._accepted_input_key = "original-input"
    message = Message(message="original analysis")
    with ExitStack() as stack:
        await runner._open_analysis_runtime(message, 12, stack)
        handle = current_analysis_budget()
        assert handle is not None and current_analysis_recovery() is not None
        assert message._budget_lineage_id == handle.lineage_id
        assert "budget" not in message.model_dump_json()
        runner._accepted_input_key = "new-input"
        with pytest.raises(InputLeaseLost):
            await handle.snapshot()
    assert current_analysis_budget() is None and current_analysis_recovery() is None


@pytest.mark.asyncio
async def test_step_contract_prevents_unnecessary_grants_and_attachments_fail_closed():
    from app.domain.models.analysis_outcome import DeliverableRequirement
    from app.domain.models.plan import ExecutionStatus
    from app.domain.services.agent_task_runner import AgentTaskRunner
    runner = object.__new__(AgentTaskRunner)
    runner._artifact_baseline_paths = set()
    runner._analysis_source_fingerprints = None
    tool = SimpleNamespace(name="operation")
    runner._flow = SimpleNamespace(plan=Plan(steps=[Step(status=ExecutionStatus.RUNNING,
        deliverables=[DeliverableRequirement(kind="image")])]),
        executor=SimpleNamespace(get_tool=lambda name: tool, _blocked_runtime_install_reason=lambda call: None))
    path = "/home/ubuntu/output/chart.png"
    runner._list_sandbox_artifacts = AsyncMock(return_value=[path])
    runner._sandbox = SimpleNamespace(validate_artifacts=AsyncMock(return_value=ToolResult(success=True, data={
        "version": 1, "files": [{"path": path, "kind": "image", "valid": True, "sha256": "a" * 64, "size": 10}]})))
    recovery = AnalysisRecoveryContext(review=AsyncMock())
    handle = SimpleNamespace(scope_digest="b" * 64)
    result = await runner._review_analysis_budget(Message(message="Make a chart"), recovery, handle,
        [{"name": "operation", "args": {}}], review_needed=True)
    assert result.confirmed_progress_units == 1
    assert not result.next_action_bounded  # Required current-step artifact is already complete.
    runner._flow.plan.steps[0].deliverables[0].min_count = 2
    result = await runner._review_analysis_budget(Message(message="Make charts"), recovery, handle,
        [{"name": "operation", "args": {}}], review_needed=True)
    assert result.next_action_bounded
    result = await runner._review_analysis_budget(Message(message="Make charts", attachments=["/home/ubuntu/uploads/data.csv"]),
        recovery, handle, [{"name": "operation", "args": {}}], review_needed=True)
    assert not result.next_action_bounded


def agent_with_messages(first, later, tools):
    agent = object.__new__(BaseAgent)
    agent.max_iterations = 1
    agent.max_retries = 0
    agent.ask = AsyncMock(return_value=first)
    agent.ask_with_messages = AsyncMock(side_effect=later)
    agent.get_tool = lambda name: tools.get(name)
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    return agent


@pytest.mark.asyncio
async def test_new_task_keeps_progress_beyond_all_removed_limits_and_counts_usage():
    repository = MemoryBudgetRepository()
    service = AnalysisBudgetService(repository, policy=BudgetPolicy.from_settings(SimpleNamespace()))
    handle = await service.open(user_id="owner", session_id="session", origin_input_id="unlimited",
                                scope_digest="a" * 64)
    sandbox = SimpleNamespace(id="box", supports_execution_receipts=True,
        exec_command_tracked=AsyncMock(), shell_operation_status=AsyncMock())
    executed = []

    async def operation(call):
        executed.append(call["id"])
        current = register_shell_attempt(sandbox, "shell", "/home/ubuntu", "operation " + call["id"])
        result = consume_shell_receipt(ToolResult(success=True,
            data={"execution_receipt": receipt(current, 0)}), current)
        return ToolMessage(tool_call_id=call["id"], name="operation", content="verified", artifact=result)

    tool = SimpleNamespace(name="operation", toolkit=SimpleNamespace(name="test"), ainvoke=operation)
    batches = [AIMessage(content="", tool_calls=[{"name": "operation", "args": {}, "id": str(index)}])
               for index in range(140)]
    agent = agent_with_messages(batches[0], [*batches[1:], AIMessage(content="complete")], {"operation": tool})
    with analysis_budget_scope(handle):
        events = [event async for event in agent.execute("Complete the analysis", max_iterations=1)]
    assert len(executed) == 140
    assert agent.last_execution_outcome["code"] == "completed"
    assert agent.last_execution_outcome["tool_batch_limit"] is None
    assert agent.last_execution_outcome["tool_hard_limit"] is None
    assert (await handle.snapshot()).tool_batches_used == 140
    assert all(call.kwargs.get("allow_tools", True) for call in agent.ask_with_messages.await_args_list)
    assert not any("额度" in event.message for event in events if isinstance(event, MessageEvent))
    assert "hard limit" not in agent.ask.await_args.args[0]


@pytest.mark.asyncio
async def test_unresolvable_execution_stops_after_real_queries_without_model_spin_or_replay():
    sandbox = SimpleNamespace(id="box", supports_execution_receipts=True,
        exec_command_tracked=AsyncMock(), shell_operation_status=AsyncMock(return_value=ToolResult(success=False)))
    executed = []

    async def lost(call):
        executed.append(call["id"])
        register_shell_attempt(sandbox, "shell", "/home/ubuntu", "original operation")
        raise OSError("response unavailable")

    failing = SimpleNamespace(name="operation", toolkit=SimpleNamespace(name="test"), ainvoke=lost)
    writer = SimpleNamespace(name="next_write", toolkit=SimpleNamespace(name="test"), ainvoke=AsyncMock())
    first = AIMessage(content="", tool_calls=[{"name": "operation", "args": {}, "id": "original"}])
    next_write = AIMessage(content="", tool_calls=[{"name": "next_write", "args": {}, "id": "next"}])
    agent = agent_with_messages(first, [next_write, AIMessage(content="Could not verify the original execution")],
                                {"operation": failing, "next_write": writer})
    events = [event async for event in agent.execute("Analyze")]
    assert executed == ["original"]
    writer.ainvoke.assert_not_awaited()
    assert sandbox.shell_operation_status.await_count == 2
    assert agent.ask_with_messages.await_count == 2
    assert agent.ask_with_messages.await_args.kwargs["allow_tools"] is False
    assert agent.last_execution_outcome["code"] == "tool_execution_unknown"
    assert agent.last_execution_outcome["has_unconfirmed_tool_execution"] is True
    assert not any((event.metadata or {}).get("analysis_progress") for event in events if isinstance(event, MessageEvent))


@pytest.mark.asyncio
async def test_healthy_running_operation_can_be_observed_more_than_two_times():
    from app.domain.services.tools.shell import ShellToolkit
    from test_execution_evidence import Client, Response, sandbox

    class RunningClient(Client):
        waits = 0
        async def post(self, url, *, json, **kwargs):
            if url.endswith("/wait"):
                self.waits += 1
                return Response({"status": "completed" if self.waits >= 6 else "running",
                                 "returncode": 0 if self.waits >= 6 else None})
            if url.endswith("/operation-status"):
                return Response(self.proof(terminal=self.waits >= 6))
            return await super().post(url, json=json, **kwargs)

    client = RunningClient(returncode=0)
    toolkit = ShellToolkit(sandbox(client))
    first = AIMessage(content="", tool_calls=[{"name": "shell_exec", "id": "exec",
        "args": {"id": "shell", "exec_dir": "/tmp", "command": "some operation"}}])
    waits = [AIMessage(content="", tool_calls=[{"name": "shell_wait", "id": f"wait-{index}",
                                               "args": {"id": "shell", "seconds": 1}}]) for index in range(6)]
    agent = agent_with_messages(first, [*waits, AIMessage(content="done")],
                                {name: toolkit.get_tool(name) for name in ["shell_exec", "shell_wait"]})
    events = [event async for event in agent.execute("Wait for this original operation", max_iterations=1)]
    assert client.waits == 6
    assert agent.last_execution_outcome["code"] == "completed"
    assert not agent.last_execution_outcome["has_unconfirmed_tool_execution"]
    assert not agent.last_execution_outcome["execution_evidence"]["replay_safe"]
    assert not any((event.metadata or {}).get("analysis_progress") for event in events if isinstance(event, MessageEvent))
