from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.application.errors.exceptions import NotFoundError
from app.application.services.agent_service import AgentService
from app.domain.models.tool_approval import ToolApprovalView
from app.domain.models.user import User
from app.domain.services.tool_approval_service import (
    ToolApprovalConflictError,
    ToolApprovalNotFoundError,
    ToolApprovalService,
)
from app.infrastructure.external.tool_approval_factory import get_tool_approval_service
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.schemas.base import APIResponse


router = APIRouter(
    prefix="/sessions/{session_id}/tool-approvals",
    tags=["tool-approvals"],
)
ApprovalId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class ToolApprovalList(BaseModel):
    approvals: list[ToolApprovalView]


class ToolApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    expected_revision: int = Field(strict=True, ge=1)


async def _owner(
    service: AgentService,
    session_id: str,
    user: User,
    *,
    write: bool = False,
) -> str:
    session = await service.get_session(session_id, user.id)
    if session is None or (write and session.user_id != user.id):
        raise NotFoundError("Session not found")
    return session.user_id


@router.get("", response_model=APIResponse[ToolApprovalList])
async def list_tool_approvals(
    session_id: str,
    limit: int = Query(default=100, ge=1, le=100),
    user: User = Depends(get_current_user),
    agents: AgentService = Depends(get_agent_service),
    approvals: ToolApprovalService = Depends(get_tool_approval_service),
):
    owner = await _owner(agents, session_id, user)
    records = await approvals.list_for_owner(owner, session_id, limit=limit)
    return APIResponse.success(
        ToolApprovalList(approvals=[record.public_view() for record in records])
    )


@router.get("/{approval_id}", response_model=APIResponse[ToolApprovalView])
async def get_tool_approval(
    session_id: str,
    approval_id: ApprovalId,
    user: User = Depends(get_current_user),
    agents: AgentService = Depends(get_agent_service),
    approvals: ToolApprovalService = Depends(get_tool_approval_service),
):
    owner = await _owner(agents, session_id, user)
    record = await approvals.get_for_owner(owner, session_id, approval_id)
    if record is None:
        raise NotFoundError("Tool approval not found")
    return APIResponse.success(record.public_view())


@router.post(
    "/{approval_id}/decision",
    response_model=APIResponse[ToolApprovalView],
)
async def decide_tool_approval(
    session_id: str,
    approval_id: ApprovalId,
    request: ToolApprovalDecisionRequest,
    x_tool_approval_action: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    agents: AgentService = Depends(get_agent_service),
    approvals: ToolApprovalService = Depends(get_tool_approval_service),
):
    if x_tool_approval_action != "decide":
        raise HTTPException(status_code=403, detail="Tool approval decision rejected")
    owner = await _owner(agents, session_id, user, write=True)
    try:
        record = await approvals.decide(
            owner,
            session_id,
            approval_id,
            request.decision,
            request.expected_revision,
        )
    except ToolApprovalNotFoundError:
        raise NotFoundError("Tool approval not found") from None
    except ToolApprovalConflictError:
        raise HTTPException(
            status_code=409,
            detail="Tool approval decision is no longer available",
        ) from None
    return APIResponse.success(record.public_view())


__all__ = ["router"]
