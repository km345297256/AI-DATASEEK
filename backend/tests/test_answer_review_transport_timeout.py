"""Real driver/middleware timeout semantics with an offline provider double."""
import asyncio
from unittest.mock import AsyncMock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr, ValidationError
import pytest

from app.core.config import Settings
from app.domain.external.model_driver import ModelCapabilities, ModelIdentity
from app.domain.services import model_runtime as runtime
from app.domain.services.agents import execution as execution_module
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.analysis_answer_review import AnswerEvidence
from app.infrastructure.external.llm.chat_model import LangChainModelDriver


class ReviewClient(BaseChatModel):
    _requests: list = PrivateAttr(default_factory=list)
    _delays: list[float | None] = PrivateAttr(default_factory=list)
    _cancelled: int = PrivateAttr(default=0)
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def _llm_type(self):
        return "offline-review-provider"

    async def _agenerate(self, messages, stop=None, **kwargs):
        self._requests.append({"messages": messages, **kwargs})
        self._started.set()
        delay = self._delays.pop(0) if self._delays else 0
        try:
            if delay is None:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._cancelled += 1
            raise
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='{"checked":true}'))])

    def _generate(self, *args, **kwargs):
        raise AssertionError("No synchronous model request")


class TraceStore:
    def __init__(self):
        self.records = {}

    async def put(self, record):
        self.records[record.trace_id] = record.model_copy(deep=True)


@pytest.fixture
def scenario(monkeypatch):
    settings = Settings(_env_file=None, api_key="unit-test-review-identity", llm_retry_attempts=2,
                        llm_retry_base_seconds=0, llm_retry_max_seconds=0)
    # A sub-second fixture deadline keeps cancellation tests fast; deployed
    # configuration is separately validated at the supported 1..300 seconds.
    settings.answer_review_request_timeout_seconds = 0.02
    monkeypatch.setattr(execution_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    captured = {}

    async def review(*, ask, timeout_seconds, **kwargs):
        captured["timeout_seconds"] = timeout_seconds
        async with asyncio.timeout(timeout_seconds):
            return await ask([SystemMessage(content="Review evidence and return JSON."),
                              HumanMessage(content="Private fixture, never stored in model trace.")])

    monkeypatch.setattr("app.domain.services.analysis_answer_review.review_answer", review)
    client = ReviewClient(cache=False)
    agent = object.__new__(ExecutionAgent)
    agent._model = LangChainModelDriver(client=client,
        identity=ModelIdentity(provider="deepseek", model_name="offline-review"),
        capabilities=ModelCapabilities(tool_calling="adapter", json_object="adapter"), max_output_tokens=4096)
    agent.execute = AsyncMock(side_effect=AssertionError("Review retry cannot run analysis tools"))
    agent._parse_json = AsyncMock(side_effect=AssertionError("Review transport cannot repair JSON"))
    return agent, client, settings, captured


async def review(agent):
    return await agent.review_delivery_answer(question="Check", draft="Draft", files=[],
        evidence=AnswerEvidence(), requirements=[], language="zh")


@pytest.mark.asyncio
async def test_provider_timeout_is_auditable_and_retry_can_succeed_without_tools(scenario):
    agent, client, settings, captured = scenario
    client._delays = [None, 0]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="review", store=store) as scope:
        response = await review(agent)
        assert scope.ledger.call_limit is None and scope.ledger.token_limit is None
        assert scope.ledger.calls == 2
    first, second = list(store.records.values())
    assert response.content == '{"checked":true}'
    assert first.status == "failed" and first.error_code == "provider_timeout"
    assert first.scheduled_retry.failed_attempt == 1 and first.scheduled_retry.next_attempt == 2
    assert second.status == "succeeded" and second.scheduled_retry is None
    assert first.logical_call_id == second.logical_call_id
    assert {first.role, second.role} == {"answer_review"}
    assert client._cancelled == 1 and len(client._requests) == 2
    assert captured["timeout_seconds"] > 2 * settings.answer_review_request_timeout_seconds
    for request in client._requests:
        assert request["response_format"] == {"type": "json_object"}
        assert request["max_tokens"] == 4096 and "tools" not in request
        assert "timeout_seconds" not in request and "request_timeout" not in request
    assert "Private fixture" not in "".join(record.model_dump_json() for record in store.records.values())
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


@pytest.mark.asyncio
async def test_all_slow_requests_exhaust_transport_attempts_not_the_analysis_scope(scenario):
    agent, client, _, _ = scenario
    client._delays = [None, None]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="slow", store=store) as scope:
        with pytest.raises(runtime.ModelProviderTimeout):
            await review(agent)
        assert scope.ledger.stopped_code is None
    first, second = list(store.records.values())
    assert [first.error_code, second.error_code] == ["provider_timeout", "provider_timeout"]
    assert first.scheduled_retry and second.scheduled_retry is None
    assert client._cancelled == 2


@pytest.mark.asyncio
async def test_user_cancellation_is_not_timeout_and_never_retries(scenario):
    agent, client, settings, _ = scenario
    settings.answer_review_request_timeout_seconds = 10
    client._delays = [None]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="cancel", store=store):
        task = asyncio.create_task(review(agent))
        await client._started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    record = next(iter(store.records.values()))
    assert record.status == record.error_code == "cancelled"
    assert record.scheduled_retry is None and len(client._requests) == 1


@pytest.mark.asyncio
async def test_standalone_review_is_timed_out_and_context_does_not_leak_to_other_requests(scenario):
    agent, client, _, _ = scenario
    client._delays = [None, 0]
    assert (await review(agent)).content == '{"checked":true}'
    assert len(client._requests) == 2
    # The same real driver outside answer review retains its established
    # network behavior, even when slower than the review-specific fixture cap.
    client._delays = [0.04]
    assert (await agent._model.ainvoke([HumanMessage(content="Other work")])).content == '{"checked":true}'
    assert client._cancelled == 1


@pytest.mark.asyncio
async def test_review_outer_wait_covers_attempts_backoff_and_trace_io(scenario):
    agent, _, settings, captured = scenario
    settings.answer_review_request_timeout_seconds = 60
    settings.llm_retry_attempts = 4
    settings.llm_retry_max_seconds = 8
    await review(agent)
    assert captured["timeout_seconds"] >= 4 * 60 + 3 * 8 + 3 * settings.model_trace_store_timeout_seconds


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan"), True])
def test_private_request_timeout_rejects_invalid_values(seconds):
    with pytest.raises(ValueError):
        with runtime.model_request_timeout(seconds):
            raise AssertionError("Invalid timeout was accepted")


@pytest.mark.parametrize("seconds", [0, 301, float("inf"), float("nan")])
def test_review_configuration_rejects_unsupported_timeout(seconds):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, answer_review_request_timeout_seconds=seconds)
