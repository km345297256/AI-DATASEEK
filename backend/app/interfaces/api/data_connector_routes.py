"""Connector catalog and session-owned standard invocation."""
from fastapi import APIRouter, Depends
from app.interfaces.schemas.base import APIResponse
from app.interfaces.dependencies import get_current_user
from app.interfaces.api.data_candidate_routes import owned_session
from app.application.errors.exceptions import UpstreamServiceError
from app.application.services.data_connector_service import get_data_connector_service
from app.domain.models.data_connector import ConnectorCall, ConnectorManifest, ConnectorResult
from app.domain.external.data_source import SourceConnectorError

router = APIRouter(tags=['data-connectors'])


@router.get('/data-connectors', response_model=APIResponse[list[ConnectorManifest]])
async def catalog(user=Depends(get_current_user), service=Depends(get_data_connector_service)):
    return APIResponse.success(service.catalog())


@router.post('/sessions/{session_id}/data-connectors/{connector_id}/call', response_model=APIResponse[ConnectorResult])
async def call(connector_id: str, request: ConnectorCall, scope=Depends(owned_session), service=Depends(get_data_connector_service)):
    try:
        return APIResponse.success(await service.call(*scope, connector_id, request.operation, request.arguments))
    except SourceConnectorError as error:
        from app.domain.services.tools.data_discovery import connector_error_message
        raise UpstreamServiceError(connector_error_message(error.reason)) from None


from pydantic import BaseModel, Field
from typing import Literal
from fastapi import HTTPException
from app.interfaces.dependencies import get_agent_service


class ImportSelection(BaseModel):
    tool_call_id: str = Field(min_length=1, max_length=200)
    resource_ids: list[str] = Field(min_length=1, max_length=20)
    mode: Literal['download', 'catalog']
    file_ids: list[str] = Field(default_factory=list, max_length=1)


@router.post('/sessions/{session_id}/data-connectors/import')
async def import_selection(request: ImportSelection, scope=Depends(owned_session),
                           agent=Depends(get_agent_service), service=Depends(get_data_connector_service)):
    """Import only identifiers present in this owner's persisted successful search."""
    from io import BytesIO, StringIO
    import csv
    import hashlib
    from app.domain.services.connector_results import connector_results
    from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
    from app.application.services.file_service import FileService
    from app.infrastructure.external.file.factory import get_file_storage
    repository = MongoSessionRepository()
    owner, session_id = scope
    session = await repository.find_owned_by_id_and_user_id(session_id, owner)
    if session is None:
        raise HTTPException(403, '仅任务所有者可导入数据')
    if session.status == 'running':
        raise HTTPException(409, '请等待当前任务结束后导入，避免与正在执行的分析冲突')
    panel, selected = await selected_results(repository, session_id, request)
    if request.mode == 'download' and not panel['can_download']:
        raise HTTPException(400, '此连接器尚未提供原始数据下载，可导入检索清单')
    if request.mode == 'download' and len(selected) > 3:
        raise HTTPException(400, '每次最多下载 3 个数据包，请分批导入')
    if request.mode == 'download' and panel['connector_id'] in ('ncbi-geo', 'ncbi-sra') and (len(selected) != 1 or not request.file_ids):
        raise HTTPException(400, '请先查看文件列表，再逐文件下载')
    _, sandbox = await agent.ensure_interactive_sandbox(session_id, owner)
    files = FileService(file_storage=get_file_storage())
    receipts, failures = [], []
    if request.mode == 'catalog':
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=['external_id', 'title', 'source_url'])
        writer.writeheader()
        # Spreadsheet formula prefixes are escaped in a metadata export.
        writer.writerows({k: "'" + v if v.startswith(('=', '+', '-', '@')) else v for k, v in row.items()} for row in selected)
        digest = hashlib.sha256(output.getvalue().encode()).hexdigest()[:12]
        info = await files.upload_file(BytesIO(output.getvalue().encode('utf-8-sig')),
            f"{panel['connector_id']}-{digest}-catalog.csv", owner, 'text/csv',
            metadata={'source': 'connector_selection', 'connector_id': panel['connector_id']})
        pending = [(info.file_id, info.filename)]
    else:
        pending = []
        for row in selected:
            try:
                args = {'accession': row['external_id']}
                if panel['connector_id'] in ('ncbi-geo', 'ncbi-sra'):
                    if len(selected) != 1 or not request.file_ids:
                        raise HTTPException(400, '请先查看文件列表，再逐文件下载')
                    args['file_ids'] = request.file_ids
                result = await service.call(owner, session_id, panel['connector_id'], 'acquire', args)
                pending.extend((file.file_id, file.filename) for file in result.files)
            except SourceConnectorError as error:
                from app.domain.services.tools.data_discovery import connector_error_message
                message = ('文件超过 64 MiB 上限，请选择较小文件。' if error.reason == 'download_too_large' and panel['connector_id'] != 'ncbi-datasets-v2' else connector_error_message(error.reason))
                failures.append({'id': row['external_id'], 'message': message})
    for file_id, filename in pending:
        stream, info = await files.download_file(file_id, owner)
        try:
            path = '/home/ubuntu/upload/' + filename
            result = await sandbox.file_upload(stream, path, filename)
            if not result.success:
                failures.append({'id': filename, 'message': '文件已下载，但导入沙盒失败，可重试'})
                continue
            info.file_path = path
            await repository.upsert_connector_file(session_id, owner, info)
            receipts.append({'file_id': file_id, 'filename': filename, 'size': info.size, 'content_type': info.content_type, 'upload_date': info.upload_date.isoformat() if info.upload_date else ''})
        finally:
            stream.close()
    return APIResponse.success({'files': receipts, 'failures': failures})


async def selected_results(repository, session_id, request):
    from app.domain.services.connector_results import connector_results
    events = await repository.get_events(session_id)
    panel = next((connector_results(event) for event in reversed(events)
                  if getattr(event, 'tool_call_id', None) == request.tool_call_id
                  and connector_results(event) is not None), None)
    if panel is None:
        raise HTTPException(400, '检索记录不可用，请重新检索')
    selected = [row for row in panel['resources'] if row['external_id'] in request.resource_ids]
    if set(request.resource_ids) != {row['external_id'] for row in selected}:
        raise HTTPException(400, '所选数据不属于本次检索结果')
    return panel, selected


@router.post('/sessions/{session_id}/data-connectors/download-options')
async def download_options(request: ImportSelection, scope=Depends(owned_session), service=Depends(get_data_connector_service)):
    from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
    from app.domain.services.tools.data_discovery import connector_error_message
    owner, session_id = scope
    repository = MongoSessionRepository()
    if await repository.find_owned_by_id_and_user_id(session_id, owner) is None:
        raise HTTPException(403, '仅任务所有者可获取下载选项')
    panel, selected = await selected_results(repository, session_id, request)
    if panel['connector_id'] not in ('ncbi-geo', 'ncbi-sra'):
        raise HTTPException(400, '此数据源可直接下载数据包')
    if len(selected) > 3:
        raise HTTPException(400, '每次最多查看 3 条数据的文件')
    options, errors = [], []
    for row in selected:
        try:
            result = await service.call(owner, session_id, panel['connector_id'], 'list_files', {'accession': row['external_id']})
            options.extend({'id': file.id, 'accession': file.external_id, 'filename': file.title,
                'source_url': file.source_url, **file.metadata} for file in result.resources)
            if not result.resources:
                errors.append(row['external_id'] + '：源站暂无公开可下载文件；SRA 可能尚未镜像到 ENA。')
        except SourceConnectorError as error:
            errors.append(row['external_id'] + '：' + connector_error_message(error.reason))
    return APIResponse.success({'options': options, 'errors': errors})
