"""New database-file readers cannot bypass existing v2 authorization gates."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from app.application.services import extended_visualization as extended
from app.application.services import unified_visualization as service
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.visualization import VisualizationPlugin
from test_unified_visualization import environment, invoke

CASES = [("viz-sql-dump", "sample.sql", {"dialect": "postgres"}),
         ("viz-postgres-dump", "sample.pgdump", {}), ("viz-postgres-dump", "sample.dump", {}),
         ("viz-postgres-dump", "sample.backup", {}), ("viz-postgres-dump", "sample.tar", {}),
         ("viz-bson", "sample.bson", {}), ("viz-redis-rdb", "sample.rdb", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,options", CASES)
async def test_file_gates_before_source_io(environment, plugin, filename, options):
    storage, catalog = environment; storage.infos["file"].filename = filename
    await catalog.set_state("owner", plugin, False)
    with pytest.raises(VisualizationDisabledError):
        await invoke(environment, plugin, "preview", kind="tree", options=options)
    await catalog.set_state("owner", plugin, True)
    with pytest.raises(FileNotFoundError):
        await invoke(environment, plugin, "preview", kind="tree", options=options, user="foreign")
    with pytest.raises(PreviewVersionChanged):
        await invoke(environment, plugin, "preview", kind="tree", options=options, version="f" * 64)
    with pytest.raises(service.ScientificPreviewRejected):
        await invoke(environment, plugin, "preview", kind="table", options=options)
    assert not storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,options", CASES)
@pytest.mark.parametrize("bad", ["sql", "path", "connection", "restore", "budget", "private", "format", "kind"])
async def test_rejected_inputs_never_read_file(environment, plugin, filename, options, bad):
    storage, _ = environment; info = storage.infos["file"]; info.filename = filename
    options = dict(options); kind = "tree"
    if bad in {"sql", "path", "connection", "restore"}: options[bad] = "forbidden"
    elif bad == "budget": info.size = 16777217
    elif bad == "private": info.metadata = {"source": "tool_output_spill"}
    elif bad == "format": info.filename = "unapproved.bak"
    else: kind = "series"
    with pytest.raises((FileNotFoundError, service.ScientificPreviewRejected)):
        await invoke(environment, plugin, "preview", kind=kind, options=options, version=preview_version(info))
    assert not storage.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,options", CASES)
@pytest.mark.parametrize("operation", ["bytes", "page", "prepare", "job"])
async def test_only_preview_is_approved(environment, plugin, filename, options, operation):
    storage, _ = environment; storage.infos["file"].filename = filename
    with pytest.raises(ValidationError if operation == "job" else service.ScientificPreviewRejected):
        await invoke(environment, plugin, operation, kind="tree", options=options)
    assert not storage.reads


@pytest.mark.asyncio
async def test_sql_requires_explicit_dialect_before_io(environment):
    storage, _ = environment; storage.infos["file"].filename = "sample.sql"
    for options in ({}, {"dialect": "auto"}, {"dialect": True}, {"dialect": "oracle"}):
        with pytest.raises(service.ScientificPreviewRejected):
            await invoke(environment, "viz-sql-dump", "preview", kind="tree", options=options)
    assert not storage.reads


@pytest.mark.asyncio
async def test_database_file_plugins_stop_independently(environment):
    _, catalog = environment
    for plugin in ("viz-sql-dump", "viz-postgres-dump", "viz-bson", "viz-redis-rdb"):
        await catalog.set_state("owner", plugin, False)
    for plugin in ("viz-duckdb-table", "viz-dbf-table", "viz-access-table", "viz-sqlite-table", "viz-archive-directory"):
        assert (await catalog.require_enabled("owner", plugin)).id == plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,options", CASES)
async def test_contract_rejects_unapproved_reader_and_capabilities(environment, plugin, filename, options):
    _, catalog = environment; descriptor = (await catalog.require_enabled("owner", plugin)).model_dump()
    for patch in ({"reader": "binary"}, {"permissions": ["file:read", "file:write"]},
                  {"capabilities": {**descriptor["capabilities"], "shared": True}},
                  {"capabilities": {**descriptor["capabilities"], "operations": ["job"]}}):
        with pytest.raises(ValidationError): VisualizationPlugin.model_validate({**copy.deepcopy(descriptor), **patch})


def test_records_payloads_normalize_without_widening_legacy_schemas():
    root = Path(__file__).resolve().parents[2]
    fixtures = json.loads((root / "frontend/tests/browser/database-records-data.json").read_text())
    for reader, cases in fixtures.items():
        for payload in cases.values():
            assert extended.validate_payload(payload, reader, payload["kind"], 2097152) is payload
            extended.validate_requested_selection(payload, reader, payload["kind"], payload["selected"], payload["metadata"]["format"], payload["metadata"]["source_bytes"])
            normalized = service.normalize_result({**payload, "plugin_id": "viz-" + reader, "version": "a" * 64, "revision": "b" * 64})
            assert normalized.kind == payload["kind"] and normalized.payload["view_kind"] == payload["kind"]
            assert "reader" not in normalized.payload and "type" not in normalized.payload
            with pytest.raises(ValueError): extended.validate_payload(payload, "database-table", payload["kind"], 2097152)
            with pytest.raises(ValueError): extended.validate_requested_selection(payload, reader, payload["kind"], payload["selected"], "sqlite", payload["metadata"]["source_bytes"])
