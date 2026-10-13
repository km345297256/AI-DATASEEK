"""Session-owned provenance and fixed-origin NCBI metadata discovery."""
from fastapi import APIRouter, Depends, Query, Path
from app.application.errors.exceptions import NotFoundError
from app.application.services.data_candidate_service import DataCandidateService
from app.domain.models.data_candidate import CandidateInput, CandidatePage, DataCandidate, DataVersion, VersionInput, VersionPage
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.repositories.mongo_data_candidate_repository import MongoDataCandidateRepository
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.schemas.base import APIResponse

# Connector-authored evidence is only created through these server-side calls.
from app.application.services.ncbi_discovery_service import get_ncbi_discovery_service
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.data_candidate import DiscoveryPage
from app.domain.models.source_connector import AcquisitionPlan, Accession, NcbiSearch, SourceRecord
from app.application.errors.exceptions import UpstreamServiceError


class NcbiDescribeRequest(SourceRecord):
    accession: Accession


router = APIRouter(prefix="/sessions/{session_id}/data-candidates", tags=["data-provenance"])


async def owned_session(session_id: str, user=Depends(get_current_user), agent=Depends(get_agent_service)):
    if not await agent.get_session(session_id, user.id):
        raise NotFoundError("会话不存在")
    return user.id, session_id


def get_candidate_service():
    return DataCandidateService(MongoDataCandidateRepository(), get_file_storage())


@router.get("", response_model=APIResponse[CandidatePage])
async def list_candidates(cursor: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                          scope=Depends(owned_session), service=Depends(get_candidate_service)):
    return APIResponse.success(await service.list_candidates(*scope, cursor))


@router.post("", response_model=APIResponse[DataCandidate])
async def create_candidate(request: CandidateInput, scope=Depends(owned_session), service=Depends(get_candidate_service)):
    return APIResponse.success(await service.create(*scope, request))


@router.get("/{candidate_id}/versions", response_model=APIResponse[VersionPage])
async def list_versions(candidate_id: str = Path(pattern=r"^[a-f0-9]{64}$"),
                        cursor: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
                        scope=Depends(owned_session), service=Depends(get_candidate_service)):
    return APIResponse.success(await service.list_versions(*scope, candidate_id, cursor))


@router.post("/{candidate_id}/versions", response_model=APIResponse[DataVersion])
async def register_version(request: VersionInput, candidate_id: str = Path(pattern=r"^[a-f0-9]{64}$"),
                           scope=Depends(owned_session), service=Depends(get_candidate_service)):
    return APIResponse.success(await service.register(*scope, candidate_id, request))


@router.post('/ncbi/search', response_model=APIResponse[DiscoveryPage])
async def search_ncbi(request: NcbiSearch, scope=Depends(owned_session), service=Depends(get_ncbi_discovery_service)):
    try:
        return APIResponse.success(await service.search(*scope, request))
    except SourceConnectorError as error:
        raise UpstreamServiceError('NCBI 检索未完成：' + error.reason) from None


@router.post('/ncbi/describe', response_model=APIResponse[DataCandidate])
async def describe_ncbi(request: NcbiDescribeRequest, scope=Depends(owned_session), service=Depends(get_ncbi_discovery_service)):
    try:
        return APIResponse.success(await service.describe(*scope, request.accession))
    except SourceConnectorError as error:
        raise UpstreamServiceError('NCBI 元数据读取未完成：' + error.reason) from None


@router.get('/{candidate_id}/acquisition-plan', response_model=APIResponse[AcquisitionPlan])
async def plan_ncbi_acquisition(candidate_id: str = Path(pattern=r'^[a-f0-9]{64}$'),
                                scope=Depends(owned_session), service=Depends(get_ncbi_discovery_service)):
    return APIResponse.success(await service.plan_acquisition(*scope, candidate_id))
