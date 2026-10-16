"""Offline completion/answer contracts through the real driver and audit path."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.config import Settings
from app.domain.models.event import MessageEvent, StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services import model_runtime as runtime
from app.domain.services.agents import base as base_module
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.agents.vision import VisionAgent
from app.domain.services.analysis_budget import BudgetUnavailableError
from app.domain.services.analysis_answer_review import _parse_response
from app.domain.utils.model_response import (
    ModelEmptyResponseError, ModelIncompleteResponseError, ModelRefusedResponseError,
    has_complete_answer_text, model_answer_text,
)
from test_no_tool_synthesis_recovery import make_agent, TraceStore


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    settings = Settings(_env_file=None, api_key="offline-response-boundary")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(base_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    usage = AsyncMock()
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", usage)
    return usage


def reply(content="", *, usage=True, **kwargs):
    return AIMessage(content=content, **kwargs, **({"usage_metadata": {
        "input_tokens": 8, "output_tokens": 0, "total_tokens": 8,
    }} if usage else {}))


def latest_requests(store):
    records = {}
    for record in store.history:
        if record.kind == "model_request":
            records[record.trace_id] = record
    return list(records.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", " \n\t", [], [{"type": "text", "text": ""}], [{"type": "output_text", "text": " "}]])
async def test_empty_normal_completion_is_failed_after_actual_usage_settlement(offline, content):
    empty = reply(content, response_metadata={"finish_reason": "stop"})
    agent, provider = make_agent([empty])
    before = empty.model_dump()
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store) as scope:
        with pytest.raises(ModelEmptyResponseError):
            await agent._model.ainvoke([HumanMessage(content="question")])
        assert scope.ledger.charged_tokens == 8 and scope.ledger.calls == 1
    record, = latest_requests(store)
    assert record.status == "failed" and record.error_code == "provider_error"
    assert record.actual_total_tokens == 8 and record.usage_source == "provider"
    assert record.scheduled_retry is None
    assert offline.await_count == 1 and len(provider._requests) == 1
    # Existing private usage tagging is allowed; content/provider metadata are not rewritten.
    assert empty.model_dump(exclude={"additional_kwargs"}) == {k: v for k, v in before.items() if k != "additional_kwargs"}
    assert not any(key.startswith("_dataseek") for key in empty.additional_kwargs)


@pytest.mark.asyncio
async def test_empty_then_success_retries_only_model_preserves_history_and_bills_once(offline):
    empty, final = reply(), reply("Observed count is 7.")
    agent, provider = make_agent([empty, final])
    history = [message.model_dump() for message in agent.memory.messages]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store) as scope:
        response = await agent._ask_without_tools([HumanMessage(content="Summarize the evidence")])
        assert scope.ledger.calls == 2 and scope.ledger.charged_tokens == 16
    first, last = latest_requests(store)
    assert first.status == "failed" and first.error_code == "provider_error"
    assert first.scheduled_retry.next_attempt == 2
    assert last.status == "succeeded" and last.scheduled_retry is None
    assert offline.await_count == 2
    assert response.content == final.content
    assert provider._requests[0]["messages"] == provider._requests[1]["messages"]
    assert [message.model_dump() for message in agent.memory.messages[:len(history)]] == history
    assert sum(message.content == "Summarize the evidence" for message in agent.memory.messages) == 1
    assert not any(message is empty for message in agent.memory.messages)
    assert sum(isinstance(message, ToolMessage) for message in agent.memory.messages) == 1
    agent.invoke_tool.assert_not_awaited()
    agent.get_tools.assert_not_called()


@pytest.mark.asyncio
async def test_empty_exhaustion_is_bounded_and_does_not_append_empty_messages(offline):
    agent, provider = make_agent([reply(), reply(), reply(), reply("must not run")])
    store = TraceStore()
    before = list(agent.memory.messages)
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store):
        with pytest.raises(base_module.LLMServiceUnavailableError):
            await agent._ask_without_tools([])
    records = latest_requests(store)
    assert len(provider._requests) == len(records) == 3
    assert [record.status for record in records] == ["failed"] * 3
    assert [record.scheduled_retry is not None for record in records] == [True, True, False]
    assert agent.memory.messages == before and offline.await_count == 3
    agent.invoke_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_without_usage_keeps_conservative_reservation(offline):
    agent, _ = make_agent([reply(usage=False)])
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store) as scope:
        with pytest.raises(ModelEmptyResponseError):
            await agent._model.ainvoke([HumanMessage(content="question")])
        reserved = scope.ledger.charged_tokens
    record, = latest_requests(store)
    assert reserved == record.input_tokens_after + record.reserved_output_tokens
    assert record.usage_source == "reservation" and record.actual_total_tokens is None
    assert record.status == "failed"
    offline.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [BudgetUnavailableError("private store failure"), asyncio.CancelledError()])
async def test_usage_settlement_failure_or_cancellation_takes_precedence_over_empty_retry(offline, failure):
    agent, provider = make_agent([reply(), reply("must not run")])
    durable = SimpleNamespace(user_id="owner", session_id="synthetic", require_live=None,
        reserve_model_request=AsyncMock(return_value=SimpleNamespace(allowed=True, reservation_id="reservation")),
        settle_model_request=AsyncMock(side_effect=failure))
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store, durable_budget=durable):
        with pytest.raises(asyncio.CancelledError) as caught:
            await agent._ask_without_tools([])
    if isinstance(failure, BudgetUnavailableError):
        assert isinstance(caught.value, runtime.ModelBudgetStopped)
        assert caught.value.code == "trace_store_unavailable"
    assert len(provider._requests) == 1
    durable.settle_model_request.assert_awaited_once_with("reservation", 8)
    assert all(record.scheduled_retry is None for record in store.history)
    offline.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["failure", "retry"])
async def test_failed_completion_or_retry_audit_failure_never_sends_again(offline, fail_at):
    class FailingStore(TraceStore):
        async def put(self, record):
            if record.status == "failed" and (fail_at == "failure" or record.scheduled_retry is not None):
                raise RuntimeError("private audit failure")
            await super().put(record)
    agent, provider = make_agent([reply(), reply("must not run")])
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=FailingStore()):
        with pytest.raises(runtime.ModelBudgetStopped):
            await agent._ask_without_tools([])
    assert len(provider._requests) == 1


@pytest.mark.asyncio
async def test_cancellation_while_waiting_after_empty_never_retries(offline, monkeypatch):
    agent, provider = make_agent([reply(), reply("must not run")])
    waiting = asyncio.Event()
    async def wait(_delay):
        waiting.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(base_module, "asyncio", SimpleNamespace(sleep=wait))
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=TraceStore()):
        task = asyncio.create_task(agent._ask_without_tools([]))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(provider._requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    reply(tool_calls=[{"id": "native", "name": "read", "args": {}}]),
    reply(invalid_tool_calls=[{"id": "malformed", "name": "read", "args": "{"}]),
    reply(additional_kwargs={"reasoning_content": "private reasoning"}),
    reply([{"type": "reasoning", "reasoning": "private", "signature": "private-signature"}]),
    reply([{"type": "image", "data": "private-image"}]),
    reply([{"type": "audio", "data": "private-audio"}]),
    reply([{"type": "extension", "private": "not answer text"}]),
    reply("partial", response_metadata={"finish_reason": "length"}),
])
async def test_calls_thinking_and_multimodal_content_are_not_empty_retried(offline, message):
    agent, provider = make_agent([message])
    before = message.model_dump(exclude={"additional_kwargs", "id"})
    result = await agent._model.ainvoke([HumanMessage(content="question")])
    assert result.model_dump(exclude={"additional_kwargs", "id"}) == before
    assert len(provider._requests) == 1
    assert not has_complete_answer_text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", "max_tokens", "incomplete", "error", "aborted", "future-status"])
async def test_empty_non_normal_finish_fails_without_transient_retry(offline, reason):
    agent, provider = make_agent([reply(response_metadata={"finish_reason": reason}), reply("must not run")])
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=TraceStore()):
        with pytest.raises(ModelIncompleteResponseError):
            await agent._ask_without_tools([])
    assert len(provider._requests) == 1 and offline.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    reply(additional_kwargs={"refusal": "private provider refusal"}),
    reply([{"type": "refusal", "refusal": "private provider refusal"}]),
    reply(response_metadata={"finish_reason": "content_filter"}),
])
async def test_explicit_refusal_is_not_retried_or_misclassified_by_json_parser(offline, message):
    agent, provider = make_agent([message, reply("must not run")])
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store):
        with runtime.model_response_validation(_parse_response):
            with pytest.raises(ModelRefusedResponseError) as caught:
                await agent._ask_without_tools([])
    record, = latest_requests(store)
    assert record.status == "failed" and record.error_code == "provider_error"
    assert record.scheduled_retry is None and offline.await_count == 1
    assert len(provider._requests) == 1 and "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_schema_validator_cannot_hide_empty_response_success(offline):
    agent, _ = make_agent([reply([])])
    def invalid_schema(_message):
        raise ValueError("private schema detail")
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store):
        with runtime.model_response_validation(invalid_schema):
            with pytest.raises(ModelEmptyResponseError):
                await agent._model.ainvoke([HumanMessage(content="question")])
    assert latest_requests(store)[0].status == "failed"
    offline.assert_awaited_once()


@pytest.mark.asyncio
async def test_structured_json_review_uses_answer_projection_inside_real_driver_audit(offline):
    text = json.dumps({"answer_complete": True, "paragraphs": [], "unsupported_claims": []})
    message = reply([{"type": "thinking", "thinking": "private", "signature": "private"},
                     {"type": "text", "text": text[:20]}, {"type": "output_text", "text": text[20:]}])
    agent, provider = make_agent([message])
    before = message.model_dump(exclude={"additional_kwargs", "id"})
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store):
        with runtime.model_response_validation(_parse_response):
            result = await agent._model.ainvoke([HumanMessage(content="question")])
    assert _parse_response(result) == json.loads(text)
    assert result.model_dump(exclude={"additional_kwargs", "id"}) == before
    assert latest_requests(store)[0].status == "succeeded"
    assert len(provider._requests) == 1 and offline.await_count == 1


@pytest.mark.parametrize("content, expected", [
    (None, ""), ("literal", "literal"), (123, ""),
    (["a", {"type": "text", "text": "b"}, {"type": "output_text", "text": "c"}], "abc"),
    ([{"type": "thinking", "thinking": "private", "signature": "private-signature"},
      {"type": "text", "text": "answer"}, {"type": "tool_use", "input": {"secret": "hidden"}},
      {"type": "image", "data": "hidden"}, {"type": "unknown", "text": "not authorized text"}], "answer"),
    ([{"text": "missing type"}, {"type": [], "text": "malformed type"}, {"type": "text", "text": {"private": "hidden"}}], ""),
])
def test_answer_projection_is_narrow_and_does_not_mutate_content(content, expected):
    before = json.dumps(content)
    assert model_answer_text(content) == expected
    assert json.dumps(content) == before


def test_execution_json_split_across_text_blocks_remains_valid_without_exposing_reasoning():
    agent = object.__new__(ExecutionAgent)
    text = json.dumps({"success": True, "result": "Observed 12 rows", "attachments": []})
    blocks = [{"type": "thinking", "thinking": "private reasoning", "signature": "private-signature"},
              {"type": "text", "text": text[:21]}, {"type": "output_text", "text": text[21:]}]
    message = AIMessage(content=blocks)
    before = message.model_dump()
    assert agent._terminal_response_problem(message) is None
    assert agent._message_content_to_text(message.content) == text
    assert message.model_dump() == before


async def visual_step(response):
    agent = object.__new__(VisionAgent)
    agent._build_image_blocks = AsyncMock(return_value=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,offline"}}])
    agent._build_prompt = lambda *_args: "Describe the chart"
    agent.ask_with_messages = AsyncMock(return_value=response)
    step = Step(description="Describe chart", agent="vision")
    events = [event async for event in agent.analyze_step(Plan(goal="Visual answer"), step, Message(message="Describe chart"), None)]
    return step, events


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    reply(), reply(" "), reply([{"type": "thinking", "thinking": "private"}]),
    reply([{"type": "image", "data": "private"}]), reply([{"type": "unknown", "text": "private"}]),
    reply("partial answer", response_metadata={"finish_reason": "length"}),
    reply("blocked", response_metadata={"finish_reason": "content_filter"}),
    reply(additional_kwargs={"refusal": "private refusal"}),
    reply("not a result", tool_calls=[{"id": "native", "name": "read", "args": {}}]),
    reply("not a result", invalid_tool_calls=[{"id": "bad", "name": "read", "args": "{"}]),
])
async def test_vision_does_not_claim_success_without_complete_displayable_answer(response):
    before = response.model_dump()
    step, events = await visual_step(response)
    assert step.status == ExecutionStatus.FAILED and not step.success
    assert [event.status for event in events if isinstance(event, StepEvent)] == [StepStatus.STARTED, StepStatus.FAILED]
    assert not any(isinstance(event, MessageEvent) for event in events)
    assert "private" not in step.error and response.model_dump() == before


@pytest.mark.asyncio
async def test_vision_text_blocks_display_only_answer_and_preserve_raw_response():
    response = reply([{"type": "reasoning", "text": "private", "signature": "hidden"},
                      {"type": "text", "text": "Observed "}, {"type": "output_text", "text": "12 rows."}])
    before = response.model_dump()
    step, events = await visual_step(response)
    assert step.success and step.status == ExecutionStatus.COMPLETED and step.result == "Observed 12 rows."
    assert [event.message for event in events if isinstance(event, MessageEvent)] == [step.result]
    assert response.model_dump() == before
