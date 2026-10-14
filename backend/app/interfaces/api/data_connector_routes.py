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
    except SourceConnectorError:
        raise UpstreamServiceError('数据源请求未完成，请稍后重试或在对话中确认检索条件') from None
