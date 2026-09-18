"""Read-only routing recovers transient transport failure without bypassing safety."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr
from openai import APIStatusError
import pytest

from app.application.services import dataset_request_resolver as resolver_module
from app.core.config import Settings
from app.domain.external.model_driver import ModelCapabilities, ModelIdentity
from app.domain.services import model_runtime as runtime
from app.infrastructure.external.llm.chat_model import LangChainModelDriver
from test_dataset_request_resolver import _resolver


class RoutingClient(BaseChatModel):
    _responses: list = PrivateAttr(default_factory=list)
    _requests: list = PrivateAttr(default_factory=list)
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _cancelled: int = PrivateAttr(default=0)

    @property
    def _llm_type(self):
        return "offline-routing-provider"

    async def _agenerate(self, messages, stop=None, **kwargs):
        self._requests.append({"messages": messages, **kwargs})
        self._started.set()
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if response is None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._cancelled += 1
                raise
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=response))])

    def _generate(self, *args, **kwargs):
        raise AssertionError("No synchronous request")


def decision(*, reject=False):
    data = json.loads(json.dumps(resolver_module.FRONT_CONTROLLER_PROMPT_EXAMPLE))
    if reject:
        data["safety"].update(decision="reject", risk_level="high", reason="Synthetic explicit rejection")
    return json.dumps(data)


@pytest.fixture
def routing(monkeypatch):
    settings = Settings(_env_file=None, api_key="unit-test-routing", llm_retry_attempts=2,
        llm_retry_base_seconds=0, llm_retry_max_seconds=0, dataset_request_resolver_timeout_seconds=0.01)
    monkeypatch.setattr(resolver_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    client = RoutingClient(cache=False)
    driver = LangChainModelDriver(client=client, identity=ModelIdentity(provider="deepseek", model_name="offline-routing"),
        capabilities=ModelCapabilities(tool_calling="adapter", json_object="adapter"), max_output_tokens=1000)
    monkeypatch.setattr(resolver_module, "create_chat_model", lambda *args, **kwargs: driver)
    resolver = _resolver()
    resolver._record_usage = AsyncMock()
    return resolver, client, settings


async def resolve(resolver):
    return await resolver.resolve(question="Compute the uploaded measurements", datasets=[], events=[])


@pytest.mark.asyncio
async def test_one_slow_front_controller_request_can_recover_before_analysis(routing):
    resolver, client, _ = routing
    client._responses = [None, decision()]
    result = await resolve(resolver)
    assert result.mode == "sandbox"
    assert client._cancelled == 1 and len(client._requests) == 2
    assert client._requests[0]["messages"] == client._requests[1]["messages"]
    assert all("tools" not in item and item["response_format"] == {"type": "json_object"}
               for item in client._requests)


@pytest.mark.asyncio
async def test_exhausted_front_controller_timeout_is_a_safe_failure(routing):
    resolver, client, _ = routing
    client._responses = [None, None]
    result = await resolve(resolver)
    assert result.mode == "reject"
    assert result.decision.safety.categories == ["front_controller_unavailable"]
    assert result.controller_metadata["failure_code"] == "transport_timeout"
    assert client._cancelled == 2 and len(client._requests) == 2


@pytest.mark.asyncio
async def test_retry_must_accept_explicit_safety_rejection(routing):
    resolver, client, _ = routing
    client._responses = [None, decision(reject=True)]
    result = await resolve(resolver)
    assert result.mode == "reject" and result.decision.safety.reason == "Synthetic explicit rejection"
    assert len(client._requests) == 2


@pytest.mark.asyncio
async def test_front_controller_cancellation_does_not_retry(routing):
    resolver, client, settings = routing
    settings.dataset_request_resolver_timeout_seconds = 60
    client._responses = [None, decision()]
    task = asyncio.create_task(resolve(resolver))
    await client._started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._cancelled == 1 and len(client._requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,delay,retry", [
    (503, {}, "0", True),
    (429, {"error": {"code": "rate_limit"}}, "0", True),
    (429, {"error": {"code": "insufficient_quota"}}, "0", False),
    (401, {}, "0", False),
    (400, {}, "0", False),
    (429, {}, "999", False),
])
async def test_transport_retry_honors_http_limits_without_repeating_permanent_errors(routing, status, body, delay, retry, caplog):
    resolver, client, _ = routing
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"),
        headers={"retry-after": delay}, json=body)
    failure = APIStatusError("PRIVATE_PROVIDER_BODY", response=response, body=body)
    client._responses = [failure, decision()]
    result = await resolve(resolver)
    assert result.mode == ("sandbox" if retry else "reject")
    assert len(client._requests) == (2 if retry else 1)
    assert "PRIVATE_PROVIDER_BODY" not in caplog.text + json.dumps(result.controller_metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,code", [
    ("not JSON", "invalid_response"),
    ('{"execution":{"mode":"sandbox"}}', "invalid_safety"),
    ('{"safety":{"decision":"allow","risk_level":"invented"}}', "invalid_safety"),
])
async def test_invalid_safety_or_json_is_not_a_transport_retry(routing, response, code):
    resolver, client, _ = routing
    client._responses = [response, decision()]
    result = await resolve(resolver)
    assert result.mode == "reject" and len(client._requests) == 1
    assert result.controller_metadata["failure_code"] == code


@pytest.mark.asyncio
async def test_routing_repair_retries_only_its_transport_and_preserves_locked_safety(routing):
    resolver, client, _ = routing
    initial = json.loads(decision())
    initial["execution"]["mode"] = "invalid-routing-mode"
    client._responses = [json.dumps(initial), None, decision()]
    result = await resolve(resolver)
    assert result.mode == "sandbox" and len(client._requests) == 3
    assert client._requests[1]["messages"] == client._requests[2]["messages"]
    assert result.decision.safety == resolver_module.RequestDecision.model_validate(json.loads(decision())).safety


@pytest.mark.asyncio
async def test_timed_out_front_controller_attempt_is_audited_as_timeout_not_user_stop(routing):
    resolver, client, _ = routing
    client._responses = [None, decision()]
    records = {}

    async def put(record):
        records[record.trace_id] = record.model_copy(deep=True)

    store = type("TraceStore", (), {"put": staticmethod(put)})()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="routing", store=store):
        result = await resolve(resolver)
    first, second = list(records.values())
    assert result.mode == "sandbox"
    assert first.status == "failed" and first.error_code == "provider_timeout"
    assert first.scheduled_retry and first.scheduled_retry.next_attempt == 2
    assert second.status == "succeeded"
    assert first.role == second.role == "front_controller"
    assert first.logical_call_id == second.logical_call_id
