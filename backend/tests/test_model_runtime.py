import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from app.domain.models.memory import Memory
from app.domain.external.model_driver import ModelIdentity, ModelCapabilities
from app.infrastructure.external.llm.chat_model import LangChainModelDriver
from test_model_driver import FakeClient

from app.core.config import Settings
from app.domain.models.model_trace import ModelTraceRecord
from app.domain.services import model_runtime as runtime
from app.domain.services.agents.base import BaseAgent
from app.domain.services.context_budget import ContextBudgetExceeded
from app.interfaces.api.model_trace_routes import router
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.infrastructure.repositories.mongo_model_trace_repository import get_model_trace_repository


class TraceStore:
    def __init__(self):
        self.records = {}
        self.history = []

    async def put(self, record):
        self.records[record.trace_id] = record.model_copy(deep=True)
        self.history.append(record.model_copy(deep=True))


@pytest.fixture(autouse=True)
def fixed_settings(monkeypatch):
    settings = Settings(_env_file=None, api_key="private-test-identity-key")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", AsyncMock())
    return settings


async def call(handler=None, messages=None, **kwargs):
    if handler is None:
        handler = AsyncMock(return_value=AIMessage(content="done", usage_metadata={
            "input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
        }))
    return await runtime.invoke_model_request(messages=messages or [HumanMessage(content="hello")],
        max_output_tokens=100, provider="deepseek", model_name="fixture", invoke=handler, **kwargs)


@pytest.mark.asyncio
async def test_model_request_reserves_before_send_settles_actual_and_does_not_leak_content():
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1", store=store) as scope:
        async def send(messages, max_output):
            assert scope.ledger.calls == 1
            assert scope.ledger.charged_tokens > 100
            assert store.history[-1].status == "started"
            assert max_output == 100
            return AIMessage(content="private-output", usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30})
        response = await call(send, [HumanMessage(content="PRIVATE /Users/private/secret.json")])
        assert scope.ledger.charged_tokens == 30
        assert response.additional_kwargs[runtime.USAGE_RECORDED_KEY]
    record = next(iter(store.records.values()))
    assert record.status == "succeeded" and record.usage_source == "provider"
    assert record.request_hmac_before == record.request_hmac_after
    public = record.public_view().model_dump_json()
    assert "PRIVATE" not in public and "/Users/" not in public and "private-output" not in public
    assert "user_id" not in public and "session_id" not in public


@pytest.mark.asyncio
async def test_request_phase_timings_preserve_pre_send_audit_and_privacy(monkeypatch):
    ticks = iter(range(100))
    # Replace the module reference, not time.perf_counter globally: unrelated
    # database/event-loop timing must not consume this deterministic clock.
    monkeypatch.setattr(runtime, "time", SimpleNamespace(perf_counter=lambda: next(ticks) * 0.01))
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="timings", store=store):
        async def send(messages, max_output):
            admitted = store.history[-1]
            assert admitted.status == "started"
            assert admitted.timings.context_prepare_ms == pytest.approx(10)
            assert admitted.timings.provider_call_ms is None
            return AIMessage(content="private response")
        await call(send)
    record = next(iter(store.records.values()))
    assert record.status == "succeeded"
    assert all(value == pytest.approx(10) for value in record.timings.model_dump().values())
    assert "private response" not in record.public_view().model_dump_json()
    legacy = record.model_dump(exclude={"timings"})
    assert ModelTraceRecord.model_validate(legacy).timings.provider_call_ms is None


@pytest.mark.asyncio
async def test_failed_provider_has_duration_without_claiming_usage_settled():
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="timing-failed", store=store):
        with pytest.raises(RuntimeError):
            await call(AsyncMock(side_effect=RuntimeError("private provider error")))
    record = next(iter(store.records.values()))
    assert record.timings.provider_call_ms is not None
    assert record.timings.provider_call_ms >= 0
    assert record.timings.usage_settlement_ms is None


@pytest.mark.asyncio
async def test_failed_attempts_keep_reservation_and_exhaustion_never_retries():
    store = TraceStore()
    failure = AsyncMock(side_effect=RuntimeError("PRIVATE provider detail"))
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1", store=store, call_limit=1) as scope:
        with pytest.raises(RuntimeError):
            await call(failure)
        reserved = scope.ledger.charged_tokens
        assert reserved > 100
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await call(failure)
        assert stopped.value.code == "task_call_budget_exceeded"
        assert scope.ledger.charged_tokens == reserved
        assert runtime.model_stop_reason() == "task_call_budget_exceeded"
    assert failure.await_count == 1
    assert {record.status for record in store.records.values()} == {"failed", "budget_exceeded"}
    assert "PRIVATE" not in "".join(record.model_dump_json() for record in store.records.values())


@pytest.mark.asyncio
async def test_missing_usage_retains_reservation_and_tasks_are_isolated():
    async def run(task_id):
        with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id=task_id, call_limit=1) as scope:
            await call(AsyncMock(return_value=AIMessage(content="no usage")))
            assert scope.ledger.calls == 1
            return scope.ledger.charged_tokens
    assert len(set(await asyncio.gather(run("a"), run("b")))) == 1
    assert runtime.model_stop_reason() is None


@pytest.mark.asyncio
async def test_production_task_metering_has_no_cumulative_limit_and_trace_says_unlimited(monkeypatch):
    monkeypatch.setenv("MODEL_TASK_TOKEN_BUDGET", "4096")
    monkeypatch.setenv("MODEL_TASK_CALL_BUDGET", "1")
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="unlimited", store=store) as scope:
        assert scope.ledger.token_limit is None and scope.ledger.call_limit is None
        # Counters far beyond both retired defaults still admit physical calls.
        scope.ledger.calls = 1000
        scope.ledger.charged_tokens = 100_000_000
        await call()
        assert scope.ledger.calls == 1001 and scope.ledger.charged_tokens == 100_000_030
    record = next(iter(store.records.values()))
    assert record.task_token_limit is None and record.task_call_limit is None
    assert record.public_view().model_dump()["task_token_limit"] is None


@pytest.mark.asyncio
async def test_child_calls_share_parent_task_admission_and_closed_scopes_stay_closed():
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1", call_limit=1) as scope:
        await asyncio.create_task(call())
        assert scope.ledger.calls == 1
        with pytest.raises(runtime.ModelBudgetStopped):
            await asyncio.create_task(call())
    assert scope.ledger.closed
    with pytest.raises(runtime.ModelBudgetStopped):
        scope.ledger.reserve(1)


@pytest.mark.asyncio
async def test_context_guard_stops_before_provider_and_has_standalone_fallback(fixed_settings):
    fixed_settings.model_context_capacity_tokens = 4096
    invoke = AsyncMock()
    messages = [SystemMessage(content="rules"), HumanMessage(content="A" * 30000)]
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1"):
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await call(invoke, messages)
        assert stopped.value.code == "context_budget_exceeded"
    with pytest.raises(ContextBudgetExceeded):
        await call(invoke, messages)
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_trace_write_failure_fails_closed_before_provider():
    store = SimpleNamespace(put=AsyncMock(side_effect=RuntimeError("private-database-address")))
    handler = AsyncMock()
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1", store=store):
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await call(handler)
        assert stopped.value.code == "trace_store_unavailable"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_transformation_is_recorded_before_following_request():
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1", store=store):
        before = runtime.memory_checkpoint([ToolMessage(content="private large result", tool_call_id="call-1")])
        runtime.note_memory_change(before, [ToolMessage(content="compacted", tool_call_id="call-1")], "tool_result_limit")
        await call()
    assert store.history[0].kind == "memory_change"
    change = store.history[0].memory_change
    assert change.before_hmac != change.after_hmac
    assert change.bytes_before > change.bytes_after
    assert "private large result" not in store.history[0].model_dump_json()


@pytest.mark.asyncio
async def test_base_agent_does_not_record_driver_usage_twice():
    agent = BaseAgent.__new__(BaseAgent)
    agent.token_usage_service = SimpleNamespace(record_from_message=AsyncMock())
    message = AIMessage(content="done", additional_kwargs={runtime.USAGE_RECORDED_KEY: True})
    await agent._record_token_usage(message)
    agent.token_usage_service.record_from_message.assert_not_awaited()


def test_trace_api_is_session_scoped_read_only_and_does_not_expose_owner():
    record = ModelTraceRecord(user_id="u1", session_id="s1", task_id="task-1", status="succeeded")
    agents = SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(user_id="u1")))
    repository = SimpleNamespace(list_for_owner=AsyncMock(return_value=[record.public_view()]))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: agents
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1")
    app.dependency_overrides[get_model_trace_repository] = lambda: repository
    with TestClient(app) as client:
        response = client.get('/sessions/s1/model-traces?task_id=task-1')
        assert response.status_code == 200
        assert "user_id" not in response.text and "session_id" not in response.text
        repository.list_for_owner.assert_awaited_once_with("u1", "s1", task_id="task-1", limit=100)
        agents.get_session.return_value = None
        assert client.get('/sessions/other/model-traces').status_code == 404
        assert client.post('/sessions/s1/model-traces').status_code == 405


@pytest.mark.asyncio
async def test_malformed_provider_usage_keeps_reservation_and_completes_trace():
    store = TraceStore()
    handler = AsyncMock(return_value=AIMessage(content="done", response_metadata={
        "token_usage": {"prompt_tokens": float("inf"), "completion_tokens": 1},
    }))
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1", store=store) as scope:
        await call(handler)
        assert scope.ledger.charged_tokens > 100
    record = next(iter(store.records.values()))
    assert record.status == "succeeded" and record.usage_source == "reservation"


def test_request_identity_includes_provider_visible_names_and_reasoning_fields():
    first = AIMessage(content="same", name="first")
    second = AIMessage(content="same", name="second")
    assert runtime._request_hmac([first], [], None) != runtime._request_hmac([second], [], None)
    reasoning = first.model_copy(update={"additional_kwargs": {"reasoning_content": "private reason"}})
    assert runtime._request_hmac([first], [], None) != runtime._request_hmac([reasoning], [], None)


@pytest.mark.asyncio
async def test_current_large_image_reaches_driver_intact_while_persisted_memory_is_bounded(monkeypatch):
    client = FakeClient(cache=False)
    driver = LangChainModelDriver(client=client, identity=ModelIdentity(provider="openai", model_name="fixture"),
        capabilities=ModelCapabilities(), max_output_tokens=100)
    monkeypatch.setattr("app.domain.services.agents.base.create_chat_model", lambda *args, **kwargs: driver)
    repository = SimpleNamespace(get_memory=AsyncMock(return_value=Memory()), save_memory=AsyncMock())
    agent = BaseAgent("agent-1", repository)
    agent.bind_tools = False
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200000}}
    message = HumanMessage(content=[{"type": "text", "text": "inspect this image"}, image])
    with runtime.model_execution_scope(user_id="u1", session_id="s1", task_id="task-1", store=TraceStore()):
        await agent.ask_with_messages([message], allow_tools=False)
    delivered = next(item for item in client._requests[0]["messages"] if item.type == "human")
    assert isinstance(delivered.content, list)
    assert delivered.content[1] == image
    assert message.content[1] == image
    for saved in repository.save_memory.await_args_list:
        assert Memory._serialized_size(saved.args[2].messages) <= agent.MAX_MEMORY_BYTES
