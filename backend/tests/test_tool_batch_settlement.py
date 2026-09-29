"""Completed observations survive cancellation, policy and delivery failures."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, ToolMessage
from langchain.tools import tool
from pydantic import PrivateAttr

from app.domain.models.event import ToolEvent, ToolStatus, WaitEvent
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tool_settlement import tool_settlement_scope
from app.domain.services.tools.base import BaseToolkit, tool_execution_contract
from app.domain.services.tools.pipeline import ToolExecutionInterceptor, ToolExecutionPipeline
from test_tool_response_protocol_recovery import make_agent


class SettlementToolkit(BaseToolkit):
    name: str = "settlement"
    _calls: list[str] = PrivateAttr(default_factory=list)
    _cancel_value: str | None = PrivateAttr(default=None)

    @tool_execution_contract(effects=("sandbox_write",))
    @tool
    async def write_probe(self, value: str) -> ToolResult:
        """Record a synthetic local execution, without external side effects."""
        self._calls.append(value)
        if value == self._cancel_value:
            raise asyncio.CancelledError("synthetic cancellation")
        return ToolResult(success=True, data={"observation": value})


def setup_agent(monkeypatch, *, cancel="B", agent_type=BaseAgent, calls=("A", "B", "C")):
    message = AIMessage(content="", tool_calls=[
        {"name": "write_probe", "args": {"value": value}, "id": value} for value in calls
    ])
    agent, _, requests, _ = make_agent(monkeypatch, [message, AIMessage(content="done")], agent_type=agent_type)
    toolkit = SettlementToolkit()
    toolkit._cancel_value = cancel
    agent.toolkits = [toolkit]
    snapshots = []

    async def save(_agent, _name, memory):
        snapshots.append(deepcopy(memory))

    agent._repository.save_memory = AsyncMock(side_effect=save)
    return agent, toolkit, snapshots, requests


def responses(memory):
    return [message for message in memory.messages if isinstance(message, ToolMessage)]


def terminal(events):
    return {event.tool_call_id: event for event in events
            if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED}


@pytest.mark.asyncio
async def test_completed_a_cancelled_b_undispatched_c_have_distinct_durable_outcomes(monkeypatch):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch)
    events = []
    with pytest.raises(asyncio.CancelledError):
        async for event in agent.execute("synthetic request"):
            events.append(event)
            if isinstance(event, ToolEvent) and event.tool_call_id == "A" and event.status == ToolStatus.CALLED:
                assert [item.tool_call_id for item in responses(snapshots[-1])] == ["A"]
    assert toolkit._calls == ["A", "B"]
    result = responses(snapshots[-1])
    assert [item.tool_call_id for item in result] == ["A", "B", "C"]
    assert result[0].status == "success" and "A" in result[0].content
    assert json.loads(result[1].content)["data"]["side_effect_state"] == "unknown"
    assert json.loads(result[2].content)["data"]["side_effect_state"] == "not_started"
    assert set(terminal(events)) == {"A", "B", "C"}
    assert agent._tool_execution_ledger.call_summary("B")["pending_execution"]
    assert not agent._tool_execution_ledger.call_summary("C")["pending_execution"]
    assert agent.last_execution_outcome["code"] == "cancelled"
    projection, _ = agent._repair_tool_call_history(agent.memory.messages)
    assert next(item for item in projection if isinstance(item, ToolMessage) and item.tool_call_id == "A").status == "success"


@pytest.mark.asyncio
async def test_all_results_append_once_and_sanitized_synthesis_is_request_only(monkeypatch):
    class SynthesisAgent(BaseAgent):
        def _tool_free_completion_instruction(self, results):
            return "Synthesize the observations"

        def _tool_free_completion_tool_responses(self, results, original):
            return [ToolMessage(tool_call_id=item.tool_call_id, name=item.name, content="sanitized") for item in original]

    agent, toolkit, snapshots, requests = setup_agent(monkeypatch, cancel=None, agent_type=SynthesisAgent)
    _events = [event async for event in agent.execute("synthetic")]
    assert toolkit._calls == ["A", "B", "C"]
    assert [item.tool_call_id for item in responses(agent.memory)] == ["A", "B", "C"]
    assert all("observation" in item.content for item in responses(snapshots[-1]))
    assert [item.content for item in requests[-1] if isinstance(item, ToolMessage)] == ["sanitized"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["policy", "result_projection", "progress"])
async def test_result_survives_exceptions_after_a_observation(monkeypatch, stage):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch, cancel=None)
    if stage == "policy":
        original = agent._blocked_runtime_install_reason
        def block(call):
            if call["id"] == "B":
                raise RuntimeError("private policy error")
            return original(call)
        agent._blocked_runtime_install_reason = block
    elif stage == "result_projection":
        original = agent._tool_result_for_memory
        count = 0
        def project(*args):
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("transient safe projection failure")
            return original(*args)
        agent._tool_result_for_memory = project
    else:
        monkeypatch.setattr("app.domain.services.agents.base.program_diagnostic_read_digest",
                            lambda *args: (_ for _ in ()).throw(RuntimeError("private progress error")))
    events = []
    with pytest.raises(RuntimeError):
        async for event in agent.execute("synthetic"):
            events.append(event)
    assert toolkit._calls == ["A"]
    assert responses(snapshots[-1])[0].status == "success"
    assert terminal(events)["A"].function_result.success
    for item in responses(snapshots[-1])[1:]:
        assert json.loads(item.content)["data"]["side_effect_state"] == "not_started"


class CancelGuard(ToolExecutionInterceptor):
    async def guard(self, context):
        if context.tool_call_id == "B":
            raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_pipeline_guard_cancel_after_calling_is_not_execution(monkeypatch):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch, cancel=None)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([CancelGuard()])
    with pytest.raises(asyncio.CancelledError):
        _events = [event async for event in agent.execute("synthetic")]
    assert toolkit._calls == ["A"]
    assert json.loads(responses(snapshots[-1])[1].content)["data"]["side_effect_state"] == "not_started"
    assert not agent._tool_execution_ledger.summary()["pending_execution"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after", ["calling", "called"])
async def test_direct_aclose_saves_results_and_never_dispatches_a_pending_call(monkeypatch, stop_after):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch, cancel=None)
    execution = agent.execute("synthetic")
    event = await anext(execution)
    assert event.status == ToolStatus.CALLING
    if stop_after == "called":
        event = await anext(execution)
        assert event.status == ToolStatus.CALLED
    await execution.aclose()
    assert toolkit._calls == ([] if stop_after == "calling" else ["A"])
    result = responses(snapshots[-1])
    assert len(result) == 3
    assert result[0].status == ("error" if stop_after == "calling" else "success")
    assert all(json.loads(item.content)["data"]["side_effect_state"] == "not_started" for item in result[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_after_commit", [False, True])
async def test_result_save_failure_stops_later_dispatch_and_never_duplicates_a(monkeypatch, failure_after_commit):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch, cancel=None)
    save = agent._repository.save_memory.side_effect
    failed = False
    async def uncertain_save(*args):
        nonlocal failed
        if responses(args[2]) and not failed:
            failed = True
            if failure_after_commit:
                await save(*args)
            raise OSError("synthetic storage loss")
        await save(*args)
    agent._repository.save_memory.side_effect = uncertain_save
    with pytest.raises(OSError):
        _events = [event async for event in agent.execute("synthetic")]
    assert toolkit._calls == ["A"]
    assert [item.tool_call_id for item in responses(snapshots[-1])] == ["A", "B", "C"]
    assert responses(snapshots[-1])[0].status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_status", [ToolStatus.CALLING, ToolStatus.CALLED])
async def test_nested_consumer_failure_closes_actual_producer_and_delivers_idempotent_terminals(monkeypatch, event_status):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch, cancel=None)
    producers = []
    async def forward(source):
        producers.append(source)
        async for event in source:
            yield event
    source = forward(forward(agent.execute("synthetic")))
    committed = {}
    failed_event = None
    async def publish(event):
        committed[event.bind_producer_event_id()] = event
    with pytest.raises(OSError):
        async with tool_settlement_scope(publish):
            async for event in source:
                await publish(event)
                if event.status == event_status:
                    failed_event = event
                    raise OSError("delivery lost after commit")
    assert toolkit._calls == ([] if event_status == ToolStatus.CALLING else ["A"])
    assert len(terminal(committed.values())) == 3
    assert len(responses(snapshots[-1])) == 3
    if event_status == ToolStatus.CALLED:
        assert terminal(committed.values())["A"] is failed_event
    await source.aclose()
    for producer in producers:
        await producer.aclose()
    assert toolkit._calls == ([] if event_status == ToolStatus.CALLING else ["A"])


@pytest.mark.asyncio
async def test_repeated_cleanup_storage_failure_keeps_original_cancellation(monkeypatch, caplog):
    agent, toolkit, _, _ = setup_agent(monkeypatch)
    save = agent._repository.save_memory.side_effect
    async def failed_cleanup(*args):
        if len(responses(args[2])) > 1:
            raise OSError("private error payload must not be logged")
        await save(*args)
    agent._repository.save_memory.side_effect = failed_cleanup
    with pytest.raises(asyncio.CancelledError):
        _events = [event async for event in agent.execute("synthetic")]
    assert toolkit._calls == ["A", "B"]
    assert [item.tool_call_id for item in responses(agent.memory)] == ["A", "B", "C"]
    assert "private error payload" not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_batch_preserves_private_mcp_image_refs_without_bytes_or_artifacts(monkeypatch):
    from test_mcp_image_results import environment, result_with
    from app.domain.services.tools.mcp_images import image_tool_message, MCP_IMAGE_REFS_KEY
    context, *_ = environment()
    result, _ = await result_with(context)
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch)
    original = agent.invoke_tool
    async def invoke(resolved, call):
        if call["id"] == "A":
            return image_tool_message(result, tool_call_id="A", name=call["name"])
        return await original(resolved, call)
    agent.invoke_tool = invoke
    events = []
    with pytest.raises(asyncio.CancelledError):
        async for event in agent.execute("synthetic image"):
            events.append(event)
    stored = responses(snapshots[-1])[0]
    assert stored.artifact is None and stored.additional_kwargs[MCP_IMAGE_REFS_KEY]
    assert "base64" not in snapshots[-1].model_dump_json()
    assert "internal-storage" not in snapshots[-1].model_dump_json()
    assert MCP_IMAGE_REFS_KEY not in terminal(events)["A"].model_dump_json()
    assert "base64" not in terminal(events)["A"].model_dump_json()


@pytest.mark.asyncio
async def test_explicit_producer_close_inside_scope_still_publishes_terminal_events(monkeypatch):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch)
    published = []
    async def publish(event):
        published.append(event)
    async with tool_settlement_scope(publish):
        source = agent.execute("synthetic")
        await publish(await anext(source))
        await source.aclose()
    assert set(terminal(published)) == {"A", "B", "C"}
    assert toolkit._calls == [] and len(responses(snapshots[-1])) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["cleanup", "publish"])
async def test_new_cancellation_during_normal_scope_exit_propagates(stage):
    from app.domain.services.tool_settlement import register_tool_settlement
    entered = asyncio.Event()
    async def cleanup():
        if stage == "cleanup":
            entered.set()
            await asyncio.Event().wait()
        return [ToolEvent(tool_call_id="synthetic", tool_name="synthetic", function_name="synthetic",
                          function_args={}, status=ToolStatus.CALLED)]
    async def publish(event):
        entered.set()
        await asyncio.Event().wait()
    async def consume():
        async with tool_settlement_scope(publish):
            register_tool_settlement(cleanup)
    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_real_wait_close_answer_resume_preserves_question_and_never_dispatches_sibling(monkeypatch):
    from contextlib import aclosing
    from app.domain.services.agents.execution import ExecutionAgent
    from app.domain.services.tools.message import MessageToolkit
    first = AIMessage(content="", tool_calls=[
        {"name": "write_probe", "args": {"value": "A"}, "id": "A"},
        {"name": "message_ask_user", "args": {"text": "Which year?"}, "id": "question"},
        {"name": "write_probe", "args": {"value": "C"}, "id": "C"},
    ])
    agent, _, requests, _ = make_agent(monkeypatch, [first, AIMessage(content=json.dumps({
        "success": True, "result": "Verified chosen year 2024", "attachments": []}))], agent_type=ExecutionAgent)
    toolkit = SettlementToolkit()
    agent.toolkits = [toolkit, MessageToolkit()]
    step = Step(id="analysis", description="Analyze chosen year")
    plan = Plan(language="en", steps=[step])
    published = []
    owner_alive = True
    async def require_live():
        from app.domain.services.input_delivery import InputLeaseLost
        if not owner_alive:
            raise InputLeaseLost()
    async def publish(event):
        nonlocal owner_alive
        await require_live()
        published.append(event)
        if hasattr(agent, "_tool_settlement_require_live"):
            agent._tool_settlement_require_live = require_live
        if isinstance(event, WaitEvent):
            owner_alive = False  # Same completion boundary as the runner sink.
    source = agent.execute_step(plan, step, Message(message="Analyze the data"))
    async with aclosing(source), tool_settlement_scope(publish) as settle_tools:
        async for event in source:
            if isinstance(event, WaitEvent):
                await settle_tools()
            await publish(event)
            if isinstance(event, WaitEvent):
                break
    assert toolkit._calls == ["A"]
    assert [item.tool_call_id for item in responses(agent.memory)] == ["A", "question", "C"]
    assert len(requests) == 1
    assert isinstance(published[-1], WaitEvent)
    assert "C" in terminal(published)
    owner_alive = True
    assert await agent.roll_back(Message(message="2024")) is True
    assert [item.tool_call_id for item in responses(agent.memory)] == ["A", "question", "C"]
    agent.reset_context = AsyncMock(side_effect=AssertionError("waiting context cannot be reset"))
    _events = [event async for event in agent.execute_step(plan, step, Message(message="2024"))]
    assert step.status == ExecutionStatus.COMPLETED
    assert "Which year?" in str(requests[-1]) and "2024" in str(requests[-1])
    assert toolkit._calls == ["A"]
    assert len([item for item in responses(agent.memory) if item.tool_call_id == "question"]) == 1
    assert await agent.roll_back(Message(message="2024")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_at", ["result_save", "cancelled_cleanup"])
async def test_lost_owner_cannot_write_completed_result_or_cleanup_snapshot(monkeypatch, lose_at):
    from app.domain.services.input_delivery import InputLeaseLost
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch)
    lost = False
    async def require_live():
        if lost:
            raise InputLeaseLost()
    original = agent.invoke_tool
    async def invoke(resolved, call):
        nonlocal lost
        if lose_at == "cancelled_cleanup" and call["id"] == "B":
            lost = True
            raise InputLeaseLost()
        result = await original(resolved, call)
        if lose_at == "result_save":
            lost = True
        return result
    agent.invoke_tool = invoke
    execution = agent.execute("synthetic")
    await anext(execution)  # initialization precedes the captured owner guard
    agent._tool_settlement_require_live = require_live
    with pytest.raises(InputLeaseLost):
        _events = [event async for event in execution]
    saved_calls = [item.tool_call_id for item in responses(snapshots[-1])]
    assert saved_calls == ([] if lose_at == "result_save" else ["A"])
    assert toolkit._calls == ["A"]
    assert agent.last_execution_outcome["code"] != "completed"


@pytest.mark.asyncio
async def test_transient_owner_check_failure_does_not_skip_unappended_observation(monkeypatch):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch, cancel=None)
    source = agent.execute("synthetic")
    await anext(source)
    attempts = 0
    async def require_live():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient owner store failure")
    agent._tool_settlement_require_live = require_live
    with pytest.raises(OSError):
        _events = [event async for event in source]
    assert toolkit._calls == ["A"]
    assert [item.tool_call_id for item in responses(snapshots[-1])] == ["A", "B", "C"]
    assert responses(snapshots[-1])[0].status == "success"


@pytest.mark.asyncio
async def test_late_old_forwarder_close_cannot_settle_a_new_execution(monkeypatch):
    agent, toolkit, snapshots, _ = setup_agent(monkeypatch, cancel=None)
    old = agent.execute("old request")
    async with tool_settlement_scope(AsyncMock()):
        await anext(old)
    assert toolkit._calls == []
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[{
        "id": "NEW", "name": "write_probe", "args": {"value": "NEW"},
    }]))
    new = agent.execute("new request")
    await anext(new)
    before = len(snapshots)
    await old.aclose()
    assert len(snapshots) == before
    assert agent._active_tool_batch["NEW"].result is None
    event = await anext(new)
    assert event.status == ToolStatus.CALLED and toolkit._calls == ["NEW"]
    await new.aclose()
