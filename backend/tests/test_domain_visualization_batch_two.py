"""Second domain batch retains unified identity, version and capability gates."""
import copy
import pytest
from pydantic import ValidationError
from app.application.services import unified_visualization as service
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.visualization import VisualizationPlugin
from test_unified_visualization import environment, invoke

CASES=[("viz-phylogeny","tree.nwk","tree"),("viz-grib-window","field.grib2","image"),("viz-seismic-window","wave.mseed","series")]

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind",CASES)
async def test_disabled_owner_and_stale_version_before_read(environment,plugin,filename,kind):
    storage,catalog=environment; storage.infos["file"].filename=filename
    await catalog.set_state("owner",plugin,False)
    with pytest.raises(VisualizationDisabledError): await invoke(environment,plugin,"preview",kind=kind)
    await catalog.set_state("owner",plugin,True)
    with pytest.raises(FileNotFoundError): await invoke(environment,plugin,"preview",kind=kind,user="foreign")
    with pytest.raises(PreviewVersionChanged): await invoke(environment,plugin,"preview",kind=kind,version="f"*64)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind",CASES[1:])
async def test_client_version_required_for_samples_even_when_host_knows_file_version(environment,plugin,filename,kind):
    storage,_=environment; storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected): await invoke(environment,plugin,"preview",kind=kind)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,options",[("viz-grib-window","field.grb",{"offset":256}),("viz-seismic-window","wave.sac",{"record_offset":16,"record_limit":16})])
async def test_directory_paging_requires_catalog_version(environment,plugin,filename,options):
    storage,_=environment; storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected,match="分页"): await invoke(environment,plugin,"preview",kind="tree",options=options)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin,filename,kind",CASES+[("viz-envi-window","cube.hdr","image")])
@pytest.mark.parametrize("operation",["bytes","prepare","page"])
async def test_no_new_raw_or_sibling_or_job_permission(environment,plugin,filename,kind,operation):
    storage,_=environment; storage.infos["file"].filename=filename
    with pytest.raises(service.ScientificPreviewRejected): await invoke(environment,plugin,operation,kind=kind)
    assert not storage.reads

@pytest.mark.asyncio
@pytest.mark.parametrize("plugin",["viz-phylogeny","viz-envi-window","viz-grib-window","viz-seismic-window"])
async def test_new_manifests_cannot_widen_scope(environment,plugin):
    _,catalog=environment; item=(await catalog.require_enabled("owner",plugin)).model_dump()
    for patch in ({"reader":"binary"},{"permissions":["file:read","file:write"]},{"capabilities":{**item["capabilities"],"shared":True}},{"capabilities":{**item["capabilities"],"operations":["job"]}}):
        with pytest.raises(ValidationError): VisualizationPlugin.model_validate({**copy.deepcopy(item),**patch})
