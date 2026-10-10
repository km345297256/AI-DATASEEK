"""No live providers: configuration layers must not reroute inherited secrets."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.application.errors.exceptions import BadRequestError
from app.application.services import model_configuration_service as service
from app.core.config import Settings
from app.domain.models.model_configuration import ModelConfiguration, ModelType
from app.domain.services.model_route import (
    ModelRouteConfigurationError, merge_model_route_overrides, resolve_model_route,
)
from app.infrastructure.external.llm.chat_model import create_chat_model


@pytest.fixture
def settings():
    return Settings(_env_file=None, model_provider="deepseek", model_name="default-model",
                    api_base="https://default.invalid/v1", api_key="default-private-key",
                    extra_headers={"Authorization": "private-header", "X-Private": "also-private"})


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [None, SimpleNamespace(enabled=False),
    SimpleNamespace(enabled=True, model_type=ModelType.EMBEDDING),
    SimpleNamespace(enabled=True, model_type=ModelType.RERANKER)])
async def test_explicit_reference_never_falls_back_after_deletion_disable_or_type_change(monkeypatch, document):
    repository = SimpleNamespace(model_config_id="id", find_one=AsyncMock(return_value=document))
    monkeypatch.setattr(service, "ModelConfigurationDocument", repository)
    with pytest.raises(BadRequestError) as error:
        await service.resolve_model_settings("selected-model", {"model_name": "must-not-use-fallback"})
    assert "selected-model" not in str(error.value)
    assert "must-not-use-fallback" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_type", [ModelType.CHAT, ModelType.VISION])
async def test_available_reference_preserves_explicit_empty_key(monkeypatch, model_type):
    model = ModelConfiguration(id="id", name="Local", model_provider="openai", model_name="local",
                               model_type=model_type, api_base="http://127.0.0.1:1234/v1", api_key="")
    document = SimpleNamespace(enabled=True, model_type=model_type, to_domain=lambda: model)
    monkeypatch.setattr(service, "ModelConfigurationDocument", SimpleNamespace(
        model_config_id="id", find_one=AsyncMock(return_value=document)))
    assert (await service.resolve_model_settings("id", {}))["api_key"] == ""


@pytest.mark.asyncio
async def test_legacy_without_reference_does_not_lookup_or_reject(monkeypatch):
    repository = SimpleNamespace(find_one=AsyncMock(side_effect=AssertionError("Unexpected lookup")))
    monkeypatch.setattr(service, "ModelConfigurationDocument", repository)
    assert await service.resolve_model_settings(None, {"model_name": "legacy", "api_key": ""}) == {
        "model_name": "legacy", "api_key": ""}
    repository.find_one.assert_not_awaited()


@pytest.mark.parametrize("overrides", [{}, {"model_name": "another-model"},
    {"model_provider": " DeepSeek ", "api_base": "https://DEFAULT.invalid:443/v1/"}])
def test_default_runtime_route_retains_compatible_inheritance(settings, overrides):
    route = resolve_model_route(settings, overrides)
    assert route.api_key == "default-private-key"
    assert route.extra_headers == settings.extra_headers
    assert route.extra_headers is not settings.extra_headers
    assert "private" not in repr(route) and "default.invalid" not in repr(route)


@pytest.mark.parametrize("overrides", [
    {"model_provider": "openai"},
    {"api_base": "https://another.invalid/v1"},
    {"api_base": "http://default.invalid/v1"},
    {"api_base": "https://default.invalid:8443/v1"},
    {"api_base": "https://default.invalid/another-tenant"},
])
def test_changed_route_requires_explicit_credential(settings, overrides, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-authorize-custom-route")
    with pytest.raises(ModelRouteConfigurationError) as error:
        resolve_model_route(settings, overrides)
    assert "private" not in str(error.value) and ".invalid" not in str(error.value)


def test_custom_route_uses_own_key_and_no_global_headers(settings):
    route = resolve_model_route(settings, {"model_provider": "openai", "api_key": "custom-key"})
    assert route.api_key == "custom-key" and route.api_base is None and route.extra_headers is None
    route = resolve_model_route(settings, {"api_base": "https://other.invalid/v1", "api_key": "custom-key"})
    assert route.extra_headers is None


@pytest.mark.parametrize("base", ["http://localhost:1234/v1", "http://127.0.0.1:1234/v1", "http://[::1]:1234/v1"])
def test_explicit_local_no_auth_does_not_inherit_key_or_headers(settings, base):
    route = resolve_model_route(settings, {"api_base": base, "api_key": ""})
    assert route.api_key == "" and route.extra_headers is None


@pytest.mark.parametrize("base", ["https://other.invalid/v1", "http://10.0.0.1:1234/v1", "https://localhost.attacker.invalid/v1"])
def test_empty_key_does_not_silently_mean_remote_credential_inheritance(settings, base):
    with pytest.raises(ModelRouteConfigurationError, match="empty model credential"):
        resolve_model_route(settings, {"api_base": base, "api_key": ""})


@pytest.mark.parametrize("base", ["", "file:///private/path", "https://user:secret@host.invalid/v1",
    "https://host.invalid/v1?token=secret", "https://host.invalid/v1#secret", "https://host.invalid:bad/v1",
    "https://default.invalid:0/v1",
    "https://host.invalid\\@other.invalid/v1", "https://host.invalid /v1"])
def test_unsafe_endpoint_fails_without_including_it_in_diagnostics(settings, base):
    with pytest.raises(ModelRouteConfigurationError) as error:
        resolve_model_route(settings, {"api_base": base, "api_key": "custom"})
    assert "secret" not in str(error.value) and "host.invalid" not in str(error.value)


def test_native_ollama_does_not_inherit_remote_auth_or_endpoint(settings):
    route = resolve_model_route(settings, {"model_provider": "ollama"})
    assert route.api_key is None and route.api_base is None and route.extra_headers is None


@pytest.mark.parametrize("updates", [
    {"model_provider": "openai"}, {"api_base": "https://child.invalid/v1"},
])
def test_child_config_cannot_disguise_parent_credentials_as_explicit(settings, updates):
    base = {"model_provider": "deepseek", "api_base": "https://parent.invalid/v1", "api_key": "parent-key"}
    merged = merge_model_route_overrides(settings, base, updates)
    assert "api_key" not in merged
    with pytest.raises(ModelRouteConfigurationError):
        resolve_model_route(settings, merged)


def test_child_same_endpoint_keeps_own_parent_key_but_other_endpoint_must_supply_own(settings):
    base = {"model_provider": "deepseek", "api_base": "https://parent.invalid/v1", "api_key": "parent-key"}
    assert merge_model_route_overrides(settings, base, {"model_name": "another"})["api_key"] == "parent-key"
    merged = merge_model_route_overrides(settings, base, {"api_base": "https://child.invalid/v1", "api_key": "child-key"})
    assert resolve_model_route(settings, merged).api_key == "child-key"
    assert base["api_key"] == "parent-key"


def test_planner_and_subagent_layers_use_route_aware_merge(settings, monkeypatch):
    from app.domain.services.flows.plan_act import PlanActFlow
    monkeypatch.setattr("app.domain.services.flows.plan_act.get_settings", lambda: settings)
    flow = object.__new__(PlanActFlow)
    base = {"model_provider": "deepseek", "api_base": "https://parent.invalid/v1", "api_key": "parent-key"}
    flow.agent_profile_config = {"planner_config": {"api_base": "https://planner.invalid/v1"}}
    flow.enabled_subagents = {"execution": SimpleNamespace(model_settings={"model_provider": "openai"}, system_prompt=None)}
    assert "api_key" not in flow._agent_llm_overrides(base, "planner")
    execution = flow._agent_llm_overrides(base, "execution")
    assert "api_key" not in execution and "api_base" not in execution


def test_vision_endpoint_override_does_not_reuse_parent_credentials(settings, monkeypatch):
    from app.domain.services.agents.vision import VisionAgent
    settings.vision_model_base = "https://vision.invalid/v1"
    monkeypatch.setattr("app.domain.services.agents.vision.get_settings", lambda: settings)
    projected = VisionAgent._build_vision_overrides({"api_base": "https://parent.invalid/v1", "api_key": "parent-key"})
    assert "api_key" not in projected
    with pytest.raises(ModelRouteConfigurationError):
        resolve_model_route(settings, projected)
    settings.vision_model_api_key = "vision-key"
    assert VisionAgent._build_vision_overrides({"api_key": "parent-key"})["api_key"] == "vision-key"


@pytest.mark.asyncio
async def test_invalid_profile_model_stops_before_session_creation(monkeypatch):
    from app.domain.models.agent_profile import AgentProfile
    from app.interfaces.api.session_routes import create_session
    from app.interfaces.schemas.session import CreateSessionRequest
    monkeypatch.setattr(service, "ModelConfigurationDocument", SimpleNamespace(
        model_config_id="id", find_one=AsyncMock(return_value=None)))
    profile = AgentProfile(id="profile", name="Selected", model_config_id="deleted", user_id="owner")
    profiles = SimpleNamespace(get_profile=AsyncMock(return_value=profile))
    agents = SimpleNamespace(create_session=AsyncMock())
    with pytest.raises(BadRequestError, match="unavailable"):
        await create_session(CreateSessionRequest(agent_profile_id="profile"), SimpleNamespace(id="owner"), agents, profiles)
    agents.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sdk_mock_wire_no_auth_cannot_pick_up_ambient_key(settings, monkeypatch):
    from langchain.messages import HumanMessage

    captured = []
    async def transport(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 0,
            "model": "local", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "ok"}}]})
    async def middleware(**request):
        return await request["invoke"](request["messages"], request["max_output_tokens"])

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-private-key")
    model = create_chat_model(settings, {"model_provider": "openai", "model_name": "local",
        "api_base": "http://127.0.0.1:1234/v1", "api_key": ""}, request_middleware=middleware)
    # Keep the factory's exact auth headers; only replace its network transport.
    model._client.root_async_client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    await model.ainvoke([HumanMessage(content="synthetic")])
    assert len(captured) == 1
    headers = str(captured[0].headers)
    assert "default-private-key" not in headers and "ambient-private-key" not in headers
    assert "private-header" not in headers and "also-private" not in headers
    model._client.root_client.close()
    await model._client.root_async_client.close()
