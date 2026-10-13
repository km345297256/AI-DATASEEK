"""Session-owned provenance: no network fetching, host paths, or claimed verification."""
from fastapi import APIRouter, Depends, Query, Path
from app.application.errors.exceptions import NotFoundError
from app.application.services.data_candidate_service import DataCandidateService
from app.domain.models.data_candidate import CandidateInput, CandidatePage, DataCandidate, DataVersion, VersionInput, VersionPage
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.repositories.mongo_data_candidate_repository import MongoDataCandidateRepository
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.schemas.base import APIResponse

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
