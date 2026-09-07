from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel

from app.application.errors.exceptions import NotFoundError
from app.application.services.agent_service import AgentService
from app.domain.models.analysis_job import AnalysisJobView
from app.domain.models.user import User
from app.domain.services.analysis_job_service import AnalysisJobService
from app.infrastructure.external.analysis_job_factory import get_analysis_job_service
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.schemas.base import APIResponse


router = APIRouter(prefix="/sessions/{session_id}/analysis-jobs", tags=["analysis-jobs"])
JobId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class AnalysisJobList(BaseModel):
    jobs: list[AnalysisJobView]


async def _owner(service: AgentService, session_id: str, user: User, *, write=False) -> str:
    session = await service.get_session(session_id, user.id)
    if session is None or (write and session.user_id != user.id):
        raise NotFoundError("Session not found")
    return session.user_id


@router.get("", response_model=APIResponse[AnalysisJobList])
async def list_analysis_jobs(
    session_id: str,
    limit: int = Query(default=100, ge=1, le=100),
    user: User = Depends(get_current_user),
    agents: AgentService = Depends(get_agent_service),
    jobs: AnalysisJobService = Depends(get_analysis_job_service),
):
    owner = await _owner(agents, session_id, user)
    records = await jobs.list_for_owner(owner, session_id, limit=limit)
    return APIResponse.success(AnalysisJobList(jobs=[record.public_view() for record in records]))


@router.get("/{job_id}", response_model=APIResponse[AnalysisJobView])
async def get_analysis_job(
    session_id: str, job_id: JobId,
    user: User = Depends(get_current_user),
    agents: AgentService = Depends(get_agent_service),
    jobs: AnalysisJobService = Depends(get_analysis_job_service),
):
    owner = await _owner(agents, session_id, user)
    record = await jobs.get_for_owner(owner, session_id, job_id)
    if record is None:
        raise NotFoundError("Analysis job not found")
    return APIResponse.success(record.public_view())


@router.post("/{job_id}/cancel", response_model=APIResponse[AnalysisJobView])
async def cancel_analysis_job(
    session_id: str, job_id: JobId,
    x_analysis_job_action: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    agents: AgentService = Depends(get_agent_service),
    jobs: AnalysisJobService = Depends(get_analysis_job_service),
):
    # A form or cross-site simple request must not cancel a local analysis.
    if x_analysis_job_action != "cancel":
        raise HTTPException(status_code=403, detail="Analysis job cancellation rejected")
    owner = await _owner(agents, session_id, user, write=True)
    try:
        record = await jobs.request_cancel(owner, session_id, job_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="This analysis job cannot be cancelled") from None
    if record is None:
        raise NotFoundError("Analysis job not found")
    return APIResponse.success(record.public_view())
