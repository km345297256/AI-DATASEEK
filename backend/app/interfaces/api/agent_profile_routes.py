from fastapi import APIRouter, Depends
from typing import List
from app.domain.models.audit import AuditRiskLevel
from app.domain.models.user import User
from app.application.errors.exceptions import BadRequestError, UnauthorizedError
from app.application.services.agent_profile_service import AgentProfileService
from app.domain.services.audit_service import AuditService, get_audit_service
from app.domain.services.permission_service import PermissionService, get_permission_service
from app.interfaces.dependencies import get_current_user, get_agent_profile_service
from app.interfaces.schemas.agent_profile import (
    CreateAgentProfileRequest,
    UpdateAgentProfileRequest,
    AgentProfileResponse,
)
from app.interfaces.schemas.base import APIResponse
from app.infrastructure.models.documents import ModelConfigurationDocument
from app.domain.models.model_configuration import ModelType

router = APIRouter(prefix="/agent-profiles", tags=["agent-profiles"])


def _ensure_admin(user: User) -> None:
    if user.role != "admin":
        raise UnauthorizedError("Only admins can manage agent profiles")


def _response(profile) -> AgentProfileResponse:
    data = profile.model_dump()
    data["api_key"] = None
    return AgentProfileResponse.model_validate(data)


async def _ensure_model_config(model_config_id: str | None) -> None:
    if not model_config_id:
        return
    doc = await ModelConfigurationDocument.find_one(
        ModelConfigurationDocument.model_config_id == model_config_id
    )
    if not doc or not doc.enabled:
        raise BadRequestError("Selected model configuration is unavailable")
    if doc.model_type not in {ModelType.CHAT, ModelType.VISION}:
        raise BadRequestError("Agent and SubAgent require a chat or vision model configuration")


async def _ensure_profile_models(request) -> None:
    await _ensure_model_config(request.model_config_id)
    for subagent in request.subagents or []:
        await _ensure_model_config(subagent.model_config_id)


@router.post("", response_model=APIResponse[AgentProfileResponse])
async def create_profile(
    request: CreateAgentProfileRequest,
    current_user: User = Depends(get_current_user),
    service: AgentProfileService = Depends(get_agent_profile_service),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[AgentProfileResponse]:
    _ensure_admin(current_user)
    await _ensure_profile_models(request)
    workspace_id = await permission_service.default_workspace_id(current_user)
    profile = await service.create_profile(
        user_id=current_user.id,
        user_role=current_user.role,
        workspace_id=workspace_id,
        name=request.name,
        model_config_id=request.model_config_id,
        model_name=request.model_name,
        model_provider=request.model_provider,
        api_base=request.api_base,
        api_key=request.api_key,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        system_prompt=request.system_prompt,
        planner_config=request.planner_config,
        subagents=request.subagents,
        is_global=request.is_global,
    )
    await audit_service.record(
        actor_user_id=current_user.id,
        action="agent_profile.create",
        resource_type="agent_profile",
        resource_id=profile.id,
        workspace_id=profile.workspace_id,
        risk_level=AuditRiskLevel.HIGH if profile.api_key else AuditRiskLevel.MEDIUM,
        metadata={"name": profile.name, "scope": profile.scope, "model_provider": profile.model_provider, "model_name": profile.model_name},
    )
    return APIResponse.success(_response(profile))


@router.get("", response_model=APIResponse[List[AgentProfileResponse]])
async def list_profiles(
    current_user: User = Depends(get_current_user),
    service: AgentProfileService = Depends(get_agent_profile_service),
) -> APIResponse[List[AgentProfileResponse]]:
    profiles = await service.list_profiles(current_user.id)
    return APIResponse.success([_response(p) for p in profiles])


@router.put("/{profile_id}", response_model=APIResponse[AgentProfileResponse])
async def update_profile(
    profile_id: str,
    request: UpdateAgentProfileRequest,
    current_user: User = Depends(get_current_user),
    service: AgentProfileService = Depends(get_agent_profile_service),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[AgentProfileResponse]:
    _ensure_admin(current_user)
    await _ensure_profile_models(request)
    profile = await service.update_profile(
        user_id=current_user.id,
        user_role=current_user.role,
        workspace_id=await permission_service.default_workspace_id(current_user),
        profile_id=profile_id,
        **request.model_dump(exclude_none=True),
    )
    await audit_service.record(
        actor_user_id=current_user.id,
        action="agent_profile.update",
        resource_type="agent_profile",
        resource_id=profile.id,
        workspace_id=profile.workspace_id,
        risk_level=AuditRiskLevel.HIGH if profile.api_key else AuditRiskLevel.MEDIUM,
        metadata={"name": profile.name, "scope": profile.scope, "model_provider": profile.model_provider, "model_name": profile.model_name},
    )
    return APIResponse.success(_response(profile))


@router.delete("/{profile_id}", response_model=APIResponse[None])
async def delete_profile(
    profile_id: str,
    current_user: User = Depends(get_current_user),
    service: AgentProfileService = Depends(get_agent_profile_service),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[None]:
    _ensure_admin(current_user)
    await service.delete_profile(
        current_user.id,
        current_user.role,
        profile_id,
        workspace_id=await permission_service.default_workspace_id(current_user),
    )
    await audit_service.record(
        actor_user_id=current_user.id,
        action="agent_profile.delete",
        resource_type="agent_profile",
        resource_id=profile_id,
        risk_level=AuditRiskLevel.MEDIUM,
    )
    return APIResponse.success()
