from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.application.errors.exceptions import NotFoundError
from app.domain.models.model_trace import ModelTraceView
from app.infrastructure.repositories.mongo_model_trace_repository import get_model_trace_repository
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.schemas.base import APIResponse

router = APIRouter(prefix="/sessions/{session_id}/model-traces", tags=["model-traces"])


class ModelTraceList(BaseModel):
    traces: list[ModelTraceView]
    next_cursor: str | None = None


@router.get("", response_model=APIResponse[ModelTraceList])
async def list_model_traces(session_id: str, task_id: str | None = Query(default=None, max_length=128),
                            limit: int = Query(default=100, ge=1, le=200),
                            before: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
                            user=Depends(get_current_user), agents=Depends(get_agent_service),
                            repository=Depends(get_model_trace_repository)):
    session = await agents.get_session(session_id, user.id)
    if session is None:
        raise NotFoundError("Session not found")
    try:
        if before is None:
            traces = await repository.list_for_owner(
                session.user_id,
                session_id,
                task_id=task_id,
                limit=limit,
            )
        else:
            traces = await repository.list_for_owner(
                session.user_id,
                session_id,
                task_id=task_id,
                limit=limit,
                before=before,
            )
    except Exception:
        raise HTTPException(status_code=503, detail="Model execution trace is unavailable") from None
    next_cursor = traces[-1].trace_id if len(traces) == limit else None
    return APIResponse.success(ModelTraceList(traces=traces, next_cursor=next_cursor))
