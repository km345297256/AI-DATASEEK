"""A rejected native batch gets useful feedback within the existing call budget."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage

from app.domain.services.agents import base as base_module
from app.domain.utils.robust_json_parser import ToolCallParseError
from test_model_retry_policy import agent_with_chain, failure
from test_tool_argument_integrity import invalid, parser
from test_tool_response_protocol_recovery import make_agent, native_call


async def rejected_batch():
    message = invalid('{"value":"private-incomplete-body', valid_calls=[{
        "name": "read_probe", "args": {"value": "unexecuted-sibling"}, "id": "unexecuted-sibling",
    }])
    with pytest.raises(ToolCallParseError) as caught:
        await parser().ainvoke(message)
    return caught.value


def assert_safe_feedback(context, original):
    assert context[:len(original)] == original
    feedback = context[len(original):]
    assert feedback and all(isinstance(item, HumanMessage) for item in feedback)
    assert "one shorter, complete native tool call" in feedback[-1].content
    assert "this batch was not executed" in feedback[-1].content
    assert "Never guess or fill in missing or truncated content" in feedback[-1].content
    assert "smaller write/append calls" in feedback[-1].content
    assert "Do not replay operations that already succeeded" in feedback[-1].content
    assert "private-incomplete-body" not in str(feedback)
    assert "private parser detail" not in str(feedback)


@pytest.mark.asyncio
async def test_first_parse_failure_changes_next_context_without_failed_ai_or_tool_history(monkeypatch):
    answer = AIMessage(content="complete response")
    agent, chain, tool = agent_with_chain(monkeypatch, [await rejected_batch(), answer])
    original = agent.memory.get_messages()
    assert await agent.ask_with_messages([]) is answer
    assert chain.ainvoke.await_count == 2
    assert chain.ainvoke.await_args_list[0].args[0] == original
    assert_safe_feedback(chain.ainvoke.await_args_list[1].args[0], original)
    assert agent.memory.get_messages() == original
    assert agent._add_to_memory.await_args_list[-1].args[0] == [answer]
    assert agent._add_to_memory.await_count == 2
    tool.ainvoke.assert_not_awaited()
    agent._record_token_usage.assert_awaited_once_with(answer)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 1, 2, 3])
async def test_parse_candidates_keep_existing_bound_and_never_persist_failed_batch(monkeypatch, attempts):
    error = await rejected_batch()
    agent, chain, tool = agent_with_chain(monkeypatch, [error] * 4)
    agent.max_retries = attempts
    original = agent.memory.get_messages()
    with pytest.raises(ToolCallParseError) as caught:
        await agent.ask_with_messages([])
    assert caught.value is error
    assert chain.ainvoke.await_count == max(1, attempts)
    for call in chain.ainvoke.await_args_list[1:]:
        assert_safe_feedback(call.args[0], original)
    agent._add_to_memory.assert_awaited_once_with([])
    agent._record_token_usage.assert_not_awaited()
    tool.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_retry_preserves_first_parse_feedback_and_completed_history(monkeypatch):
    answer = AIMessage(content="recovered")
    agent, chain, tool = agent_with_chain(monkeypatch, [await rejected_batch(), failure("0"), answer])
    monkeypatch.setattr(base_module, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    original = agent.memory.get_messages()
    assert await agent.ask_with_messages([]) is answer
    assert chain.ainvoke.await_count == 3
    contexts = [call.args[0] for call in chain.ainvoke.await_args_list]
    assert_safe_feedback(contexts[1], original)
    assert contexts[2] == contexts[1]
    assert sum(isinstance(item, ToolMessage) for item in contexts[2]) == 1
    tool.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_parse_feedback_stops_without_third_candidate(monkeypatch):
    agent, chain, tool = agent_with_chain(monkeypatch, [
        await rejected_batch(), asyncio.CancelledError(), AIMessage(content="must not run"),
    ])
    original = agent.memory.get_messages()
    with pytest.raises(asyncio.CancelledError):
        await agent.ask_with_messages([])
    assert chain.ainvoke.await_count == 2
    assert_safe_feedback(chain.ainvoke.await_args_list[1].args[0], original)
    agent._add_to_memory.assert_awaited_once_with([])
    tool.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_pipeline_dispatches_only_complete_resend_and_keeps_prior_success(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [
        native_call("earlier-success", call_id="earlier"), await rejected_batch(),
        native_call("complete-resend", call_id="resend"), AIMessage(content="Observed results"),
    ])
    agent.max_retries = 3
    _events = [event async for event in agent.execute("Read synthetic data")]
    assert toolkit._calls == ["earlier-success", "complete-resend"]
    assert len(requests) == 4
    assert_safe_feedback(requests[2], requests[1])
    for context in requests[1:]:
        assert sum(isinstance(item, ToolMessage) and item.tool_call_id == "earlier" for item in context) == 1
    assert not any(isinstance(item, AIMessage) and item.invalid_tool_calls for item in agent.memory.messages)
    assert not any(isinstance(item, ToolMessage) and item.tool_call_id == "unexecuted-sibling"
                   for item in agent.memory.messages)
