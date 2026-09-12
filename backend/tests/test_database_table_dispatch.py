"""Independent database plugins retain all existing unified file gates."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from app.application.services import unified_visualization as service
from app.application.services import extended_visualization as extended
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.visualization import VisualizationPlugin
from test_unified_visualization import environment, invoke

CASES = [("viz-duckdb-table", "sample.duckdb"), ("viz-duckdb-table", "sample.ddb"),
         ("viz-dbf-table", "sample.dbf"), ("viz-access-table", "sample.mdb"), ("viz-access-table", "sample.accdb")]
SELECTION = {"table": "t-" + "a" * 24, "columns": [0], "row_offset": 0, "row_limit": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename", CASES)
async def test_disabled_foreign_version_and_missing_version_before_source_read(environment, plugin, filename):
    storage, catalog = environment; storage.infos["file"].filename = filename
    await catalog.set_state("owner", plugin, False)
    with pytest.raises(VisualizationDisabledError): await invoke(environment, plugin, "preview", kind="tree")
    await catalog.set_state("owner", plugin, True)
    with pytest.raises(FileNotFoundError): await invoke(environment, plugin, "preview", kind="tree", user="foreign")
    with pytest.raises(PreviewVersionChanged): await invoke(environment, plugin, "preview", kind="tree", version="f" * 64)
    with pytest.raises(service.ScientificPreviewRejected): await invoke(environment, plugin, "preview", kind="table", options=SELECTION)
    assert storage.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename", CASES)
@pytest.mark.parametrize("operation", ["bytes", "page", "prepare"])
async def test_unapproved_operations_cannot_bypass_native_reader(environment, plugin, filename, operation):
    storage, _ = environment; storage.infos["file"].filename = filename
    with pytest.raises(service.ScientificPreviewRejected): await invoke(environment, plugin, operation, kind="tree")
    assert not storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename", CASES)
@pytest.mark.parametrize("bad", ["sql", "path", "budget", "private", "format", "columns"])
async def test_options_storage_and_size_rejected_before_read(environment, plugin, filename, bad):
    storage, _ = environment; storage.infos["file"].filename = filename
    kind, options = "tree", {}
    if bad in {"sql", "path"}: options = {bad: "private-token"}
    elif bad == "budget": storage.infos["file"].size = 16777217
    elif bad == "private": storage.infos["file"].metadata = {"source": "tool_output_spill"}
    elif bad == "format": storage.infos["file"].filename = "backup.bak"
    else: kind, options = "table", {**SELECTION, "columns": [True]}
    with pytest.raises((FileNotFoundError, service.ScientificPreviewRejected)):
        await invoke(environment, plugin, "preview", kind=kind, options=options, version=preview_version(storage.infos["file"]))
    assert not storage.reads


@pytest.mark.asyncio
async def test_database_plugin_disable_does_not_disable_other_databases_or_sqlite(environment):
    _, catalog = environment
    await catalog.set_state("owner", "viz-duckdb-table", False)
    for plugin in ("viz-dbf-table", "viz-access-table", "viz-sqlite-table", "shapefile"):
        assert (await catalog.require_enabled("owner", plugin)).id == plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename", CASES)
async def test_reader_sharing_and_write_permissions_remain_closed(environment, plugin, filename):
    _, catalog = environment; descriptor = (await catalog.require_enabled("owner", plugin)).model_dump()
    for patch in ({"reader": "binary"}, {"permissions": ["file:read", "file:write"]},
                  {"capabilities": {**descriptor["capabilities"], "shared": True}},
                  {"capabilities": {**descriptor["capabilities"], "operations": ["job"]}}):
        with pytest.raises(ValidationError): VisualizationPlugin.model_validate({**copy.deepcopy(descriptor), **patch})


def test_native_payload_uses_existing_envelope_without_expanding_legacy_limits():
    root = Path(__file__).resolve().parents[2]
    fixtures = json.loads((root / "frontend/tests/browser/database-native-data.json").read_text())
    for payload in fixtures.values():
        assert extended.validate_payload(payload, "database-table", payload["kind"], 2097152) is payload
        extended.validate_requested_selection(payload, "database-table", payload["kind"], payload["selected"], "duckdb", payload["metadata"]["source_bytes"])
        raw = {**payload, "plugin_id": "viz-duckdb-table", "version": "a" * 64, "revision": "b" * 64}
        normalized = service.normalize_result(raw)
        assert normalized.kind == payload["kind"] and normalized.payload["view_kind"] == payload["kind"]
        assert "reader" not in normalized.payload and "type" not in normalized.payload
        with pytest.raises(ValueError): extended.validate_payload(payload, "sqlite-table", payload["kind"], 2097152)
        with pytest.raises(ValueError): extended.validate_requested_selection(payload, "database-table", payload["kind"], payload["selected"], "dbf", payload["metadata"]["source_bytes"])
