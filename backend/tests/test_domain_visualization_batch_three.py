"""Batch three identity, explicit selection and capability regression."""
import copy
import pytest
from pydantic import ValidationError
from app.application.services import unified_visualization as service
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.visualization import VisualizationPlugin
from test_unified_visualization import environment, invoke

CASES=[("viz-mass-spectrum","sample.mgf","series"),("viz-fcs-window","sample.fcs","series"),
       ("viz-diffraction","sample.xrdml","series")]

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind",CASES)
async def test_disabled_owner_and_stale_version_before_read(environment,plugin,filename,kind):
    storage,catalog=environment;storage.infos["file"].filename=filename
    await catalog.set_state("owner",plugin,False)
    with pytest.raises(VisualizationDisabledError):await invoke(environment,plugin,"preview",kind=kind)
    await catalog.set_state("owner",plugin,True)
    with pytest.raises(FileNotFoundError):await invoke(environment,plugin,"preview",kind=kind,user="foreign")
    with pytest.raises(PreviewVersionChanged):await invoke(environment,plugin,"preview",kind=kind,version="f"*64)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind",CASES)
async def test_samples_require_client_version_not_implicit_host_version(environment,plugin,filename,kind):
    storage,_=environment;storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected):await invoke(environment,plugin,"preview",kind=kind)
    assert not storage.reads

@pytest.mark.asyncio
async def test_mass_catalog_pages_require_snapshot(environment):
    storage,_=environment;storage.infos["file"].filename="sample.mzml"
    with pytest.raises(service.ScientificPreviewRejected):
        await invoke(environment,"viz-mass-spectrum","preview",kind="tree",options={"offset":64})
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind",CASES+[("viz-ripple-window","cube.rpl","image")])
@pytest.mark.parametrize("operation",["bytes","prepare","page"])
async def test_no_new_raw_or_job_permission(environment,plugin,filename,kind,operation):
    storage,_=environment;storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected):await invoke(environment,plugin,operation,kind=kind)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin",[c[0] for c in CASES]+["viz-ripple-window"])
async def test_manifests_cannot_widen_scope(environment,plugin):
    _,catalog=environment;item=(await catalog.require_enabled("owner",plugin)).model_dump()
    for patch in ({"reader":"binary"},{"permissions":["file:read","file:write"]},
                  {"capabilities":{**item["capabilities"],"shared":True}},
                  {"capabilities":{**item["capabilities"],"operations":["job"]}}):
        with pytest.raises(ValidationError):VisualizationPlugin.model_validate({**copy.deepcopy(item),**patch})
