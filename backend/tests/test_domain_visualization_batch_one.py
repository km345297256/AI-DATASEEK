"""New domain capabilities retain the existing public authorization boundary."""
import copy

import pytest
from pydantic import ValidationError

from app.application.services import unified_visualization as service
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.visualization import VisualizationPlugin
from test_unified_visualization import environment, invoke


CASES = [("viz-columnar-window", "table.parquet", "table"),
         ("viz-nexus-window", "experiment.nxs", "series"),
         ("viz-scientific-graph", "network.graphml", "graph")]


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind", CASES)
async def test_new_plugin_disable_and_owner_gate_before_any_read(environment, plugin, filename, kind):
    storage, catalog = environment
    storage.infos["file"].filename = filename
    await catalog.set_state("owner", plugin, False)
    with pytest.raises(VisualizationDisabledError):
        await invoke(environment, plugin, "preview", kind=kind)
    await catalog.set_state("owner", plugin, True)
    with pytest.raises(FileNotFoundError):
        await invoke(environment, plugin, "preview", user="foreign", kind=kind)
    assert not storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind", CASES[:2])
async def test_no_host_supplied_version_substitutes_for_client_catalog_pin(environment, plugin, filename, kind):
    storage, _ = environment
    storage.infos["file"].filename = filename
    with pytest.raises(service.ScientificPreviewRejected, match="同一文件版本"):
        await invoke(environment, plugin, "preview", kind=kind)
    assert not storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind", CASES)
async def test_stale_file_version_rejected_before_new_reader(environment, plugin, filename, kind):
    storage, _ = environment
    storage.infos["file"].filename = filename
    with pytest.raises(PreviewVersionChanged):
        await invoke(environment, plugin, "preview", version="f" * 64, kind=kind)
    assert not storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind", CASES)
@pytest.mark.parametrize("operation", ["bytes", "prepare", "page"])
async def test_no_new_plugin_implicitly_grants_unapproved_operations(environment, plugin, filename, kind, operation):
    storage, _ = environment
    storage.infos["file"].filename = filename
    with pytest.raises(service.ScientificPreviewRejected):
        await invoke(environment, plugin, operation, kind=kind, version=preview_version(storage.infos["file"]))
    assert not storage.reads


@pytest.mark.asyncio
async def test_scientific_network_and_nexus_do_not_replace_existing_defaults(environment):
    _, catalog = environment
    plugins = (await catalog.list_for_user("owner")).plugins
    def matches(filename):
        return sorted((p for p in plugins if p.enabled and p.matches_filename(filename)), key=lambda p: (-p.priority, p.id))
    assert matches("values.json")[0].id == "viz-structured-tree"
    assert matches("experiment.nxs")[0].id == "viz-h5web"
    assert {p.id for p in matches("experiment.h5")} >= {"viz-h5web", "viz-array-window", "viz-nexus-window"}
    await catalog.set_state("owner", "viz-nexus-window", False)
    assert (await catalog.require_enabled("owner", "viz-h5web")).reader == "hdf5"
    assert (await catalog.require_enabled("owner", "viz-array-window")).reader == "array-window"


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind", CASES)
async def test_new_manifests_cannot_widen_reader_shared_or_execution_scope(environment, plugin, filename, kind):
    _, catalog = environment
    item = (await catalog.require_enabled("owner", plugin)).model_dump()
    for change in ({"reader": "binary"}, {"permissions": ["file:read", "file:write"]},
                   {"capabilities": {**item["capabilities"], "shared": True}},
                   {"capabilities": {**item["capabilities"], "operations": ["job"]}}):
        with pytest.raises(ValidationError):
            VisualizationPlugin.model_validate({**copy.deepcopy(item), **change})


def test_graph_is_an_explicit_result_kind_not_geography_or_file_structure():
    result = service.normalize_result({"contract_version": 2, "plugin_id": "viz-scientific-graph",
        "version": "a" * 64, "revision": "b" * 64, "type": "scientific-graph", "reader": "scientific-graph",
        "kind": "graph", "media_type": "application/json", "graph": {"directed": False, "nodes": [], "edges": []},
        "metadata": {}, "warnings": [], "sampled": False})
    assert result.kind == "graph" and result.payload["view_kind"] == "graph"
    assert "graph" in result.payload and not {"tree", "geojson", "reader", "type"} & result.payload.keys()
