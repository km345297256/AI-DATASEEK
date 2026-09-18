"""Offline provider fault injection through the real Agent/driver/trace path."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import APIStatusError
from pydantic import PrivateAttr

from app.core.config import Settings
from app.domain.external.model_driver import ModelCapabilities, ModelIdentity
from app.domain.models.memory import Memory
from app.domain.services import model_runtime as runtime
from app.domain.services.agents import base as base_module
from app.infrastructure.external.llm.chat_model import LangChainModelDriver


class FaultInjectingProvider(BaseChatModel):
    _steps: list[Any] = PrivateAttr(default_factory=list)
    _requests: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _entered: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def _llm_type(self):
        return "offline-synthesis-provider"

    async def _agenerate(self, messages, stop=None, **kwargs):
        self._requests.append({"messages": list(messages), **kwargs})
        self._entered.set()
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, float):
            await asyncio.sleep(step)
            step = AIMessage(content="late response")
        return ChatResult(generations=[ChatGeneration(message=step)])

    def _generate(self, *_args, **_kwargs):
        raise AssertionError("Only the governed async provider path is allowed")


class TraceStore:
    def __init__(self):
        self.history = []

    async def put(self, record):
        self.history.append(record.model_copy(deep=True))


@pytest.fixture
def settings(monkeypatch):
    settings = Settings(_env_file=None, api_key="offline-synthesis-test-key")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(base_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", AsyncMock())
    return settings


def make_agent(steps, *, attempts=3):
    provider = FaultInjectingProvider(cache=False)
    provider._steps = list(steps)
    agent = object.__new__(base_module.BaseAgent)
    agent.name = "execution"
    agent.system_prompt = "Synthesize only verified evidence."
    agent._model = LangChainModelDriver(
        client=provider, identity=ModelIdentity(provider="openai", model_name="offline-synthesis"),
        capabilities=ModelCapabilities(tool_calling="adapter"), max_output_tokens=256,
    )
    agent._model_name = "offline-synthesis"
    agent._llm_retry_attempts = attempts
    agent._llm_retry_base_seconds = 0.001
    agent._llm_retry_max_seconds = 0.002
    agent.dynamic_system_prompt_provider = None
    agent.dynamic_user_context_provider = None
    agent.memory = Memory(messages=[
        SystemMessage(content=agent.system_prompt),
        HumanMessage(content="Analyze the synthetic observations"),
        AIMessage(content="", tool_calls=[{"id": "completed-once", "name": "measure", "args": {}}]),
        ToolMessage(content="Verified count: 7", tool_call_id="completed-once", name="measure"),
    ])
    agent._persist_memory = AsyncMock()
    agent._record_token_usage = AsyncMock()
    agent.get_tools = MagicMock(side_effect=AssertionError("Synthesis cannot advertise tools"))
    agent.invoke_tool = AsyncMock(side_effect=AssertionError("Completed operations must not be replayed"))
    return agent, provider


def provider_failure(status):
    return APIStatusError("private transport diagnostic", response=httpx.Response(
        status, request=httpx.Request("POST", "https://offline.invalid/synthesis")),
        body={"error": {"code": "invalid_request" if status == 400 else "unavailable"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", [0.1, provider_failure(503)])
async def test_synthesis_retries_physical_request_without_replaying_completed_tools(settings, first_failure):
    final = AIMessage(content="The verified count is 7.")
    agent, provider = make_agent([first_failure, final])
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store) as scope:
        result = await agent._ask_without_tools(
            [HumanMessage(content="Return the final answer")], request_timeout=0.01,
        )
        assert scope.ledger.calls == 2
        assert scope.ledger.call_limit is None and scope.ledger.token_limit is None
    assert result.content == final.content
    assert len(provider._requests) == 2
    assert provider._requests[0]["messages"] == provider._requests[1]["messages"]
    for request in provider._requests:
        assert not request.get("tools") and not request.get("tool_choice")
        assert sum(isinstance(message, ToolMessage) for message in request["messages"]) == 1
    assert sum(message.content == "Return the final answer" for message in agent.memory.messages) == 1
    amended = next(record for record in store.history if record.scheduled_retry is not None)
    assert amended.status == "failed"
    assert amended.error_code == ("provider_timeout" if isinstance(first_failure, float) else "provider_error")
    assert amended.scheduled_retry.next_attempt == 2
    assert all(record.status != "cancelled" for record in store.history)
    assert store.history[-1].status == "succeeded"
    assert runtime._REQUEST_TIMEOUT.get() is None
    agent.invoke_tool.assert_not_awaited()
    agent.get_tools.assert_not_called()


@pytest.mark.asyncio
async def test_synthesis_transient_failure_exhaustion_remains_honest_and_bounded(settings):
    agent, provider = make_agent([0.1, 0.1, 0.1])
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store):
        with pytest.raises(base_module.LLMServiceUnavailableError):
            await agent._ask_without_tools([], request_timeout=0.01)
    assert len(provider._requests) == 3
    assert len([record for record in store.history if record.scheduled_retry is not None]) == 2
    assert store.history[-1].error_code == "provider_timeout"
    assert runtime._REQUEST_TIMEOUT.get() is None
    agent.invoke_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_synthesis_cancellation_is_not_timeout_and_never_retries(settings):
    agent, provider = make_agent([10.0, AIMessage(content="must not run")])
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="synthetic", task_id="task", store=store):
        task = asyncio.create_task(agent._ask_without_tools([], request_timeout=1.0))
        await provider._entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(provider._requests) == 1
    assert store.history[-1].status == "cancelled"
    assert all(record.scheduled_retry is None for record in store.history)


@pytest.mark.asyncio
async def test_synthesis_permanent_provider_error_does_not_retry(settings):
    agent, provider = make_agent([provider_failure(400), AIMessage(content="must not run")])
    with pytest.raises(APIStatusError):
        await agent._ask_without_tools([], request_timeout=0.1)
    assert len(provider._requests) == 1
    agent.invoke_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", [
    AIMessage(content="answer", tool_calls=[{"id": "forbidden", "name": "write", "args": {}}]),
    AIMessage(content="answer", invalid_tool_calls=[{"id": "forbidden", "name": "write", "args": "{broken"}]),
])
async def test_no_tool_synthesis_never_repairs_or_executes_forbidden_calls(settings, monkeypatch, forbidden):
    agent, provider = make_agent([forbidden])
    monkeypatch.setattr(base_module.RobustJsonParser, "from_llm", MagicMock(
        side_effect=AssertionError("Do not repair tool arguments in a no-tool synthesis")))
    result = await agent._ask_without_tools([], request_timeout=0.1)
    assert not agent._tool_free_completion_is_valid(result)
    assert len(provider._requests) == 1
    agent.invoke_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_watchdog_still_cancels_stuck_nonprovider_work(settings):
    agent, provider = make_agent([])
    cancelled = asyncio.Event()

    async def stuck_history(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    agent._add_to_memory = stuck_history
    agent._no_tool_synthesis_watchdog_seconds = lambda _timeout: 0.01
    with pytest.raises(TimeoutError):
        await agent._ask_without_tools([], request_timeout=0.1)
    assert cancelled.is_set()
    assert provider._requests == []
    assert runtime._REQUEST_TIMEOUT.get() is None


def test_watchdog_covers_all_transport_attempts_backoff_and_trace_io(settings):
    agent, _provider = make_agent([], attempts=4)
    agent._llm_retry_max_seconds = 8.0
    assert agent._no_tool_synthesis_watchdog_seconds(75) == (
        4 * 75 + 3 * 8 + 13 * settings.model_trace_store_timeout_seconds + 5
    )


def test_only_driver_provider_timeout_is_retryable_not_arbitrary_callback_timeouts():
    assert base_module._is_retryable_llm_error(runtime.ModelProviderTimeout())
    assert not base_module._is_retryable_llm_error(TimeoutError("history storage timeout"))
