from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.application.services.visualization_catalog import (
    VisualizationCatalogService, VisualizationDisabledError, VisualizationNotFoundError,
)
from app.domain.external.plugin_runtime import PluginRuntimeUnavailableError, PluginRuntimeProtocolError
from app.domain.models.visualization import VisualizationPlugin, VisualizationSnapshot
from app.infrastructure.external.plugins.node_runtime import NodePluginRuntime
from app.infrastructure.repositories.mongo_visualization_repository import MongoVisualizationRepository
from app.interfaces.api.visualization_routes import router
from app.interfaces.dependencies import get_current_user, get_visualization_catalog


def manifest():
    return {
        "contract_version": 1, "id": "netcdf-map", "version": "1.0.0", "name": "NetCDF map",
        "description": "Geographic view", "extensions": ["nc"], "filenames": [],
        "view_kind": "map", "adapter": "scientific-map", "data_kind": "scientific", "reader": "netcdf",
        "default_enabled": True, "priority": 50, "permissions": ["file:read"],
        "limits": {"max_input_bytes": 67108864, "max_output_bytes": 524288},
    }


class MemoryPreferences:
    def __init__(self):
        self.users = {}

    async def get_states(self, user_id):
        return dict(self.users.get(user_id, {}))

    async def set_state(self, user_id, plugin_id, enabled):
        self.users.setdefault(user_id, {})[plugin_id] = enabled


@pytest.fixture
def service():
    snapshot = VisualizationSnapshot(engine="cordis", revision="a" * 64, plugins=[VisualizationPlugin(**manifest())])
    runtime = SimpleNamespace(visualization_snapshot=AsyncMock(return_value=snapshot))
    return VisualizationCatalogService(runtime, MemoryPreferences())


@pytest.mark.asyncio
async def test_visualization_switch_is_persisted_per_owner_and_does_not_disable_other_user(service):
    assert (await service.require_enabled("alice", "netcdf-map")).id == "netcdf-map"
    disabled = await service.set_state("alice", "netcdf-map", False)
    assert disabled.enabled is False
    with pytest.raises(VisualizationDisabledError):
        await service.require_enabled("alice", "netcdf-map")
    assert (await service.require_enabled("bob", "netcdf-map")).id == "netcdf-map"
    restored = VisualizationCatalogService(service.runtime, service.repository)
    assert (await restored.list_for_user("alice")).plugins[0].enabled is False
    await restored.set_state("alice", "netcdf-map", True)
    assert (await restored.list_for_user("alice")).plugins[0].enabled is True


@pytest.mark.asyncio
async def test_unknown_plugin_cannot_create_preference_or_receive_data(service):
    with pytest.raises(VisualizationNotFoundError):
        await service.set_state("alice", "unknown-plugin", False)
    with pytest.raises(VisualizationNotFoundError):
        await service.require_enabled("alice", "unknown-plugin")
    assert service.repository.users == {}


@pytest.mark.asyncio
async def test_visualization_unavailable_fails_closed_without_builtin_fallback(service):
    service.runtime.visualization_snapshot.side_effect = PluginRuntimeUnavailableError("private debug details")
    with pytest.raises(PluginRuntimeUnavailableError):
        await service.list_for_user("alice")
    with pytest.raises(PluginRuntimeUnavailableError):
        await service.require_enabled("alice", "netcdf-map")
    with pytest.raises(PluginRuntimeUnavailableError):
        await VisualizationCatalogService(None, service.repository).list_for_user("alice")


@pytest.mark.parametrize("update", [
    {"entry": "https://bad.test/script.js"}, {"api_url": "http://127.0.0.1"},
    {"contract_version": True}, {"contract_version": 2}, {"default_enabled": "false"},
    {"adapter": "remote-script"}, {"view_kind": "image"}, {"reader": "fastq"},
    {"permissions": ["file:write"]}, {"extensions": ["../private"]},
    {"extensions": ["nc", "nc"]}, {"priority": 1.5},
    {"limits": {"max_input_bytes": True, "max_output_bytes": 123}},
])
def test_visualization_protocol_rejects_unsafe_or_inconsistent_descriptors(update):
    with pytest.raises(ValidationError):
        VisualizationPlugin.model_validate({**manifest(), **update})


def test_all_shipped_manifests_match_python_and_file_selection():
    directory = Path(__file__).resolve().parents[2] / "plugin-host" / "visualizations"
    plugins = [VisualizationPlugin.model_validate_json(path.read_text()) for path in directory.glob("*.json")]
    assert len(plugins) == 34
    assert sum(plugin.contract_version == 1 for plugin in plugins) == 14
    assert sum(plugin.contract_version == 2 for plugin in plugins) == 20
    fastq = next(plugin for plugin in plugins if plugin.id == "fastq-quality")
    assert fastq.matches_filename("Reads.FASTQ")
    assert fastq.matches_filename("Reads.fq")
    assert not fastq.matches_filename("Reads.FASTQ.GZ")
    assert not fastq.matches_filename("Reads.fq.gz")
    assert not fastq.matches_filename("fastq.gz.exe")
    assert next(plugin for plugin in plugins if plugin.id == "molecular").matches_filename("POSCAR")
    assert next(plugin for plugin in plugins if plugin.id == "text").limits.max_input_bytes == 65536
    assert next(plugin for plugin in plugins if plugin.id == "csv").limits.max_input_bytes == 131072


def test_contract_supports_explicit_compound_suffix_without_enabling_unsupported_readers():
    plugin = VisualizationPlugin.model_validate({**manifest(), "extensions": ["example.nc"]})
    assert plugin.matches_filename("sample.EXAMPLE.NC")
    assert not plugin.matches_filename("sample.EXAMPLE.NC.exe")


def test_routes_require_strict_boolean_owner_identity_and_safe_errors(service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_visualization_catalog] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="alice")
    with TestClient(app) as client:
        assert client.get("/visualizations").json()["data"]["engine"] == "cordis"
        assert client.patch("/visualizations/netcdf-map/state", json={"enabled": "false"}).status_code == 422
        assert client.patch("/visualizations/netcdf-map/state", json={"enabled": False, "user_id": "bob"}).status_code == 422
        response = client.patch("/visualizations/netcdf-map/state", json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["data"]["enabled"] is False
        assert service.repository.users == {"alice": {"netcdf-map": False}}
        assert client.patch("/visualizations/unknown/state", json={"enabled": True}).status_code == 404
        service.runtime.visualization_snapshot.side_effect = PluginRuntimeUnavailableError("/private/secret")
        response = client.get("/visualizations")
        assert response.status_code == 503
        assert "secret" not in response.text


@pytest.mark.asyncio
async def test_mongo_preferences_only_query_owner_and_atomically_update_one_plugin():
    collection = SimpleNamespace(find_one=AsyncMock(return_value={"states": {"netcdf-map": False, "invalid": "false"}}), update_one=AsyncMock())
    repository = MongoVisualizationRepository(collection)
    assert await repository.get_states("alice") == {"netcdf-map": False}
    collection.find_one.assert_awaited_once_with({"_id": "alice"}, {"states": 1})
    await repository.set_state("alice", "netcdf-map", False)
    collection.update_one.assert_awaited_once_with({"_id": "alice"}, {"$set": {"states.netcdf-map": False}}, upsert=True)
    with pytest.raises(ValueError):
        await repository.set_state("alice", "a.b", False)


@pytest.mark.asyncio
async def test_node_visualization_snapshot_is_validated_without_mutating_tool_generation(tmp_path):
    runtime = NodePluginRuntime(host_path=tmp_path / "host.js", tools_dir=tmp_path)
    runtime.start = AsyncMock()
    runtime._request = AsyncMock(return_value={"engine": "cordis", "revision": "a" * 64, "plugins": [manifest()]})
    original = runtime._snapshot
    result = await runtime.visualization_snapshot()
    assert result.plugins[0].reader == "netcdf"
    assert runtime._snapshot is original
    runtime._request.return_value["plugins"][0]["entry"] = "bad.js"
    with pytest.raises(PluginRuntimeProtocolError):
        await runtime.visualization_reload()
    assert runtime._snapshot is original
