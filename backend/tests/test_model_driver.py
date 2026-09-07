"""Offline contract checks for the governed LangChain model boundary."""

import asyncio
import json
import shutil
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from app.core.config import Settings
from app.domain.external.model_driver import ModelCapabilities, ModelIdentity
from app.domain.utils.robust_json_parser import RobustJsonParser
from app.infrastructure.external.llm.chat_model import LangChainModelDriver, create_chat_model


class FakeClient(BaseChatModel):
    _requests: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _responses: list[AIMessage | BaseException] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "fake-provider"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=list(tools), **kwargs)

    async def _agenerate(self, messages, stop=None, **kwargs):
        self._requests.append({"messages": messages, "stop": stop, **kwargs})
        answer = self._responses.pop(0) if self._responses else AIMessage(
            content="ok", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )
        if isinstance(answer, BaseException):
            raise answer
        return ChatResult(generations=[ChatGeneration(message=answer)])

    def _generate(self, messages, stop=None, **kwargs):
        raise AssertionError("Only the governed async provider path should run")


class RecordingMiddleware:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.failures = []
        self.output = 31

    async def __call__(self, **kwargs):
        self.calls.append({key: value for key, value in kwargs.items() if key != "invoke"})
        try:
            response = await kwargs["invoke"](
                [*kwargs["messages"], HumanMessage(content="budget-prepared")], self.output,
            )
        except BaseException as exc:
            self.failures.append(type(exc).__name__)
            raise
        self.responses.append(response)
        return response


def driver(provider="openai"):
    client = FakeClient(cache=False)
    middleware = RecordingMiddleware()
    model = LangChainModelDriver(
        client=client, identity=ModelIdentity(provider=provider, model_name="test-model"),
        capabilities=ModelCapabilities(tool_calling="adapter"), max_output_tokens=400,
        request_middleware=middleware,
    )
    return model, client, middleware


TOOL = {"type": "function", "function": {"name": "measure", "description": "Measure", "parameters": {
    "type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"],
}}}


@pytest.mark.asyncio
async def test_bind_then_bind_tools_preserves_format_output_and_driver_boundary():
    model, client, middleware = driver()
    original = [SystemMessage(content="trusted"), HumanMessage(content="question")]
    result = await model.bind(response_format={"type": "json_object"}, max_tokens=80).bind_tools(
        [TOOL], tool_choice="auto", parallel_tool_calls=False,
    ).ainvoke(original, stop=["END"])
    assert len(middleware.calls) == len(client._requests) == 1
    assert middleware.calls[0]["max_output_tokens"] == 80
    assert middleware.calls[0]["tool_schemas"] == [TOOL]
    assert middleware.calls[0]["response_format"] == {"type": "json_object"}
    assert middleware.calls[0]["driver_version"] == "langchain-driver/v1"
    assert client._requests[0]["max_tokens"] == 31
    assert client._requests[0]["response_format"] == {"type": "json_object"}
    assert client._requests[0]["parallel_tool_calls"] is False
    assert client._requests[0]["tool_choice"] == "auto"
    assert client._requests[0]["stop"] == ["END"]
    assert client._requests[0]["messages"][-1].content == "budget-prepared"
    assert len(original) == 2
    assert result.usage_metadata["total_tokens"] == 10
    assert not ({"provider", "driver_version", "max_output_tokens", "invoke"} & client._requests[0].keys())


@pytest.mark.asyncio
async def test_bind_tools_then_bind_keeps_governance_and_per_request_settings():
    model, client, middleware = driver()
    await model.bind_tools([TOOL]).bind(max_tokens=70, response_format={"type": "json_object"}).ainvoke("one")
    await model.ainvoke("two")
    assert [call["max_output_tokens"] for call in middleware.calls] == [70, 400]
    assert client._requests[0]["tools"] == [TOOL]
    assert "tools" not in client._requests[1]


@pytest.mark.asyncio
async def test_provider_clone_from_strict_binding_stays_inside_driver():
    class CloneClient(FakeClient):
        def bind_tools(self, tools, **kwargs):
            return self.model_copy().bind(tools=list(tools), **kwargs)

    model, _, middleware = driver()
    model._client = CloneClient(cache=False)
    await model.bind_tools([TOOL], strict=True).ainvoke("one")
    assert len(middleware.calls) == 1
    assert model._client._requests[0]["strict"] is True


@pytest.mark.asyncio
async def test_json_parser_repair_is_an_additional_governed_physical_request():
    model, client, middleware = driver()
    client._responses = [AIMessage(content='{"fixed": true}')]
    result = await RobustJsonParser.from_llm(model)._stage3_output_fixing("not valid JSON")
    assert result == {"fixed": True}
    assert len(middleware.calls) == len(client._requests) == 1
    assert middleware.calls[0]["tool_schemas"] == ()


@pytest.mark.asyncio
async def test_failure_and_explicit_retry_each_cross_middleware_once():
    model, client, middleware = driver()
    client._responses = [TimeoutError("provider unavailable"), AIMessage(content="recovered")]
    with pytest.raises(TimeoutError):
        await model.ainvoke("one")
    assert len(client._requests) == 1
    await model.ainvoke("one")
    assert len(middleware.calls) == 2
    assert middleware.failures == ["TimeoutError"]


@pytest.mark.asyncio
async def test_cancellation_does_not_retry():
    model, client, middleware = driver()
    client._responses = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await model.ainvoke("one")
    assert len(client._requests) == 1
    assert middleware.failures == ["CancelledError"]


@pytest.mark.asyncio
async def test_nonretryable_budget_stop_subclass_survives_langchain_and_json_repair():
    class BudgetStopped(asyncio.CancelledError):
        code = "task_token_budget_exceeded"

    stopped = BudgetStopped()

    async def refuse(**_):
        raise stopped

    model, client, _ = driver()
    model._middleware = refuse
    with pytest.raises(BudgetStopped) as direct:
        await model.ainvoke("one")
    assert direct.value is stopped
    with pytest.raises(BudgetStopped) as repair:
        await RobustJsonParser.from_llm(model)._stage3_output_fixing("not JSON")
    assert repair.value is stopped
    assert not client._requests


@pytest.mark.asyncio
async def test_ollama_adapts_native_token_and_json_parameters():
    model, client, middleware = driver("ollama")
    await model.bind(max_tokens=88, response_format={"type": "json_object"}, tool_choice="none").ainvoke("one")
    request = client._requests[0]
    assert request["num_predict"] == 31
    assert request["format"] == "json"
    assert "max_tokens" not in request and "response_format" not in request and "tool_choice" not in request
    assert middleware.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_anthropic_does_not_silently_emulate_unsupported_json_mode():
    model, client, middleware = driver("anthropic")
    with pytest.raises(ValueError, match="does not support response_format"):
        await model.bind(response_format={"type": "json_object"}).ainvoke("one")
    assert not client._requests and not middleware.calls
    await model.bind(response_format=None, tool_choice="none").ainvoke("plain request")
    assert "response_format" not in client._requests[0]
    assert "tool_choice" not in client._requests[0]


@pytest.mark.parametrize("kwargs", [{"max_tokens": 0}, {"max_tokens": True}, {"n": 2}, {"stream": True}])
@pytest.mark.asyncio
async def test_invalid_or_unbudgeted_request_shapes_fail_before_provider(kwargs):
    model, client, _ = driver()
    with pytest.raises(ValueError):
        await model.bind(**kwargs).ainvoke("one")
    assert not client._requests


def test_synchronous_call_still_uses_same_governed_boundary():
    model, client, middleware = driver()
    assert model.invoke("one").content == "ok"
    assert len(client._requests) == len(middleware.calls) == 1


@pytest.mark.parametrize("provider", ["openai", "azure_openai", "xai", "perplexity", "anthropic", "ollama"])
def test_factory_only_sends_provider_appropriate_constructor_parameters(monkeypatch, provider):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return FakeClient(cache=False)

    monkeypatch.setattr("app.infrastructure.external.llm.chat_model.init_chat_model", create)
    model = create_chat_model(Settings(api_key="test-secret", api_base="https://private.example/v1"), {
        "model_provider": provider, "model_name": "test-model", "temperature": 0.2,
        "max_tokens": 300, "client_max_retries": 8,
    })
    assert isinstance(model, LangChainModelDriver)
    assert "extra_body" not in captured
    assert captured["cache"] is False
    if provider == "ollama":
        assert captured["num_predict"] == 300
        assert "max_retries" not in captured and "api_key" not in captured
    else:
        assert captured["max_retries"] == 0
        assert captured["api_key"] == "test-secret"
    serialized = model.model_dump_json() + repr(model) + repr(model._identifying_params)
    assert "test-secret" not in serialized and "private.example" not in serialized
    assert model.identity.model_name == "test-model"


def test_deepseek_keeps_thinking_disabled_without_hidden_sdk_retries(monkeypatch):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return FakeClient(cache=False)

    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", create)
    model = create_chat_model(Settings(api_key="test-secret"), {"model_provider": " DeepSeek "})
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["max_retries"] == 0
    assert model.identity.provider == "deepseek"


def test_public_identity_does_not_expose_endpoint_shaped_model_name(monkeypatch):
    monkeypatch.setattr("app.infrastructure.external.llm.chat_model.init_chat_model", lambda **_: FakeClient())
    model = create_chat_model(Settings(), {"model_name": "https://user:secret@private.example/v1?token=secret"})
    assert model.identity.model_name == "custom-model"
    assert "secret" not in model.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
async def test_real_cordis_catalog_reaches_production_execution_agent_and_runtime(monkeypatch):
    """Run the real 280-tool catalog and DeepSeek schema adapter, no HTTP."""
    from app.domain.models.memory import Memory
    from app.domain.models.dataset import DatasetFile, MountedDataset
    from app.domain.services.agents.execution import ExecutionAgent
    from app.domain.services.flows.plan_act import PlanActFlow
    from app.domain.services import model_runtime
    from app.domain.services.context_budget import ContextBudgetExceeded, estimate_context_tokens, prepare_context
    from app.domain.services.prompts.execution import EXECUTION_PROMPT
    from app.domain.services.tools.mcp import MCPToolkit
    from app.domain.services.tools.plugin import PluginToolkit, default_plugin_directory
    from app.application.services.data_center_dataset_service import render_dataset_context
    from app.infrastructure.external.plugins import NodePluginRuntime
    from app.infrastructure.external.plugins.node_runtime import (
        default_execution_contract_directory, default_plugin_host_path,
    )

    host_path = default_plugin_host_path()
    if not host_path.is_file():
        pytest.skip("Cordis host has not been built")
    runtime = NodePluginRuntime(
        host_path=host_path, tools_dir=default_plugin_directory(),
        execution_contract_dir=default_execution_contract_directory(),
        node_executable=shutil.which("node"), startup_timeout_seconds=15,
        request_timeout_seconds=5, shutdown_timeout_seconds=2,
    )
    observed = {}
    settings = Settings(model_provider="deepseek", model_name="deepseek-chat", api_key="offline-test-key",
                        api_base="https://offline.example.test/v1", max_tokens=4096,
                        model_context_capacity_tokens=65_536, model_context_safety_tokens=2048)
    governed = create_chat_model(settings)
    real_middleware = model_runtime.invoke_model_request

    async def middleware(**kwargs):
        observed["runtime"] = {key: value for key, value in kwargs.items() if key != "invoke"}
        observed["estimate"] = estimate_context_tokens(
            kwargs["messages"], tool_schemas=kwargs["tool_schemas"], response_format=kwargs["response_format"],
        )
        return await real_middleware(**kwargs)

    async def physical_request(_self, messages, stop=None, **kwargs):
        # extra_body fields are merged into the top-level JSON by the SDK.
        observed["physical"] = {"messages": messages, **kwargs, **(kwargs.get("extra_body") or {})}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='{"success":true,"result":"offline","attachments":[]}'))])

    monkeypatch.setattr(model_runtime, "invoke_model_request", middleware)
    monkeypatch.setattr(model_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.agents.base.get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.flows.plan_act.get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.agents.base.create_chat_model", lambda *_, **__: governed)
    monkeypatch.setattr(type(governed._client), "_agenerate_with_cache", physical_request)
    try:
        snapshot = await runtime.start()
        assert snapshot.engine == "cordis"
        assert snapshot.plugin_count == 15 and snapshot.tool_count == 280
        toolkit = PluginToolkit(SimpleNamespace(), session_id="offline-driver", plugin_runtime=runtime)
        repository = SimpleNamespace(get_memory=AsyncMock(return_value=Memory()), save_memory=AsyncMock())
        agent = ExecutionAgent("offline-driver", repository, tools=[toolkit])
        agent.token_usage_service = SimpleNamespace(record_from_message=AsyncMock())
        prompt = EXECUTION_PROMPT.format(
            step="Inspect the NetCDF time axis and summarize missing precipitation data.",
            message="分析当前数据集的降水时间趋势及缺测情况", attachments="[]", language="Chinese",
            dataset_contract=json.dumps({"dataset_id": "offline-dataset", "files": [
                {"path": f"precipitation/year_{year}.nc", "size": 1024 * 1024, "content_type": "application/x-netcdf"}
                for year in range(2011, 2021)
            ]}),
        )
        await agent.ask_with_messages([HumanMessage(content=prompt)], "json_object", max_tokens=4096)
        received = observed["runtime"]
        assert len(received["tool_schemas"]) == 280
        assert all(schema["type"] == "function" and schema["function"]["parameters"]["type"] == "object"
                   for schema in received["tool_schemas"])
        assert received["response_format"] == {"type": "json_object"}
        assert received["max_output_tokens"] == 4096
        assert observed["physical"]["tools"] == received["tool_schemas"]
        assert observed["physical"]["response_format"] == received["response_format"]
        assert observed["physical"]["max_tokens"] == 4096
        total, tools = observed["estimate"]
        assert total <= 65_536 - 4096 - 2048
        print(f"offline-cordis-model-budget tools=280 tool_tokens={tools} input_tokens={total} input_limit=59392 headroom={59392-total}")

        # Include the actual production core toolkits, not just Cordis tools.
        # Only host/service objects are inert fakes: PlanActFlow, all tool
        # schemas, dynamic runtime/subagent/dataset prompts are production code.
        settings.skills_enabled = False
        flow = PlanActFlow(
            agent_id="offline-driver", user_id="offline-driver-user", agent_repository=repository,
            session_id="offline-driver", session_repository=SimpleNamespace(),
            sandbox=SimpleNamespace(), browser=SimpleNamespace(), mcp_tool=MCPToolkit(),
            search_engine=SimpleNamespace(), plugin_runtime=runtime, spill_artifact_store=SimpleNamespace(),
            # This regression intentionally measures the full-catalog rollback
            # mode. New no-profile sessions progressively disclose schemas.
            llm_overrides={"agent_profile": {"tool_runtime": {"selection_mode": "all"}}},
        )
        mounted = MountedDataset(
            dataset_id="offline-dataset", name="Ten years of precipitation", data_center_id="offline-center",
            data_center_name="Offline center", description="NetCDF precipitation fixtures, 2011–2020.",
            temporal_coverage="2011–2020", data_type="NetCDF", sandbox_path="/home/ubuntu/datasets/offline-dataset",
            files=[DatasetFile(path=f"precipitation/year_{year}.nc", size=1024 * 1024,
                               content_type="application/x-netcdf") for year in range(2011, 2021)],
        )
        flow.dataset_context = render_dataset_context([mounted])
        all_tools = governed.bind_tools(flow.executor.get_tools()).kwargs["tools"]
        full_messages = [SystemMessage(content=flow.executor.system_prompt),
                         SystemMessage(content=flow._dynamic_system_prompt()), HumanMessage(content=prompt)]
        full_total, full_tools = estimate_context_tokens(
            full_messages, tool_schemas=all_tools, response_format={"type": "json_object"},
        )
        assert len(all_tools) > snapshot.tool_count
        assert len(all_tools) == sum(len(toolkit.get_tools()) for toolkit in flow.executor.toolkits)
        fixed_fit = True
        try:
            prepare_context(full_messages, tool_schemas=all_tools, response_format={"type": "json_object"},
                            capacity_tokens=65_536, max_output_tokens=4096, safety_tokens=2048)
        except ContextBudgetExceeded:
            fixed_fit = False
        assert fixed_fit == (full_total <= 59_392)
        print(f"offline-production-model-budget tools={len(all_tools)} tool_tokens={full_tools} input_tokens={full_total} input_limit=59392 headroom={59392-full_total} fits={fixed_fit}")
        print("offline-production-toolkit-counts=" + json.dumps({toolkit.name: len(toolkit.get_tools()) for toolkit in flow.executor.toolkits}))
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.asyncio
async def test_real_sdk_json_object_uses_create_with_unmodified_nonstrict_tools(provider):
    """Exercise actual LangChain + OpenAI SDK branches down to an HTTP stub."""
    import httpx
    from openai import AsyncOpenAI

    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "offline-sdk-response", "object": "chat.completion", "created": 1,
            "model": "deepseek-chat" if provider == "deepseek" else "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"ok":true}',
            }}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34},
        })

    middleware = RecordingMiddleware()
    model = create_chat_model(Settings(model_provider=provider, model_name="test-model", api_key="offline-key",
                                       api_base="https://offline.example.test/v1"), request_middleware=middleware)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http_client:
        sdk = AsyncOpenAI(api_key="offline-key", base_url="https://offline.example.test/v1",
                          http_client=http_client, max_retries=0)
        model._client.root_async_client = sdk
        model._client.async_client = sdk.chat.completions
        result = await model.bind(response_format={"type": "json_object"}, tool_choice="none", max_tokens=64).bind_tools(
            [TOOL], extra_body={"test_request_option": "preserved"},
        ).ainvoke("Return a JSON object with ok=true. Do not call tools.")
    assert result.content == '{"ok":true}'
    assert result.usage_metadata["total_tokens"] == 34
    assert len(captured) == len(middleware.calls) == 1
    payload = captured[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["tools"] == [TOOL]
    assert "strict" not in payload["tools"][0]["function"]
    assert payload["tool_choice"] == "none"
    assert payload["test_request_option"] == "preserved"
    assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 31
    assert "extra_body" not in payload
    if provider == "deepseek":
        assert payload["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in payload
