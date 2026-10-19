from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.services.connector_results import connector_results
from app.interfaces.api.data_connector_routes import ImportSelection, import_selection


def event(connector='ncbi-geo'):
    return ToolEvent(tool_call_id='call-1', tool_name='data_discovery', function_name='data_connector_call',
        function_args={'connector_id':connector, 'operation':'search'}, status=ToolStatus.CALLED,
        function_result={'success':True, 'data':{'resources':[{'external_id':'GSE123', 'title':'Example',
        'source_url':'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE123'}]}})


def test_projection_preserves_actionable_public_fields():
    view=connector_results(event())
    assert view['resources'][0]['source_url'].endswith('?acc=GSE123')
    assert not view['can_download']
    assert connector_results(event().model_copy(update={'status':ToolStatus.CALLING})) is None
    assert connector_results(event().model_copy(update={'function_result':{'success':False}})) is None


@pytest.fixture
def runtime(monkeypatch):
    repository=SimpleNamespace(find_owned_by_id_and_user_id=AsyncMock(return_value=SimpleNamespace(status='completed')),
        get_events=AsyncMock(return_value=[event()]), upsert_connector_file=AsyncMock())
    monkeypatch.setattr('app.infrastructure.repositories.mongo_session_repository.MongoSessionRepository',lambda:repository)
    info=SimpleNamespace(file_id='file1', filename='catalog.csv', size=123, content_type='text/csv', upload_date=None)
    files=SimpleNamespace(upload_file=AsyncMock(return_value=info), download_file=AsyncMock(return_value=(BytesIO(b'csv'),info)))
    monkeypatch.setattr('app.application.services.file_service.FileService',lambda **kwargs:files)
    monkeypatch.setattr('app.infrastructure.external.file.factory.get_file_storage',lambda:None)
    sandbox=SimpleNamespace(file_upload=AsyncMock(return_value=SimpleNamespace(success=True)))
    agent=SimpleNamespace(ensure_interactive_sandbox=AsyncMock(return_value=(None,sandbox)))
    return repository,files,sandbox,agent


@pytest.mark.asyncio
async def test_import_catalog_and_only_confirm_sandbox_success(runtime):
    repo,files,sandbox,agent=runtime
    request=ImportSelection(tool_call_id='call-1',resource_ids=['GSE123'],mode='catalog')
    result=await import_selection(request,('owner','session'),agent,None)
    assert result.data['files'][0]['file_id']=='file1'
    assert sandbox.file_upload.call_args.args[1]=='/home/ubuntu/upload/catalog.csv'
    assert repo.upsert_connector_file.await_count==1
    sandbox.file_upload.return_value.success=False
    result=await import_selection(request,('owner','session'),agent,None)
    assert result.data['files']==[] and result.data['failures']
    assert repo.upsert_connector_file.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,ids', [('download',['GSE123']),('catalog',['GSE999'])])
async def test_reject_unsupported_download_or_forged_selection(runtime,mode,ids):
    repo,files,sandbox,agent=runtime
    with pytest.raises(HTTPException) as err:
        await import_selection(ImportSelection(tool_call_id='call-1',resource_ids=ids,mode=mode),('owner','session'),agent,None)
    assert err.value.status_code==400
    agent.ensure_interactive_sandbox.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('session,code', [(None,403),(SimpleNamespace(status='running'),409)])
async def test_reject_nonowner_and_running_task(runtime,session,code):
    repo,files,sandbox,agent=runtime
    repo.find_owned_by_id_and_user_id.return_value=session
    with pytest.raises(HTTPException) as err:
        await import_selection(ImportSelection(tool_call_id='call-1',resource_ids=['GSE123'],mode='catalog'),('owner','session'),agent,None)
    assert err.value.status_code==code
    agent.ensure_interactive_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_dispatches_saved_identifier_and_imports_archive(runtime):
    repo,files,sandbox,agent=runtime
    saved=event('ncbi-datasets-v2')
    saved.function_result['data']['resources'][0].update(external_id='GCF_000009045.1',
        source_url='https://www.ncbi.nlm.nih.gov/datasets/genome/GCF_000009045.1/')
    repo.get_events.return_value=[saved]
    info=files.upload_file.return_value
    info.filename='GCF_000009045.1_genomic.zip'
    service=SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(files=[info])))
    result=await import_selection(ImportSelection(tool_call_id='call-1',resource_ids=['GCF_000009045.1'],mode='download'),
        ('owner','session'),agent,service)
    service.call.assert_awaited_once_with('owner','session','ncbi-datasets-v2','acquire',{'accession':'GCF_000009045.1'})
    assert result.data['files'][0]['filename'].endswith('.zip')
    assert sandbox.file_upload.call_args.args[1].endswith('.zip')
