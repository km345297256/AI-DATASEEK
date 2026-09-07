import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.application.services.agent_profile_service import AgentProfileService
from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot,
    PluginDescriptor,
    PluginToolDefinition,
    ToolExecutionDescriptor,
    ToolPresentationDescriptor,
)
from app.domain.models.agent_profile import AgentProfile, AgentSubAgentConfig, AgentPlannerConfig
from app.domain.models.tool_runtime import ToolRuntimeConfig
from app.domain.models.user import User, UserRole
from app.domain.services.domain_presets import get_domain_preset, list_domain_presets
from app.domain.services import tool_runtime_config
from app.domain.services.audit_service import get_audit_service
from app.domain.services.permission_service import get_permission_service
from app.infrastructure.external.llm import chat_model
from app.interfaces.api import agent_profile_routes, domain_preset_routes
from app.interfaces.dependencies import get_agent_profile_service, get_current_user, get_plugin_runtime
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.schemas.agent_profile import UpdateAgentProfileRequest


class MemoryProfileRepository:
    """Exercise a serialization round trip without opening a database."""

    def __init__(self) -> None:
        self.profiles: dict[str, AgentProfile] = {}

    @staticmethod
    def _round_trip(profile: AgentProfile) -> AgentProfile:
        return AgentProfile.model_validate(profile.model_dump(mode="python"))

    async def create(self, profile: AgentProfile) -> AgentProfile:
        stored = self._round_trip(profile)
        self.profiles[stored.id] = stored
        return stored.model_copy(deep=True)

    async def get_by_id(self, profile_id: str) -> AgentProfile | None:
        profile = self.profiles.get(profile_id)
        return profile.model_copy(deep=True) if profile else None

    async def list_for_user(self, _user_id: str) -> list[AgentProfile]:
        return [profile.model_copy(deep=True) for profile in self.profiles.values()]

    async def update(self, profile: AgentProfile) -> AgentProfile:
        stored = self._round_trip(profile)
        self.profiles[stored.id] = stored
        return stored.model_copy(deep=True)

    async def delete(self, profile_id: str) -> None:
        self.profiles.pop(profile_id, None)


def _user(role: UserRole) -> User:
    return User(
        id=f"{role.value}-1",
        fullname=f"{role.value.title()} User",
        email=f"{role.value}@example.test",
        role=role,
    )


@pytest.mark.parametrize("preset_id", [preset.id for preset in list_domain_presets()])
@pytest.mark.parametrize("selection_mode", ["all", "on_demand"])
def test_tool_runtime_accepts_known_presets_and_modes(preset_id: str, selection_mode: str) -> None:
    runtime = ToolRuntimeConfig(
        preset_id=preset_id,
        selection_mode=selection_mode,
        code_mode_enabled=True,
        domain_subagents_enabled=True,
    )

    assert runtime.preset_id == preset_id
    assert runtime.selection_mode == selection_mode


@pytest.mark.parametrize(
    "payload",
    [
        {"preset_id": "not_a_preset"},
        {"preset_id": "../geoscience"},
        {"selection_mode": "everything"},
        {"selection_mode": True},
    ],
)
def test_tool_runtime_rejects_unknown_preset_and_mode(payload: dict) -> None:
    with pytest.raises((ValidationError, ValueError)):
        ToolRuntimeConfig.model_validate(payload)


def test_legacy_profile_stays_all_but_no_profile_uses_deployment_default(monkeypatch) -> None:
    legacy = AgentProfile.model_validate({"id": "legacy", "name": "Legacy profile"})
    assert legacy.tool_runtime == ToolRuntimeConfig(
        preset_id="general",
        selection_mode="all",
        code_mode_enabled=False,
        domain_subagents_enabled=False,
    )

    monkeypatch.setattr(
        tool_runtime_config,
        "get_settings",
        lambda: SimpleNamespace(
            tool_preset_id="general",
            tool_selection_mode="on_demand",
            code_mode_enabled=False,
            domain_subagents_enabled=False,
        ),
    )
    assert tool_runtime_config.resolve_tool_runtime(None) == ToolRuntimeConfig(
        preset_id="general",
        selection_mode="on_demand",
        code_mode_enabled=False,
        domain_subagents_enabled=False,
    )


@pytest.mark.asyncio
async def test_profile_tool_runtime_create_and_update_round_trip_preserves_models_and_subagents() -> None:
    repository = MemoryProfileRepository()
    service = AgentProfileService(repository)
    subagents = [
        AgentSubAgentConfig(
            key="domain_tabular",
            name="Tabular Agent",
            handler_type="execution",
            preset_id="tabular",
            model_config={"provider": "private-provider", "api_key": "subagent-secret"},
        ),
        AgentSubAgentConfig(
            key="vision",
            name="Vision Agent",
            handler_type="vision",
            model_config={"model_name": "vision-model"},
        ),
    ]
    created = await service.create_profile(
        user_id="admin-1",
        user_role="admin",
        workspace_id="personal-admin-1",
        name="Private model profile",
        model_name="model-before-update",
        model_provider="private-provider",
        api_base="https://private-provider.example/v1",
        api_key="profile-secret",
        temperature=0.25,
        max_tokens=8192,
        system_prompt="Keep this prompt",
        planner_config=AgentPlannerConfig(api_key="planner-secret", model_name="planner-model"),
        subagents=subagents,
        tool_runtime=ToolRuntimeConfig(preset_id="tabular", selection_mode="all"),
    )
    assert isinstance(created.tool_runtime, ToolRuntimeConfig)
    assert all(isinstance(subagent, AgentSubAgentConfig) for subagent in created.subagents)

    request = UpdateAgentProfileRequest.model_validate({
        "tool_runtime": {
            "preset_id": "geoscience",
            "selection_mode": "on_demand",
            "code_mode_enabled": True,
            "domain_subagents_enabled": True,
        },
    })
    updated = await service.update_profile(
        user_id="admin-1",
        user_role="admin",
        workspace_id="personal-admin-1",
        profile_id=created.id,
        **request.model_dump(exclude_none=True),
    )

    assert updated.tool_runtime == ToolRuntimeConfig(
        preset_id="geoscience",
        selection_mode="on_demand",
        code_mode_enabled=True,
        domain_subagents_enabled=True,
    )
    assert isinstance(updated.tool_runtime, ToolRuntimeConfig)
    assert updated.model_name == "model-before-update"
    assert updated.model_provider == "private-provider"
    assert updated.api_base == "https://private-provider.example/v1"
    assert updated.api_key == "profile-secret"
    assert updated.temperature == 0.25
    assert updated.max_tokens == 8192
    assert updated.system_prompt == "Keep this prompt"
    assert updated.planner_config.api_key == "planner-secret"
    assert [subagent.key for subagent in updated.subagents] == ["domain_tabular", "vision"]
    assert all(isinstance(subagent, AgentSubAgentConfig) for subagent in updated.subagents)
    assert updated.subagents[0].model_settings["api_key"] == "subagent-secret"
    assert updated.subagents[1].model_settings["model_name"] == "vision-model"


def test_subagent_domain_preset_is_only_valid_for_execution_handlers() -> None:
    execution = AgentSubAgentConfig(
        key="domain_space",
        name="Space Agent",
        handler_type="execution",
        preset_id="space",
    )
    assert execution.preset_id == "space"

    with pytest.raises(ValidationError, match="domain presets require an execution handler"):
        AgentSubAgentConfig(
            key="vision",
            name="Vision Agent",
            handler_type="vision",
            preset_id="image_science",
        )
    with pytest.raises((ValidationError, ValueError), match="Unknown domain preset"):
        AgentSubAgentConfig(
            key="domain_unknown",
            name="Unknown Agent",
            handler_type="execution",
            preset_id="unknown",
        )


@pytest.mark.parametrize("key", ["execution", "vision"])
def test_domain_presets_cannot_shadow_reserved_core_subagent_keys(key: str) -> None:
    payload = {"key": key, "name": "Core identity", "handler_type": "execution", "preset_id": "tabular"}
    with pytest.raises(ValidationError, match="reserved core subagent keys"):
        AgentSubAgentConfig.model_validate(payload)
    with pytest.raises(ValidationError, match="reserved core subagent keys"):
        UpdateAgentProfileRequest.model_validate({"subagents": [payload]})
    # Existing profiles without a preset retain exactly their old core keys.
    legacy = AgentSubAgentConfig.model_validate({**payload, "preset_id": None})
    assert legacy.key == key and legacy.preset_id is None


def test_runtime_preset_profile_api_inherits_safe_settings_and_requires_admin(monkeypatch) -> None:
    repository = MemoryProfileRepository()
    service = AgentProfileService(repository)
    permission = SimpleNamespace(default_workspace_id=AsyncMock(return_value="personal-admin-1"))
    audit = SimpleNamespace(record=AsyncMock())
    settings = SimpleNamespace(
        model_name="deployment-model",
        model_provider="deployment-provider",
        temperature=0.15,
        max_tokens=12288,
        api_base="https://private-deployment.example/v1",
        api_key="deployment-secret",
    )
    monkeypatch.setattr(agent_profile_routes, "get_settings", lambda: settings)
    # Guard against the route accidentally constructing a model client.
    monkeypatch.setattr(chat_model, "create_chat_model", lambda *_args, **_kwargs: pytest.fail("model client must not be created"))

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(agent_profile_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.ADMIN)
    app.dependency_overrides[get_agent_profile_service] = lambda: service
    app.dependency_overrides[get_permission_service] = lambda: permission
    app.dependency_overrides[get_audit_service] = lambda: audit
    payload = {
        "name": "Explicit runtime profile",
        "tool_runtime": {
            "preset_id": "space",
            "selection_mode": "on_demand",
            "code_mode_enabled": True,
            "domain_subagents_enabled": False,
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/agent-profiles/runtime-preset", json=payload)
        app.dependency_overrides[get_current_user] = lambda: _user(UserRole.USER)
        denied = client.post("/api/v1/agent-profiles/runtime-preset", json=payload)

    assert response.status_code == 200
    data = response.json()["data"]
    stored = next(iter(repository.profiles.values()))
    for profile in (stored, SimpleNamespace(**data)):
        assert profile.model_name == "deployment-model"
        assert profile.model_provider == "deployment-provider"
        assert profile.temperature == 0.15
        assert profile.max_tokens == 12288
        assert profile.api_base is None
        assert profile.api_key is None
    serialized = response.text
    assert "deployment-secret" not in serialized
    assert "private-deployment" not in serialized
    assert stored.tool_runtime == ToolRuntimeConfig.model_validate(payload["tool_runtime"])
    assert denied.status_code == 401
    assert len(repository.profiles) == 1
    permission.default_workspace_id.assert_awaited_once()
    audit.record.assert_awaited_once()


def _catalog_snapshot() -> PluginCatalogSnapshot:
    plugin_tools = (
        ("data_foundation", "data_format_inspect"),
        ("scientific", "scientific_inspect"),
        ("tabular", "table_profile"),
    )
    tools = tuple(
        PluginToolDefinition(
            contract_version=2,
            name=name,
            description=f"Inspect /Users/private/{name}.dat with api_key=do-not-return",
            parameters={"type": "object", "properties": {}},
            output_schema=None,
            execution=ToolExecutionDescriptor(),
            presentation=ToolPresentationDescriptor(),
            plugin=plugin,
            version="1.0.0",
        )
        for plugin, name in plugin_tools
    )
    plugins = tuple(
        PluginDescriptor(
            plugin=plugin,
            version="1.0.0",
            manifest_digest=(str(index) * 64),
            tool_count=1,
        )
        for index, plugin in enumerate(("data_foundation", "scientific", "tabular"), start=1)
    )
    return PluginCatalogSnapshot(
        engine="cordis",
        version="4.0.2",
        revision="catalog-revision-safe",
        manifest_digest="a" * 64,
        execution_bundle_digest="b" * 64,
        plugin_count=len(plugins),
        tool_count=len(tools),
        plugins=plugins,
        tools=tools,
    )


def test_domain_preset_api_returns_nine_snapshot_scoped_public_counts(monkeypatch) -> None:
    snapshot = _catalog_snapshot()
    runtime = SimpleNamespace(current_snapshot=snapshot)
    monkeypatch.setattr(
        tool_runtime_config,
        "get_settings",
        lambda: SimpleNamespace(
            tool_preset_id="general",
            tool_selection_mode="on_demand",
            code_mode_enabled=False,
            domain_subagents_enabled=False,
        ),
    )
    app = FastAPI()
    app.include_router(domain_preset_routes.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.ADMIN)
    app.dependency_overrides[get_plugin_runtime] = lambda: runtime

    with TestClient(app) as client:
        response = client.get("/api/v1/plugins/presets")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert len(payload["presets"]) == 9
    assert {entry["id"] for entry in payload["presets"]} == {preset.id for preset in list_domain_presets()}
    assert payload["defaults"] == {
        "preset_id": "general",
        "selection_mode": "on_demand",
        "code_mode_enabled": False,
        "domain_subagents_enabled": False,
    }
    assert payload["catalog_revision"] == snapshot.revision

    names = {tool.name for tool in snapshot.tools}
    for entry in payload["presets"]:
        preset = get_domain_preset(entry["id"])
        available = {
            tool.name
            for tool in snapshot.tools
            if preset.id == "general" or tool.plugin in preset.plugin_ids
        }
        assert entry["available_tool_count"] == len(available)
        assert entry["initial_tool_count"] == len(available.intersection(preset.initial_tools))
        assert set(entry) == {
            "id", "name", "description", "plugin_ids", "initial_tools",
            "available_tool_count", "initial_tool_count",
        }
        assert available <= names

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "/Users/private" not in serialized
    assert "do-not-return" not in serialized
