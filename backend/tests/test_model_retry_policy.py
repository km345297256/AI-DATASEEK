import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from openai import APIStatusError

from app.core.config import Settings
from app.domain.models.model_trace import ModelTraceView, ScheduledModelRetry
from app.domain.services import model_runtime as runtime
from app.domain.services.agents import base as base_module
from app.domain.services.model_retry import model_retry_decision


NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def failure(header=None, *, code=None, status=429):
    request = httpx.Request("POST", "https://provider.invalid/private-endpoint")
    response = httpx.Response(status, request=request, headers={"Retry-After": header} if header is not None else {})
    return APIStatusError("private-provider-diagnostic", response=response,
                          body={"error": {"code": code, "message": "private-provider-diagnostic"}})


def decision(header, **kwargs):
    return model_retry_decision(failure(header), attempt=kwargs.pop("attempt", 2),
                               base_seconds=1.0, max_seconds=8.0, now=NOW,
                               random_fraction=kwargs.pop("random_fraction", 0.5), **kwargs)


@pytest.mark.parametrize("header,seconds", [("0", 0), ("1", 1), ("2.5", 2.5), ("8", 8), (" 3 ", 3)])
def test_retry_after_seconds_takes_precedence_over_jitter(header, seconds):
    result = decision(header, random_fraction=0)
    assert result.delay_seconds == seconds
    assert result.reason == "retry_after_seconds"


def test_retry_after_http_date_uses_current_utc_time():
    result = decision(format_datetime(NOW + timedelta(seconds=5), usegmt=True))
    assert result.delay_seconds == 5
    assert result.reason == "retry_after_date"


@pytest.mark.parametrize("header", ["NaN", "Infinity", "-1", "1e9", "garbage", "", "a" * 200])
def test_invalid_retry_after_falls_back_to_jitter(header):
    result = decision(header)
    assert result.delay_seconds == 2
    assert result.reason == "invalid_retry_after_jitter"


@pytest.mark.parametrize("offset", [0, -1, -1000])
def test_expired_retry_after_date_uses_fallback(offset):
    result = decision(format_datetime(NOW + timedelta(seconds=offset), usegmt=True))
    assert result.delay_seconds == 2
    assert result.reason == "expired_retry_after_jitter"


@pytest.mark.parametrize("header", ["8.01", "99999999", "9" * 1000,
    format_datetime(NOW + timedelta(days=1), usegmt=True)])
def test_valid_server_wait_above_bound_stops_instead_of_retrying_early(header):
    result = decision(header)
    assert result.delay_seconds is None
    assert result.reason == "retry_after_exceeds_limit"


@pytest.mark.parametrize("fraction,expected", [(0, 1.6), (0.5, 2), (1, 2.4)])
def test_exponential_retry_has_bounded_twenty_percent_jitter(fraction, expected):
    result = decision(None, random_fraction=fraction)
    assert result.delay_seconds == pytest.approx(expected)
    assert result.reason == "exponential_jitter"
    assert decision(None, attempt=10000, random_fraction=fraction).delay_seconds <= 8


@pytest.mark.parametrize("code", ["insufficient_quota", "quota_exceeded", "billing_hard_limit_reached",
    "billing_not_active", "insufficient_balance", "account_quota_exceeded"])
def test_billing_errors_remain_nonretryable_even_with_retry_after(code):
    assert not base_module._is_retryable_llm_error(failure("1", code=code))


def agent_with_chain(monkeypatch, side_effect):
    agent = object.__new__(base_module.BaseAgent)
    agent.name = "execution"
    agent.max_retries = agent._llm_retry_attempts = 3
    agent.retry_interval = agent._llm_retry_base_seconds = 1
    agent._llm_retry_max_seconds = 8
    agent.bind_tools = True
    agent.tool_choice = None
    agent.dynamic_system_prompt_provider = None
    history = [
        HumanMessage(content="question"),
        AIMessage(content="", tool_calls=[{"id": "already-completed", "name": "measure", "args": {}}]),
        ToolMessage(content="previously computed result", tool_call_id="already-completed", name="measure"),
    ]
    agent.memory = SimpleNamespace(get_messages=lambda: list(history))
    agent._add_to_memory = AsyncMock()
    agent._record_token_usage = AsyncMock()
    tool = SimpleNamespace(ainvoke=AsyncMock())
    agent.get_tools = MagicMock(return_value=[tool])
    model, runnable, chain = MagicMock(), MagicMock(), MagicMock()
    chain.ainvoke = AsyncMock(side_effect=side_effect)
    runnable.bind_tools.return_value = runnable
    runnable.__or__.return_value = chain
    model.bind.return_value = runnable
    agent._model = model
    monkeypatch.setattr(base_module.RobustJsonParser, "from_llm", lambda _model: object())
    return agent, chain, tool


@pytest.mark.asyncio
async def test_agent_retries_model_only_and_keeps_completed_tool_result(monkeypatch):
    answer = AIMessage(content="recovered")
    agent, chain, tool = agent_with_chain(monkeypatch, [failure("2"), answer])
    sleep = AsyncMock()
    monkeypatch.setattr(base_module, "asyncio", SimpleNamespace(sleep=sleep))
    assert await agent.ask_with_messages([]) is answer
    sleep.assert_awaited_once_with(2)
    assert chain.ainvoke.await_count == 2
    for call in chain.ainvoke.await_args_list:
        assert call.args[0][-1].content == "previously computed result"
    tool.ainvoke.assert_not_awaited()
    agent._record_token_usage.assert_awaited_once_with(answer)


@pytest.mark.asyncio
async def test_cancellation_during_wait_stops_without_second_model_call(monkeypatch):
    agent, chain, _tool = agent_with_chain(monkeypatch, [failure("2"), AIMessage(content="must not run")])
    entered = asyncio.Event()

    async def waiting(_delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(base_module, "asyncio", SimpleNamespace(sleep=waiting))
    task = asyncio.create_task(agent.ask_with_messages([]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert chain.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_wait_above_limit_stops_with_friendly_error_and_private_log(monkeypatch, caplog):
    agent, chain, _tool = agent_with_chain(monkeypatch, [failure("999")])
    with pytest.raises(base_module.LLMServiceUnavailableError, match="未提前重复请求") as caught:
        await agent.ask_with_messages([])
    assert chain.ainvoke.await_count == 1
    assert "retry_after_exceeds_limit" in caplog.text
    assert "private-provider" not in caplog.text + str(caught.value)
    assert "private-endpoint" not in caplog.text + str(caught.value)


@pytest.mark.asyncio
async def test_invalid_header_retry_log_contains_only_structured_timing(monkeypatch, caplog):
    agent, _chain, _tool = agent_with_chain(monkeypatch, [failure("private-header-secret"), AIMessage(content="ok")])
    monkeypatch.setattr(base_module, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    await agent.ask_with_messages([])
    assert "reason=invalid_retry_after_jitter" in caplog.text
    assert "failed_attempt=1 next_attempt=2 maximum_attempts=3" in caplog.text
    assert "private-header-secret" not in caplog.text
    assert "private-provider-diagnostic" not in caplog.text


class TraceStore:
    def __init__(self, *, fail_retry=False):
        self.history = []
        self.fail_retry = fail_retry

    async def put(self, record):
        if record.scheduled_retry is not None and self.fail_retry:
            raise RuntimeError("private-storage-error")
        self.history.append(record.model_copy(deep=True))


@pytest.fixture
def runtime_settings(monkeypatch):
    settings = Settings(_env_file=None, api_key="private-test-identity-key")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", AsyncMock())


def schedule():
    return ScheduledModelRetry(failed_attempt=1, next_attempt=2, maximum_attempts=3,
                               delay_seconds=1, reason="retry_after_seconds")


async def invoke(provider, messages=None):
    return await runtime.invoke_model_request(
        messages=messages or [HumanMessage(content="private-user-prompt")],
        max_output_tokens=100, provider="deepseek", model_name="fixture", invoke=provider,
    )


@pytest.mark.asyncio
async def test_retry_trace_is_private_and_commits_before_sleep(monkeypatch, runtime_settings):
    store = TraceStore()
    provider = AsyncMock(side_effect=[failure("1"), AIMessage(content="ok")])

    async def call(context):
        return await invoke(provider, context)

    agent, chain, _tool = agent_with_chain(monkeypatch, call)

    async def sleep(delay):
        assert delay == 1
        assert store.history[-1].status == "failed"
        assert store.history[-1].scheduled_retry.next_attempt == 2

    monkeypatch.setattr(base_module, "asyncio", SimpleNamespace(sleep=sleep))
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store) as scope:
        result = await agent.ask_with_messages([])
        assert scope.ledger.calls == 2
        assert scope.ledger.call_limit is None and scope.ledger.token_limit is None
    assert result.content == "ok" and chain.ainvoke.await_count == 2
    amended = next(record for record in store.history if record.scheduled_retry is not None)
    assert amended.scheduled_retry.failed_attempt == 1
    assert amended.scheduled_retry.maximum_attempts == 3
    assert "scheduled_retry" not in amended.public_view().model_dump()
    assert "scheduled_retry" not in ModelTraceView.model_json_schema()["properties"]
    assert "private-provider" not in amended.model_dump_json()
    assert "private-user-prompt" not in amended.model_dump_json()


@pytest.mark.asyncio
async def test_retry_audit_storage_failure_prevents_next_model_request(monkeypatch, runtime_settings):
    store = TraceStore(fail_retry=True)
    provider = AsyncMock(side_effect=[failure("1"), AIMessage(content="must not run")])

    async def call(context):
        return await invoke(provider, context)

    agent, chain, _tool = agent_with_chain(monkeypatch, call)
    sleep = AsyncMock()
    monkeypatch.setattr(base_module, "asyncio", SimpleNamespace(sleep=sleep))
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        with pytest.raises(runtime.ModelBudgetStopped) as caught:
            await agent.ask_with_messages([])
    assert caught.value.code == "trace_store_unavailable"
    assert chain.ainvoke.await_count == provider.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_exception_trace_cannot_be_amended_from_another_owner(runtime_settings):
    store = TraceStore()
    error = failure("1")
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        with pytest.raises(APIStatusError):
            await invoke(AsyncMock(side_effect=error))
        with runtime.model_execution_scope(user_id="other-owner", session_id="other-session", task_id="other-task", store=store):
            with pytest.raises(runtime.ModelBudgetStopped) as caught:
                await runtime.record_model_retry(error, schedule())
    assert caught.value.code == "runtime_closed"
    assert all(record.scheduled_retry is None for record in store.history)


@pytest.mark.asyncio
async def test_compatibility_error_without_trace_uses_logs_only():
    assert await runtime.record_model_retry(failure("1"), schedule()) is False


@pytest.mark.asyncio
async def test_immutable_provider_exception_is_not_masked_by_trace_binding(runtime_settings):
    @dataclass(frozen=True)
    class ImmutableProviderError(Exception):
        detail: str

    error = ImmutableProviderError("private provider detail")
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        with pytest.raises(ImmutableProviderError) as caught:
            await invoke(AsyncMock(side_effect=error))
        assert await runtime.record_model_retry(error, schedule()) is False
    assert caught.value is error
    assert store.history[-1].status == "failed"


@pytest.mark.asyncio
async def test_cancelled_retry_audit_does_not_issue_another_request(monkeypatch, runtime_settings):
    entered = asyncio.Event()

    class WaitingStore(TraceStore):
        async def put(self, record):
            if record.scheduled_retry is not None:
                entered.set()
                await asyncio.Event().wait()
            await super().put(record)

    provider = AsyncMock(side_effect=[failure("1"), AIMessage(content="must not run")])

    async def call(context):
        return await invoke(provider, context)

    agent, chain, _tool = agent_with_chain(monkeypatch, call)
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=WaitingStore()):
        task = asyncio.create_task(agent.ask_with_messages([]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert chain.ainvoke.await_count == provider.await_count == 1


@pytest.mark.asyncio
async def test_billing_header_does_not_schedule_agent_retry(monkeypatch):
    agent, chain, _tool = agent_with_chain(monkeypatch, [failure("1", code="insufficient_balance")])
    record = AsyncMock()
    monkeypatch.setattr(base_module, "record_model_retry", record)
    with pytest.raises(APIStatusError):
        await agent.ask_with_messages([])
    assert chain.ainvoke.await_count == 1
    record.assert_not_awaited()
