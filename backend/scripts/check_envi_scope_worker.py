"""Synthetic managed ENVI pair -> actual host range broker -> isolated worker.

Run in an ephemeral backend with the repository mounted read-only at /workspace.
Only a TemporaryDirectory inside that container is written; production Mongo,
MinIO, dataset mounts, preferences and sessions are never accessed.
"""
from __future__ import annotations
import asyncio
import json
import struct
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
import docker
from app.application.services.dataset_file_preview import DatasetFilePreviewRequest, DatasetFilePreviewService
from app.application.services.envi_scope import envi_visualization
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.file_service import FileService
from app.application.services.unified_visualization import VisualizationRequest, unified_visualization
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.core.config import get_settings
from app.domain.models.dataset import DataCenterDataset, DatasetFile, DatasetLocation, DatasetStorageType
from app.domain.models.visualization import VisualizationSnapshot
from app.infrastructure.external.file.datasetfile import DatasetPreviewFileStorage
from app.infrastructure.external.sandbox import window_visualization_worker as gateway
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.external.sandbox.node_health import LOCAL_DEFAULT_NODE_ID
from check_batch_three_scope_worker import Preferences, References, Datasets, observe

PLUGIN="viz-envi-window"
class Runtime:
    def __init__(self):
        path=Path(__file__).resolve().parents[2]/"plugin-host/visualizations/envi-window.json"
        self.snapshot=VisualizationSnapshot(engine="cordis",revision="a"*64,plugins=[json.loads(path.read_text())])
    async def visualization_snapshot(self): return self.snapshot

async def check(folder,image,containers):
    ident="synthetic-envi-"+uuid.uuid4().hex
    root=Path(folder)/ident; root.mkdir()
    header=b"ENVI\nsamples = 4\nlines = 3\nbands = 3\nfile type = ENVI Standard\ndata type = 4\nbyte order = 0\ninterleave = bsq\nwavelength = {400,500,600}\nwavelength units = Nanometers\n"
    raw=struct.pack("<36f",*[b*100+y*10+x for b in range(3) for y in range(3) for x in range(4)])
    objects={"cube.hdr":header,"cube.bin":raw,"unrelated.txt":b"never granted"}
    for name,value in objects.items(): (root/name).write_bytes(value)
    dataset=DataCenterDataset(dataset_id=ident,data_center_id="synthetic",data_center_name="synthetic",name="synthetic",
        files=[DatasetFile(path=name,size=len(value)) for name,value in objects.items()],
        locations=[DatasetLocation(node_id=LOCAL_DEFAULT_NODE_ID,storage_type=DatasetStorageType.MANAGED_UPLOAD,source_path=ident,read_only=True,verified=True)])
    datasets,references,preferences=Datasets(dataset),References(),Preferences()
    catalog=VisualizationCatalogService(Runtime(),preferences)
    previews=DatasetFilePreviewService(datasets=datasets,repository=references,catalog=catalog,
        settings=SimpleNamespace(jwt_secret_key="synthetic-only-not-a-credential",dataset_storage_root=folder,dataset_host_path_allowlist="/no-host-source-granted"))
    files=FileService(DatasetPreviewFileStorage(SimpleNamespace(),previews))
    info=(await previews.prepare(ident,"synthetic-owner",DatasetFilePreviewRequest(path="cube.hdr",plugin_id=PLUGIN))).file
    assert info.size==len(header) and folder not in info.model_dump_json()
    async def request(kind="tree",options=None,version=None,owner="synthetic-owner"):
        return await unified_visualization(files,catalog,image,info.file_id,owner,VisualizationRequest(plugin_id=PLUGIN,operation="preview",kind=kind,options=options or {},version=version))
    checks,negative=[],[]
    with observe(containers):
        tree=await request()
        assert tree.kind=="tree" and tree.metadata["read_bytes"]==len(header) and tree.metadata["read_requests"]==1
        assert tree.metadata["source_bytes"]==len(header)+len(raw)
        checks.append("header-only-exact-pair")
        options={"band":2,"x":1,"y":1,"width":3,"height":2}
        pixels=await request("image",options,tree.version)
        assert pixels.kind=="array" and pixels.payload["array"]["values"]==[211,212,213,221,222,223]
        assert pixels.payload["axes"][0]["values"]==[1,2] and pixels.payload["axes"][1]["values"]==[1,2,3]
        checks.append("true-single-band-roi")
        series=await request("series",{"x":2,"y":1,"band_start":0,"band_count":3},tree.version)
        assert series.payload["array"]["values"]==[12,112,212]
        assert series.payload["axes"][0]=={"label":"光谱坐标","unit":"Nanometers","values":[400,500,600]}
        assert all(value not in series.model_dump_json() for value in (folder,ident,"cube.bin","unrelated"))
        checks.append("true-pixel-spectrum")
        async def reject(label,call,exception=(ValueError,FileNotFoundError)):
            try: await call()
            except exception: negative.append(label)
            else: raise AssertionError(label+" was accepted")
        await reject("client-version-mandatory",lambda:request("image",options))
        await reject("stale-pair-version",lambda:request("image",options,"0"*64),PreviewVersionChanged)
        await reject("foreign-owner",lambda:request(owner="other-owner"))
        await catalog.set_state("synthetic-owner",PLUGIN,False)
        await reject("disabled-plugin",request,VisualizationDisabledError)
        await catalog.set_state("synthetic-owner",PLUGIN,True)
        original=gateway.run_window_visualization_worker
        def changed_worker(*args,**kwargs):
            result=original(*args,**kwargs); (root/"cube.bin").write_bytes(raw+b"x"); return result
        await reject("unread-data-member-final-fence",lambda:envi_visualization(files,catalog,image,info.file_id,"synthetic-owner",VisualizationRequest(plugin_id=PLUGIN,operation="preview",kind="tree"),worker=changed_worker),PreviewVersionChanged)
        (root/"cube.bin").write_bytes(raw)
        datasets.archived=True
        await reject("archived-dataset",request)
    return {"positive":checks,"negative":negative,"business_storage_writes":0}

def main():
    image=get_settings().sandbox_image; assert image
    containers=[]
    try:
        with tempfile.TemporaryDirectory(prefix="dataseek-envi-scope-check-") as folder:
            result=asyncio.run(check(folder,image,containers))
        assert not Path(folder).exists()
    finally:
        client=docker.from_env(timeout=5); remaining=[]
        try:
            for ident in containers:
                try: container=client.containers.get(ident)
                except docker.errors.NotFound: continue
                remaining.append(ident); _remove_worker(container)
        finally: client.close()
        assert not remaining,"Worker required fallback cleanup"
    print(json.dumps({"passed":True,**result,"workers_verified":len(containers),"worker_residue":0,"temporary_sources_removed":True},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
