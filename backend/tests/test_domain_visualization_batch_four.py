"""Batch four additive geometry contract, identity and capability regressions."""
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
 ("viz-dicom-window","synthetic.dcm","image",{"frame":0,"roi":[0,0,1,1],"confirm_deidentified":True}),
 ("viz-spatial-window","synthetic.h5ad","geometry",{"feature":0,"observation_start":0,"observation_count":1,"decode":"raw"}),
 ("viz-pointcloud-window","synthetic.las","geometry",{"point_offset":0,"point_count":1}),
 ("viz-gro-trajectory","synthetic.gro","geometry",{"frame":0}),
 ("viz-simulation-mesh","synthetic.vtu","geometry",{"field":None,"component":0}),
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

GEOMETRY_FIELDS={"spatial-window":"spatial","pointcloud-window":"array","gro-trajectory":"trajectory","simulation-mesh":"mesh"}
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

@pytest.mark.parametrize("reader",["dicom-window","spatial-window","pointcloud-window"])
def test_new_window_profiles_have_independent_existing_budget(reader):
    p=WINDOW_PROFILES[reader]
    assert (p["total"],p["reads"],p["output"])==(8388608,128,2097152)
    assert p["default_kind"]=="tree"
    validate_limits({"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":128},reader)
    for key,value in (("max_read_bytes",1048577),("max_total_bytes",8388609),("max_reads",129)):
        with pytest.raises(VisualizationWorkerError):validate_limits({**{"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":128},key:value},reader)
    assert WINDOW_PROFILES["edf"]["total"]==8388608 and "geometry" not in WINDOW_PROFILES["edf"]["kinds"]

def test_actual_spatial_result_normalizes_geometry_without_losing_ordinals():
    value=json.loads(Path(__file__).with_name("spatial_window_contract_fixtures.json").read_text())["csr"]["geometry"]
    value.update(plugin_id="viz-spatial-window",version="a"*64,revision="b"*64)
    result=service.normalize_result(value)
    assert result.kind=="geometry" and result.payload["spatial"]=={"observations":[1,2],"x":[2,3],"y":[20,30],"values":[0,5]}


def _generic_visits(value):
    """Include dictionary keys, as the production generic complexity gate does."""
    if isinstance(value,dict):
        return 1+sum(_generic_visits(k)+_generic_visits(v) for k,v in value.items())
    if isinstance(value,list):
        return 1+sum(_generic_visits(v) for v in value)
    return 1


@pytest.mark.asyncio
async def test_full_8192_atom_gro_with_velocities_fits_its_additive_budget(environment):
    fixture=Path(__file__).resolve().parents[2]/"frontend/tests/browser/geometry-data.json"
    value=json.loads(fixture.read_text())["gro_geometry"]
    trajectory=value["trajectory"]
    positions=trajectory["positions"]
    velocities=trajectory["velocities"]
    trajectory["positions"]=[positions[i%len(positions)][:] for i in range(8192)]
    trajectory["velocities"]=[velocities[i%len(velocities)][:] for i in range(8192)]
    trajectory["atoms"]=[{"residue_number":1,"residue_name":"SYN","atom_name":f"P{i%1000}","atom_number":i} for i in range(8192)]
    for frame in value["choices"]["frames"]:frame["atoms"]=8192
    value["metadata"]["total_atoms"]=8192*value["metadata"]["frame_count"]
    # A 68-character atom record plus newline in each synthetic velocity frame.
    value["metadata"]["source_bytes"]=69*value["metadata"]["total_atoms"]+200
    assert 100000<_generic_visits(value)<200000
    assert len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())<2097152
    assert extended.validate_payload(value,"gro-trajectory","geometry",2097152) is value
    value.update(plugin_id="viz-gro-trajectory",version="a"*64,revision="b"*64)
    normalized=service.normalize_result(value)
    assert normalized.kind=="geometry" and len(normalized.payload["trajectory"]["velocities"])==8192
    _,catalog=environment
    service.check_output_budget(normalized,await catalog.require_enabled("owner","viz-gro-trajectory"))
    assert len(normalized.model_dump_json().encode())<2097152


def _legacy_envelope(kind):
    return {"contract_version":2,"type":"hdf5","reader":"hdf5","kind":kind,
        "media_type":"application/json","metadata":{},"warnings":[],"sampled":False}


def test_legacy_generic_complexity_exact_boundary_remains_100000():
    value=_legacy_envelope("tree");value["tree"]=[];value["metadata"]["bounded_diagnostics"]=[]
    value["metadata"]["bounded_diagnostics"]=[0]*(100000-_generic_visits(value))
    assert _generic_visits(value)==100000
    assert extended.validate_payload(value,"hdf5","tree",2097152) is value
    value["metadata"]["bounded_diagnostics"].append(0)
    assert _generic_visits(value)==100001
    with pytest.raises(ValueError,match="Preview complexity exceeded"):
        extended.validate_payload(value,"hdf5","tree",2097152)


def test_legacy_numeric_array_exact_boundary_remains_16384():
    value=_legacy_envelope("series");value["array"]={"shape":[16384],"values":[0]*16384}
    assert extended.validate_payload(value,"hdf5","series",2097152) is value
    value["array"]["shape"][0]=16385;value["array"]["values"].append(0)
    assert _generic_visits(value)<100000
    with pytest.raises(ValueError,match="Invalid numeric array"):
        extended.validate_payload(value,"hdf5","series",2097152)
