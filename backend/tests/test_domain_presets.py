import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot, PluginDescriptor, PluginToolDefinition,
)
from app.domain.models.domain_preset import DomainPreset
from app.domain.models.tool_result import ToolResult
from app.domain.services.domain_presets import (
    describe_domain_presets, get_domain_preset, list_domain_presets,
    validate_domain_preset_references,
)
from app.domain.services.tools.pipeline import ToolExecutionInterceptor
from app.domain.services.tools.plugin import PluginToolkit, default_plugin_directory
from app.domain.services.tools.registry import ToolRegistry
from app.domain.services.tools.tool_selection import PluginToolView, ToolDiscoveryToolkit


@pytest.fixture
def catalog():
    # Exercise the repository's real manifest names/contracts without a Node,
    # model or sandbox request. A separate existing suite covers Cordis parsing.
    source = PluginToolkit(SimpleNamespace(), session_id="source", plugins_dir=default_plugin_directory())
    definitions = tuple(PluginToolDefinition.model_validate(value) for value in source._definitions.values())
    counts = Counter(value.plugin for value in definitions)
    versions = {value.plugin: value.version for value in definitions}
    return PluginCatalogSnapshot(
        engine="cordis", version="4.0.2", revision="a" * 64,
        manifest_digest="b" * 64, execution_bundle_digest="c" * 64,
        plugin_count=len(counts), tool_count=len(definitions),
        plugins=tuple(PluginDescriptor(plugin=name, version=versions[name], manifest_digest="d" * 64, tool_count=count) for name, count in counts.items()),
        tools=definitions,
    )


@pytest.fixture
def toolkit(catalog):
    return PluginToolkit(SimpleNamespace(), session_id="test-session", plugin_runtime=SimpleNamespace(current_snapshot=catalog))


def test_all_static_preset_references_exist_in_actual_catalog(catalog):
    validate_domain_preset_references(catalog)
    assert len(list_domain_presets()) == 9
    assert catalog.tool_count >= 280
    assert len(get_domain_preset("general").initial_tools) <= 8


def test_invalid_preset_fails_closed():
    with pytest.raises(ValueError, match="Unknown domain preset"):
        get_domain_preset("invented")
    with pytest.raises(ValueError):
        DomainPreset(id="bad", name="Bad", description="Bad", plugin_ids=(), initial_tools=("a",), instructions="Bad")
    with pytest.raises(ValueError):
        get_domain_preset("general").name = "modified"


def test_missing_plugins_produce_safe_zero_counts():
    views = describe_domain_presets(PluginCatalogSnapshot.unavailable())
    assert all(view["available_tool_count"] == view["initial_tool_count"] == 0 for view in views)
    assert all(set(view) == {"id", "name", "description", "plugin_ids", "initial_tools", "available_tool_count", "initial_tool_count"} for view in views)


def test_missing_initial_tool_is_not_silently_replaced(tmp_path):
    empty = PluginToolkit(SimpleNamespace(), session_id="empty", plugins_dir=tmp_path)
    view = PluginToolView(empty)
    assert view.get_tools() == []
    assert view.load(["data_format_inspect"]).success is False


@pytest.mark.parametrize("preset", list_domain_presets(), ids=lambda value: value.id)
def test_presets_start_small_and_scope_matches_public_counts(toolkit, catalog, preset):
    view = PluginToolView(toolkit, preset_id=preset.id)
    advertised = next(value for value in describe_domain_presets(catalog) if value["id"] == preset.id)
    assert 1 <= len(view.get_tools()) <= 8
    assert view.selection_snapshot()["available_tool_count"] == advertised["available_tool_count"]
    assert view.selection_snapshot()["loaded_tool_count"] == advertised["initial_tool_count"]
    assert set(view.loaded_tool_names) == set(preset.initial_tools)
    assert view.dataset_fast_path_tool_names <= toolkit.dataset_fast_path_tool_names


def test_legacy_all_preserves_complete_catalog_and_on_demand_does_not(toolkit):
    legacy = PluginToolView(toolkit, selection_mode="all")
    small = PluginToolView(toolkit)
    assert len(legacy.get_tools()) == len(toolkit.get_tools())
    assert len(small.get_tools()) == 3
    assert legacy.get_tool("space_ground_track") is not None
    assert small.get_tool("space_ground_track") is None
    assert len(toolkit.get_tools()) >= 280


def test_domain_all_and_search_cannot_cross_preset_scope(toolkit):
    view = PluginToolView(toolkit, preset_id="tabular", selection_mode="all")
    assert view.get_tool("table_profile") is not None
    assert view.get_tool("space_ground_track") is None
    assert view.load(["space_ground_track"]).success is False
    assert view.search(plugin_id="space").data["matches"] == []
    assert {schema["function"]["name"] for schema in view.get_tools()} != {
        schema["function"]["name"] for schema in toolkit.get_tools()
    }


def test_search_load_is_bounded_and_atomic_and_does_not_execute(toolkit):
    toolkit.call_tool = AsyncMock()
    view = PluginToolView(toolkit, max_loaded_tools=8)
    before = view.selection_snapshot()
    assert view.search(limit=13).success is False
    assert view.search(limit=True).success is False
    assert view.search(query="x" * 161).success is False
    assert view.search(plugin_id="/Users/private").success is False
    assert view.load(["table_profile", "invented_tool"]).success is False
    assert view.selection_snapshot() == before
    assert view.load(["table_profile"] * 9).success is False
    assert view.load([]).success is False
    assert view.load(["/Users/private"]).success is False
    assert view.load(["table_profile", "table_extract", "table_filter_aggregate", "table_join_compare", "table_pivot"]).success
    full = view.selection_snapshot()
    assert full["loaded_tool_count"] == 8
    assert view.load(["table_schema_infer"]).success is False
    assert view.selection_snapshot() == full
    assert view.load(["table_profile"]).success
    assert view.selection_snapshot() == full
    assert view.get_tool("table_profile") is not None
    toolkit.call_tool.assert_not_called()


def test_search_returns_only_bounded_public_metadata_without_echo(toolkit):
    view = PluginToolView(toolkit)
    output = view.search("表格", limit=2)
    assert output.success and len(output.data["matches"]) == 2
    assert output.data["has_more"]
    assert set(output.data["matches"][0]) == {"name", "plugin", "version", "description", "loaded"}
    assert "parameters" not in json.dumps(output.model_dump())
    assert "query" not in output.data
    assert all(len(item["description"]) <= 600 for item in output.data["matches"])


def test_catalog_metadata_redacts_host_paths_and_credentials(toolkit):
    toolkit._schemas[0]["function"]["description"] = "Input /Users/alice/private/data.csv with api_key=never_disclose"
    name = toolkit._schemas[0]["function"]["name"]
    view = PluginToolView(toolkit)
    serialized = json.dumps(view.search(name).model_dump())
    assert "/Users/alice" not in serialized and "never_disclose" not in serialized
    assert "protected path" in serialized


@pytest.mark.asyncio
async def test_views_are_task_local_reset_rejects_cached_handles_and_snapshots_unchanged(toolkit):
    one, two = PluginToolView(toolkit), PluginToolView(toolkit)
    original = toolkit.catalog_snapshot.model_dump_json()
    assert one.load(["table_profile"]).success
    cached = one.get_tool("table_profile")
    assert two.get_tool("table_profile") is None
    one.reset()
    assert one.get_tool("table_profile") is None
    with pytest.raises(ValueError, match="not loaded"):
        await cached.ainvoke({"name": "table_profile", "args": {}, "id": "stale", "type": "tool_call"})
    assert toolkit.catalog_snapshot.model_dump_json() == original


@pytest.mark.asyncio
async def test_selected_plugin_keeps_original_governed_pipeline(toolkit):
    calls = []

    class Guard(ToolExecutionInterceptor):
        async def guard(self, context):
            calls.append((context.tool_name, context.metadata["plugin"]))

    view = PluginToolView(toolkit)
    assert view.tool_execution_pipeline is toolkit.tool_execution_pipeline
    ToolRegistry([toolkit]).register_interceptor(Guard())
    toolkit.call_tool = AsyncMock(return_value=ToolResult(success=True, data={"ok": True}))
    result = await view.get_tool("data_format_inspect").ainvoke({
        "name": "data_format_inspect", "args": {"input_paths": ["/home/ubuntu/datasets/test/input.csv"]}, "id": "call1", "type": "tool_call",
    })
    assert result.artifact.success
    assert calls == [("data_format_inspect", "data_foundation")]
    toolkit.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_is_pipeline_governed_and_usable_in_dataset_fast_path(toolkit):
    view = PluginToolView(toolkit)
    discovery = ToolDiscoveryToolkit(view)
    assert discovery.dataset_fast_path_tool_names == {"tool_catalog_search", "tool_catalog_load"}
    calls = []

    class Guard(ToolExecutionInterceptor):
        async def guard(self, context):
            calls.append(context.tool_name)

    registry = ToolRegistry([toolkit, discovery])
    registry.assert_unique_tool_names()
    registry.register_interceptor(Guard())
    result = await discovery.get_tool("tool_catalog_load").ainvoke({
        "name": "tool_catalog_load", "args": {"tool_names": ["table_profile"]}, "id": "load1", "type": "tool_call",
    })
    assert result.artifact.success
    assert calls == ["tool_catalog_load"]
    assert view.get_tool("table_profile") is not None


def test_returned_schemas_and_selection_snapshots_cannot_mutate_catalog(toolkit):
    view = PluginToolView(toolkit)
    schemas = view.get_tools()
    schemas[0]["function"]["name"] = "modified"
    state = view.selection_snapshot()
    state["loaded_tool_names"].clear()
    assert len(view.loaded_tool_names) == 3
    assert "modified" not in view.loaded_tool_names


def test_view_disable_is_local_and_plugin_disable_remains_authoritative(toolkit):
    one, two = PluginToolView(toolkit), PluginToolView(toolkit)
    one.set_enabled(False)
    assert one.get_tools() == [] and one.get_tool("data_format_inspect") is None
    assert one.load(["table_profile"]).success is False
    assert len(two.get_tools()) == 3 and toolkit.enabled
    toolkit.set_enabled(False)
    assert two.get_tools() == [] and two.get_tool("data_format_inspect") is None
    assert two.search().success is False
