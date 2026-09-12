"""Batch five additive SQLite, radar and UGRID contract, identity and capability regressions."""
import copy
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.application.services import unified_visualization as service
from app.application.services import extended_visualization as extended
from app.application.services.file_preview import PreviewVersionChanged,preview_version
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.visualization import VisualizationPlugin
from app.infrastructure.external.sandbox.window_visualization_worker import WINDOW_PROFILES,validate_limits
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError
from test_unified_visualization import environment,invoke

CASES=[
 ("viz-sqlite-table","synthetic.sqlite","table",{"table":"t-"+"a"*24,"columns":[0],"row_offset":0,"row_limit":20}),
 ("viz-radar-window","synthetic.h5","image",{"sweep":1,"quantity":"DBZH","ray_start":0,"ray_count":1,"gate_start":0,"gate_count":1,"decode":"raw"}),
 ("viz-ugrid-window","synthetic.nc","geometry",{"mesh":"u-"+"a"*32,"field":None,"indices":[]}),
]
@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind,options",CASES)
async def test_disabled_foreign_and_stale_snapshot_before_any_read(environment,plugin,filename,kind,options):
    storage,catalog=environment;storage.infos["file"].filename=filename
    await catalog.set_state("owner",plugin,False)
    with pytest.raises(VisualizationDisabledError):await invoke(environment,plugin,"preview",kind=kind,options=options)
    await catalog.set_state("owner",plugin,True)
    with pytest.raises(FileNotFoundError):await invoke(environment,plugin,"preview",kind=kind,options=options,user="foreign")
    with pytest.raises(PreviewVersionChanged):await invoke(environment,plugin,"preview",kind=kind,options=options,version="f"*64)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind,options",CASES)
async def test_client_version_is_required_not_silently_injected(environment,plugin,filename,kind,options):
    storage,_=environment;storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected):await invoke(environment,plugin,"preview",kind=kind,options=options)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind,options",CASES)
@pytest.mark.parametrize("bad",["private","mismatch","options"])
async def test_private_spill_mismatched_format_and_path_options_fail_closed(environment,plugin,filename,kind,options,bad):
    storage,_=environment;storage.infos["file"].filename=filename
    if bad=="private":storage.infos["file"].metadata={"source":"tool_output_spill"}
    elif bad=="mismatch":storage.infos["file"].filename="wrong.txt"
    else:options={**options,"path":"/private/file"}
    with pytest.raises((FileNotFoundError,service.ScientificPreviewRejected)):
        await invoke(environment,plugin,"preview",kind=kind,options=options,version=preview_version(storage.infos["file"]))
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind,options",CASES)
@pytest.mark.parametrize("operation",["bytes","page","prepare"])
async def test_no_raw_page_or_prepare_capability_added(environment,plugin,filename,kind,options,operation):
    storage,_=environment;storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected):await invoke(environment,plugin,operation,kind=kind,options=options)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind,options",CASES)
async def test_approved_manifests_cannot_borrow_other_readers_or_sharing(environment,plugin,filename,kind,options):
    _,catalog=environment;original=(await catalog.require_enabled("owner",plugin)).model_dump()
    for patch in ({"reader":"binary"},{"reader":"netcdf"},{"permissions":["file:read","file:write"]},
        {"capabilities":{**original["capabilities"],"shared":True}},
        {"capabilities":{**original["capabilities"],"operations":["job"]}},
        {"capabilities":{**original["capabilities"],"input_mode":"prefix"}}):
        with pytest.raises(ValidationError):VisualizationPlugin.model_validate({**copy.deepcopy(original),**patch})

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename",[
 ("viz-array-window","file.h5"),("viz-fcs-window","file.fcs"),("netcdf-map","file.nc"),
 ("viz-h5web","file.h5"),("viz-scientific-graph","file.graphml")])
async def test_legacy_readers_do_not_acquire_geometry_by_request(environment,plugin,filename):
    storage,_=environment;storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected):
        await invoke(environment,plugin,"preview",kind="geometry",options={},version=preview_version(storage.infos["file"]))
    assert not storage.reads

GEOMETRY_FIELDS={"ugrid-window":"ugrid"}
def internal(reader,kind="geometry"):
    return {"contract_version":2,"reader":reader,"type":reader,"kind":kind,"media_type":"application/json",
      "plugin_id":"test-plugin","version":"a"*64,"revision":"b"*64,"metadata":{},"warnings":[],"sampled":False,
      GEOMETRY_FIELDS.get(reader,"array"):{}}

@pytest.mark.parametrize("reader,field",list(GEOMETRY_FIELDS.items()))
def test_additive_geometry_normalizer_preserves_designated_payload(reader,field):
    value=internal(reader);result=service.normalize_result(value)
    assert result.contract_version==2 and result.kind=="geometry" and result.payload[field]=={}
    assert result.payload["view_kind"]=="geometry" and "reader" not in result.payload

@pytest.mark.parametrize("reader,field",list(GEOMETRY_FIELDS.items()))
@pytest.mark.parametrize("bad",[None,[],1,"geometry"])
def test_geometry_requires_reader_specific_dictionary(reader,field,bad):
    value=internal(reader);value[field]=bad
    with pytest.raises(VisualizationWorkerError):service.normalize_result(value)

@pytest.mark.parametrize("reader",["edf","array-window","nexus-window","h5web","scientific-graph","unknown"])
def test_normalizer_never_upgrades_legacy_reader_to_geometry(reader):
    value=internal(reader)
    result=service.normalize_result(value)
    assert result.kind=="array"
    value.pop("array");value["spatial"]={}
    with pytest.raises(VisualizationWorkerError):service.normalize_result(value)

@pytest.mark.parametrize("reader",list(GEOMETRY_FIELDS))
def test_tree_remains_tree_for_new_geometry_readers(reader):
    value=internal(reader,"tree");value.pop(GEOMETRY_FIELDS[reader]);value["tree"]=[]
    assert service.normalize_result(value).kind=="tree"

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind,options",CASES)
async def test_unified_output_envelope_budget_still_applies(environment,plugin,filename,kind,options):
    _,catalog=environment;p=await catalog.require_enabled("owner",plugin)
    result=service.VisualizationResult(plugin_id=plugin,version="a"*64,revision="b"*64,kind="geometry" if kind=="geometry" else "media",
        payload={"data":"x"*(p.limits.max_output_bytes)},metadata={},warnings=[],sampled=False)
    with pytest.raises(service.ScientificPreviewRejected):service.check_output_budget(result,p)

@pytest.mark.parametrize("reader",["radar-window","ugrid-window"])
def test_new_window_profiles_have_independent_existing_budget(reader):
    p=WINDOW_PROFILES[reader]
    assert (p["total"],p["reads"],p["output"])==(8388608,128,2097152)
    assert p["default_kind"]=="tree"
    validate_limits({"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":128},reader)
    for key,value in (("max_read_bytes",1048577),("max_total_bytes",8388609),("max_reads",129)):
        with pytest.raises(VisualizationWorkerError):validate_limits({**{"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":128},key:value},reader)
    assert WINDOW_PROFILES["edf"]["total"]==8388608 and "geometry" not in WINDOW_PROFILES["edf"]["kinds"]
