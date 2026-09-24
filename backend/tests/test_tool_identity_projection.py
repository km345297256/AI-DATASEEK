"""Provider projections must neither mutate evidence nor authorize replay."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage

from app.domain.models.memory import Memory
from app.domain.services.agents.base import BaseAgent
from app.domain.utils.robust_json_parser import ToolCallParseError, validate_tool_call_identity
from test_tool_argument_integrity import parser, invalid
from test_model_retry_policy import agent_with_chain


def call(identity, name="read"):
    return {"id": identity, "name": name, "args": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("promoted", [False, True])
async def test_duplicate_identity_rejects_whole_batch_including_promoted_calls(promoted):
    message = (invalid('{}', valid_calls=[call("incomplete-write")]) if promoted else
               AIMessage(content="", tool_calls=[call("duplicate"), call("duplicate")]))
    with pytest.raises(ToolCallParseError) as error:
        await parser().ainvoke(message)
    assert "this batch was not executed" in str(error.value)
    assert all(isinstance(item, HumanMessage) for item in error.value.make_retry_context([]))


def test_missing_ids_are_assigned_once_without_mutating_provider_object():
    original = AIMessage(content="", tool_calls=[call(None), call(""), call("stable")])
    before = deepcopy(original)
    result = validate_tool_call_identity(original)
    assert original == before
    assert len({item["id"] for item in result.tool_calls}) == 3
    assert all(item["id"] for item in result.tool_calls)
    assert validate_tool_call_identity(result) is result
    result.tool_calls[0]["args"]["probe"] = True
    assert original.tool_calls[0]["args"] == {}


def test_duplicate_historical_results_survive_but_are_not_success_evidence():
    history = [AIMessage(content="", tool_calls=[call("dup"), call("dup")]),
               ToolMessage(content="first observation", tool_call_id="dup"),
               ToolMessage(content="second observation", tool_call_id="dup")]
    before = deepcopy(history)
    projected, repaired = BaseAgent._repair_tool_call_history(object.__new__(BaseAgent), history)
    assert repaired and history == before
    assert len({item["id"] for item in projected[0].tool_calls}) == 2
    assert [result.tool_call_id for result in projected[1:]] == [item["id"] for item in projected[0].tool_calls]
    assert all(result.status == "error" for result in projected[1:])
    assert "first observation" in projected[1].content and "second observation" in projected[2].content
    assert all("Do not repeat" in result.content for result in projected[1:])
    assert BaseAgent._repair_tool_call_history(object.__new__(BaseAgent), history)[0] == projected


def test_missing_result_and_orphan_never_disappear_or_claim_success():
    history = [AIMessage(content="", tool_calls=[call(None)]),
               ToolMessage(content="orphan observation", tool_call_id="unmatched")]
    before = deepcopy(history)
    projected, _ = BaseAgent._repair_tool_call_history(object.__new__(BaseAgent), history)
    assert history == before
    assert projected[1].status == "error"
    assert "TOOL_OUTCOME_UNKNOWN" in projected[1].content
    assert "orphan observation" in projected[2].content
    assert isinstance(projected[2], HumanMessage)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_history", [False, True])
async def test_request_prompts_and_nested_mutations_never_enter_memory(monkeypatch, broken_history):
    answer = AIMessage(content="answer")
    agent, chain, _ = agent_with_chain(monkeypatch, [answer, answer])
    agent.memory = Memory(messages=[SystemMessage(content="stable"), HumanMessage(content="question")])
    if broken_history:
        agent.memory.messages.append(AIMessage(content="", tool_calls=[call("unfinished")]))
    original = deepcopy(agent.memory.messages)
    agent.dynamic_system_prompt_provider = lambda: "request-only-system"
    agent.dynamic_user_context_provider = lambda: '{"request_only":true}'
    agent._persist_memory = AsyncMock()
    await agent.ask_with_messages([])
    context = chain.ainvoke.await_args.args[0]
    assert "request-only-system" in str(context)
    assert "request_only" in str(context)
    context[0].content = "provider-side mutation"
    await agent.ask_with_messages([])
    assert agent.memory.messages == original
    assert chain.ainvoke.await_args.args[0][0].content == "stable"
    agent._persist_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_batch_is_retried_before_any_memory_or_tool_dispatch(monkeypatch):
    rejected = AIMessage(content="", tool_calls=[call("dup"), call("dup")])
    accepted = AIMessage(content="safe answer")
    agent, chain, tool = agent_with_chain(monkeypatch, [rejected, accepted])
    assert await agent.ask_with_messages([]) is accepted
    assert chain.ainvoke.await_count == 2
    assert "identity" in chain.ainvoke.await_args.args[0][-1].content
    assert agent._add_to_memory.await_args.args[0] == [accepted]
    tool.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_identity_recovery_cannot_execute_batch(monkeypatch):
    import asyncio
    rejected = AIMessage(content="", tool_calls=[call("dup"), call("dup")])
    agent, chain, tool = agent_with_chain(monkeypatch, [rejected, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await agent.ask_with_messages([])
    agent._add_to_memory.assert_awaited_once_with([])
    tool.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_agent_loop_never_dispatches_any_member_of_a_duplicate_batch(monkeypatch):
    from test_tool_response_protocol_recovery import make_agent, native_call
    first = native_call("unexecuted-first", call_id="collision")
    second = native_call("unexecuted-second", call_id="collision")
    rejected = AIMessage(content="", tool_calls=[*first.tool_calls, *second.tool_calls])
    agent, toolkit, requests, _ = make_agent(monkeypatch, [rejected, AIMessage(content="No operations ran")])
    agent.max_retries = 3
    _events = [event async for event in agent.execute("Read synthetic data")]
    assert toolkit._calls == []
    assert len(requests) == 2
    assert "this batch was not executed" in requests[1][-1].content
    assert not any(isinstance(message, ToolMessage) for message in agent.memory.messages)


@pytest.mark.parametrize("identity,name", [("", "read"), ("stable", "wrong_tool")])
def test_missing_or_conflicting_historical_identity_is_not_success(identity, name):
    history = [AIMessage(content="", tool_calls=[call(identity)]),
               ToolMessage(content="unconfirmed observation", tool_call_id=identity, name=name)]
    projection, repaired = BaseAgent._repair_tool_call_history(object.__new__(BaseAgent), history)
    assert repaired and projection[1].status == "error"
    assert "TOOL_OUTCOME_UNKNOWN" in projection[1].content
    assert history[1].status == "success"
