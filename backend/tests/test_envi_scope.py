"""ENVI two-file capability security; isolated fake dataset storage only."""
import asyncio
from types import SimpleNamespace
import pytest
from app.application.services import envi_scope as module
from app.application.services import ome_zarr_scope as base
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.unified_visualization import VisualizationRequest
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.dataset import DatasetFile, DatasetStorageType
from test_dataset_file_preview import env, prepare

PLUGIN="viz-envi-window"

@pytest.fixture(autouse=True)
def slots(monkeypatch):
    monkeypatch.setattr(base,"_SCOPE_SLOTS",asyncio.Semaphore(2))

def configure(env, host=False, header="folder/cube.hdr", data="folder/cube.bin"):
    location=env.datasets.dataset.locations[0]; prefix=""
    if host:
        location.storage_type=DatasetStorageType.HOST_PATH; location.source_path="/allowed/private-source"; location.mount_name="data"
        prefix=f"sources/{location.location_id}/data/"
    objects={header:b"ENVI synthetic header",data:b"binary-samples", "other/cube.bin":b"secret", "folder/private.txt":b"secret"}
    env.datasets.dataset.files=[DatasetFile(path=prefix+path,size=len(value)) for path,value in objects.items()]
    env.state.update(objects=objects,versions={path:1 for path in objects},on_data=None)
    def reader(source, relative, *, offset, length, max_bytes):
        env.state["reads"].append((source,relative,offset,length,max_bytes)); assert length is not None
        if length and env.state["on_data"]: env.state["on_data"]()
        return objects[relative][offset:offset+length],{"size":len(objects[relative]),"mtime_ns":env.state["versions"][relative],"ctime_ns":1,"inode":list(objects).index(relative)+1,"device":1}
    env.service.reader=reader
    return prefix

async def scope_for(env, **kwargs):
    configure(env,**kwargs)
    info=(await prepare(env,path=kwargs.get("header","folder/cube.hdr"),plugin=PLUGIN)).file
    env.state["reads"].clear()
    scope=await module.EnviDatasetScope(env.service,info.file_id,"owner").initialize()
    return scope

@pytest.mark.asyncio
@pytest.mark.parametrize("host",[False,True])
async def test_exact_pair_stat_only_no_names_or_paths_in_public_state(env,host):
    scope=await scope_for(env,host=host)
    assert [r["key"] for r in scope.resources] == ["header","data"]
    assert all(r[3]==0 for r in env.state["reads"])
    assert {r[1] for r in env.state["reads"]} == {"folder/cube.hdr","folder/cube.bin"}
    public=scope.info.model_dump_json()
    for secret in ("private-source","/allowed","folder/","sources/","cube.bin","private.txt"):
        assert secret not in public
    assert scope.info.file_path is None and scope.info.file_url is None

@pytest.mark.asyncio
@pytest.mark.parametrize("header,data",[("cube.img.hdr","cube.img"),("folder/cube.hdr","folder/cube"),("folder/cube.hdr","folder/cube.bip"),("folder/cube.HDR","folder/cube.BIN")])
async def test_declared_append_replace_and_extensionless_pairs(env,header,data):
    scope=await scope_for(env,header=header,data=data)
    assert scope.entries[1]["path"] == data

@pytest.mark.asyncio
async def test_ambiguous_or_missing_pair_fails_before_any_stat(env):
    configure(env); info=(await prepare(env,path="folder/cube.hdr",plugin=PLUGIN)).file
    for change in ("extra","missing"):
        env.state["reads"].clear()
        if change=="extra": env.datasets.dataset.files.append(DatasetFile(path="folder/cube.img",size=1))
        else: env.datasets.dataset.files=[f for f in env.datasets.dataset.files if f.path not in {"folder/cube.bin","folder/cube.img"}]
        with pytest.raises(ScientificPreviewRejected): await module.EnviDatasetScope(env.service,info.file_id,"owner").initialize()
        assert not env.state["reads"]

@pytest.mark.asyncio
async def test_range_cannot_cross_members_or_read_undeclared_siblings(env):
    scope=await scope_for(env); env.state["reads"].clear(); data=scope.resources[1]
    value,info=await scope.download_file_range(scope.source_id,"owner",offset=data["offset"]+1,length=3)
    assert value==b"ina" and info is scope.info
    assert len(env.state["reads"])==1 and env.state["reads"][0][1]=="folder/cube.bin"
    for offset,length in [(data["offset"]-1,2),(scope.info.size,1),(-1,1),(True,1),(0,1024**2+1)]:
        with pytest.raises(ScientificPreviewRejected): await scope.download_file_range(scope.source_id,"owner",offset=offset,length=length)
    assert len(env.state["reads"])==1

@pytest.mark.asyncio
@pytest.mark.parametrize("member",["folder/cube.hdr","folder/cube.bin"])
async def test_version_includes_both_members_even_if_selected_window_did_not_read_it(env,member):
    scope=await scope_for(env); original=preview_version(scope.info)
    env.state["versions"][member]+=1
    with pytest.raises(PreviewVersionChanged): await scope.verify_snapshot()
    newer=await module.EnviDatasetScope(env.service,scope.source_id,"owner").initialize()
    assert preview_version(newer.info)!=original

@pytest.mark.asyncio
async def test_owner_archival_and_declaration_changes_revoke_scope(env):
    scope=await scope_for(env)
    with pytest.raises(FileNotFoundError): await scope.download_file_range(scope.source_id,"foreign",offset=0,length=1)
    env.datasets.archived=True
    with pytest.raises(FileNotFoundError): await scope.check_live()
    env.datasets.archived=False; env.datasets.dataset.files[1].size+=1
    with pytest.raises(PreviewVersionChanged): await scope.check_live()

@pytest.mark.asyncio
async def test_plugin_off_and_unapproved_parameters_never_establish_scope(env):
    configure(env); info=(await prepare(env,path="folder/cube.hdr",plugin=PLUGIN)).file
    storage=SimpleNamespace(_file_storage=SimpleNamespace(previews=env.service))
    env.state["reads"].clear()
    await env.catalog.set_state("owner",PLUGIN,False)
    with pytest.raises(VisualizationDisabledError): await module.envi_visualization(storage,env.catalog,"image",info.file_id,"owner",VisualizationRequest(plugin_id=PLUGIN,operation="preview"))
    await env.catalog.set_state("owner",PLUGIN,True)
    for request in (VisualizationRequest(plugin_id=PLUGIN,operation="preview",kind="tree",options={"resource_id":"foreign"}),
                    VisualizationRequest(plugin_id=PLUGIN,operation="preview",kind="image",options={"band":0,"x":0,"y":0,"width":1,"height":1})):
        with pytest.raises(ScientificPreviewRejected): await module.envi_visualization(storage,env.catalog,"image",info.file_id,"owner",request)
    assert not env.state["reads"]

@pytest.mark.asyncio
async def test_standalone_uploaded_header_does_not_acquire_any_related_file(env):
    configure(env); env.state["reads"].clear()
    with pytest.raises(ScientificPreviewRejected,match="数据集"):
        await module.envi_visualization(SimpleNamespace(),env.catalog,"image","opaque-upload","owner",VisualizationRequest(plugin_id=PLUGIN,operation="preview"))
    assert not env.state["reads"]

@pytest.mark.asyncio
async def test_mid_read_member_change_is_rejected(env):
    scope=await scope_for(env)
    env.state["on_data"] = lambda: env.state["versions"].__setitem__("folder/cube.hdr",2)
    with pytest.raises(PreviewVersionChanged): await scope.download_file_range(scope.source_id,"owner",offset=0,length=1)

@pytest.mark.asyncio
async def test_scope_final_fence_is_passed_to_existing_window_service(env,monkeypatch):
    configure(env); info=(await prepare(env,path="folder/cube.hdr",plugin=PLUGIN)).file
    async def window(scope,catalog,image,file_id,owner,request,**kwargs):
        assert kwargs["resources"]==scope.resources
        env.state["versions"]["folder/cube.bin"]+=1
        await kwargs["final_fence"]()
    monkeypatch.setattr(module,"window_visualization",window)
    with pytest.raises(PreviewVersionChanged):
        await module.envi_visualization(SimpleNamespace(_file_storage=SimpleNamespace(previews=env.service)),env.catalog,"image",info.file_id,"owner",VisualizationRequest(plugin_id=PLUGIN,operation="preview"))
