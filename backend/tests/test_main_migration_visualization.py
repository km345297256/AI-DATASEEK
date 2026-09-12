"""Compatibility/security tests for the six additive Cordis workbenches."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services import extended_visualization as extended
from app.application.services import main_migration_visualization as migration
from app.application.services import unified_visualization as unified
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import FileService
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.domain.models.file import FileInfo
from app.domain.models.visualization import VisualizationSnapshot

ROOT = Path(__file__).resolve().parents[2]


class Storage:
    def __init__(self, reader):
        fmt = {"matrix-workbench":"npy", "astronomy-workbench":"fits", "alignment-browser":"sam", "sequence-browser":"fa", "genome-tracks":"bed", "blast-hits":"blast6"}[reader]
        self.info = FileInfo(file_id="opaque", user_id="owner", filename="synthetic."+fmt, size=8)
        self.reads = []
    async def get_file_info(self, file_id, user_id):
        return self.info if file_id == "opaque" and user_id == "owner" else None
    async def download_file_range(self, file_id, user_id, *, offset, length):
        if await self.get_file_info(file_id,user_id) is None: raise FileNotFoundError()
        self.reads.append((offset,length)); return b"0"*length, self.info
    async def download_file(self, *args, **kwargs): raise AssertionError("Unbounded source read")


class Preferences:
    def __init__(self): self.values = {}
    async def get_states(self,user): return self.values.get(user,{})
    async def set_state(self,user,plugin,enabled): self.values.setdefault(user,{})[plugin]=enabled


def environment(reader):
    manifests=[json.loads(p.read_text()) for p in (ROOT/"plugin-host/visualizations").glob("*.json")]
    snapshot=VisualizationSnapshot(engine="cordis",revision="a"*64,plugins=manifests)
    return Storage(reader),VisualizationCatalogService(SimpleNamespace(visualization_snapshot=AsyncMock(return_value=snapshot)),Preferences())


def test_main_has_six_additive_trusted_plugins_without_legacy_registry():
    assert len(migration.KINDS)==6
    for reader,kinds in migration.KINDS.items():
        manifest=json.loads((ROOT/f"plugin-host/visualizations/{reader}.json").read_text())
        assert manifest["reader"]==manifest["adapter"]==reader and manifest["id"]=="viz-"+reader
        assert manifest["capabilities"]=={"operations":["preview"],"input_mode":"whole","shared":False}
        assert manifest["priority"]==-20 and manifest["default_enabled"] is True
        assert manifest["limits"]["max_input_bytes"]==migration.INPUT_LIMITS[reader]
        assert manifest["limits"]["max_output_bytes"]==migration.OUTPUT_LIMITS[reader]
        migration.validate_options(reader,"tree",{})
        for invalid in ({"path":"/private/source"},{"reader":"office"},{"url":"https://invalid.example"}):
            with pytest.raises((ValueError,TypeError)):migration.validate_options(reader,"tree",invalid)


@pytest.mark.parametrize("reader",migration.KINDS)
def test_no_native_parser_in_host_validator_and_exact_pure_copy(reader):
    sandbox_name={"matrix-workbench":"main_matrix_payload", "astronomy-workbench":"astronomy_workbench_payload", "alignment-browser":"alignment_browser_payload"}.get(reader,"sequence_browser_payload")
    host_name={"matrix-workbench":"main_matrix_visualization", "astronomy-workbench":"astronomy_workbench_visualization", "alignment-browser":"alignment_browser_visualization"}.get(reader,"sequence_browser_visualization")
    pure=(ROOT/f"sandbox/app/services/{sandbox_name}.py").read_text()
    assert pure==(ROOT/f"backend/app/application/services/{host_name}.py").read_text()
    import ast
    native={"pysam","numpy","astropy","rasterio","h5py","scipy","tifffile"}
    for node in ast.walk(ast.parse(pure)):
        if isinstance(node,ast.Import):assert not any(n.name.split('.')[0] in native for n in node.names)
        if isinstance(node,ast.ImportFrom):assert (node.module or '').split('.')[0] not in native


@pytest.mark.asyncio
@pytest.mark.parametrize("reader",migration.KINDS)
async def test_unified_requires_client_version_before_injecting_host_version(reader):
    storage,catalog=environment(reader)
    for kind in migration.KINDS[reader]-{"tree"}:
        with pytest.raises(unified.ScientificPreviewRejected):
            await unified.unified_visualization(FileService(storage),catalog,"sandbox:test","opaque","owner",
                unified.VisualizationRequest(plugin_id="viz-"+reader,operation="preview",kind=kind))
    assert storage.reads==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("reader",migration.KINDS)
@pytest.mark.parametrize("case",["foreign","disabled","oversize","stale","spill","options","operation"])
async def test_new_workbench_rejects_before_io(reader,case):
    storage,catalog=environment(reader); options={}; user="owner"; version=None
    if case=="foreign":user="other"
    if case=="disabled":await catalog.set_state(user,"viz-"+reader,False)
    if case=="oversize":storage.info.size=migration.INPUT_LIMITS[reader]+1
    if case=="stale":version="f"*64
    if case=="spill":storage.info.metadata={"source":"tool_output_spill"}
    if case=="options":options={"path":"/private/secret"}
    with pytest.raises((FileNotFoundError,VisualizationDisabledError,PreviewVersionChanged,extended.ScientificPreviewRejected)):
        await extended.extended_visualization(FileService(storage),catalog,"sandbox:test","opaque",user,
            extended.ExtendedPreviewRequest(plugin_id="viz-"+reader,kind="tree",options=options,version=version),
            binary=case=="operation",worker=lambda *a,**kw:pytest.fail("native parser called"))
    assert storage.reads==[]


@pytest.mark.asyncio
async def test_matrix_segments_keep_storage_cap_and_version_fence(monkeypatch):
    storage,catalog=environment("matrix-workbench"); monkeypatch.setattr(extended,"MAX_INPUT",4)
    # Force two small chunks using the exact production loop without allocating
    # 128 MiB in a contract test. A version race must prevent native execution.
    original=storage.download_file_range
    async def read(*args,**kw):
        value,info=await original(*args,**kw)
        if kw["offset"]:info=info.model_copy(update={"size":9})
        return value,info
    storage.download_file_range=read
    with pytest.raises(PreviewVersionChanged):
        await extended.extended_visualization(FileService(storage),catalog,"sandbox:test","opaque","owner",
            extended.ExtendedPreviewRequest(plugin_id="viz-matrix-workbench",kind="tree"),worker=lambda *a,**kw:pytest.fail("native parser called"))
    assert storage.reads==[(0,4),(4,4)]


@pytest.mark.parametrize("reader",migration.KINDS)
def test_public_normalizer_keeps_declared_result_kind(reader):
    field={"matrix-workbench":"matrix","astronomy-workbench":"workbench","alignment-browser":"alignment","sequence-browser":"sequence","genome-tracks":"tracks","blast-hits":"hits"}[reader]
    for kind in migration.KINDS[reader]:
        value={"plugin_id":"viz-"+reader,"version":"a"*64,"revision":"b"*64,"reader":reader,"type":reader,"contract_version":2,"kind":kind,"metadata":{},"warnings":[],"sampled":False,field:[] if reader=="genome-tracks" else {}}
        result=unified.normalize_result(value)
        expected="array" if reader=="matrix-workbench" and kind=="image" else "raster" if reader=="astronomy-workbench" and kind=="image" else "features" if kind=="map" else kind
        assert result.kind==expected and result.payload["view_kind"]==kind
        if kind!="tree":
            del value[field]
            with pytest.raises(unified.VisualizationWorkerError):unified.normalize_result(value)


@pytest.mark.parametrize("reader",migration.KINDS)
@pytest.mark.parametrize("value",[None,{},[],{"reader":"office"},{"contract_version":True}])
def test_arbitrary_worker_payload_cannot_enter_dedicated_schema(reader,value):
    with pytest.raises((ValueError,TypeError)):
        extended.validate_payload(value,reader,"tree",migration.OUTPUT_LIMITS[reader])
