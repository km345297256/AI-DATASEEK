from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException

from app.domain.models.renderer import Renderer, RendererKind, RendererScope
from app.domain.models.user import User
from app.domain.models.audit import AuditRiskLevel
from app.domain.services.audit_service import AuditService, get_audit_service
from app.domain.services.permission_service import PermissionService, get_permission_service
from app.infrastructure.repositories.renderer_repository import MongoRendererRepository
from app.interfaces.dependencies import get_current_user
from app.interfaces.dependencies import get_user_repository
from app.domain.repositories.user_repository import UserRepository
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.renderer import (
    RendererListResponse,
    RendererRequest,
    RendererResponse,
    RendererUpsertResponse,
)

router = APIRouter(prefix="/renderers", tags=["renderers"])


def _repository() -> MongoRendererRepository:
    return MongoRendererRepository()


def _normalize_extensions(extensions: list[str]) -> list[str]:
    normalized: list[str] = []
    for extension in extensions:
        value = extension.strip().lower().lstrip(".")
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _validate_request(request: RendererRequest) -> list[str]:
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Renderer name is required")
    normalized_extensions = _normalize_extensions(request.extensions)
    if not normalized_extensions:
        raise HTTPException(status_code=400, detail="Renderer extensions are required")
    if request.kind == RendererKind.BUILTIN:
        raise HTTPException(status_code=400, detail="Builtin renderers cannot be managed by API")
    if request.kind == RendererKind.API and not request.api_url:
        raise HTTPException(status_code=400, detail="API renderer requires api_url")
    if request.kind == RendererKind.COMPONENT and not request.entry:
        raise HTTPException(status_code=400, detail="Component renderer requires entry")
    return normalized_extensions


def _is_owned(renderer: Renderer, user_id: str) -> bool:
    return (renderer.owner_user_id or renderer.user_id) == user_id


def _is_installed(renderer: Renderer, user: User) -> bool:
    return _is_owned(renderer, user.id) or renderer.id in user.installed_renderer_ids


def _renderer_response(renderer: Renderer, user: User | None = None) -> RendererResponse:
    return RendererResponse(
        **renderer.model_dump(),
        installed=_is_installed(renderer, user) if user else False,
        source="official" if not (renderer.owner_user_id or renderer.user_id) else "personal",
    )


async def _stored_user(current_user: User, user_repository: UserRepository) -> User:
    user = await user_repository.get_user_by_id(current_user.id)
    if user:
        return user
    return await user_repository.create_user(current_user)


@router.get("", response_model=APIResponse[RendererListResponse])
async def list_renderers(
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[RendererListResponse]:
    renderers = await _repository().list_accessible(current_user.id)
    user = await _stored_user(current_user, user_repository)
    return APIResponse.success(
        RendererListResponse(renderers=[_renderer_response(renderer, user) for renderer in renderers if _is_installed(renderer, user)])
    )


@router.get("/catalog", response_model=APIResponse[RendererListResponse])
async def list_renderer_catalog(
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[RendererListResponse]:
    renderers = await _repository().list_accessible(current_user.id)
    user = await _stored_user(current_user, user_repository)
    return APIResponse.success(
        RendererListResponse(renderers=[_renderer_response(renderer, user) for renderer in renderers])
    )


@router.post("/{renderer_id}/install", response_model=APIResponse[RendererResponse])
async def install_renderer(
    renderer_id: str,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[RendererResponse]:
    renderer = await _repository().get_accessible_by_id(renderer_id, current_user.id)
    if not renderer:
        raise HTTPException(status_code=404, detail="Renderer not found")
    user = await _stored_user(current_user, user_repository)
    if renderer.id not in user.installed_renderer_ids:
        user.installed_renderer_ids.append(renderer.id)
        user = await user_repository.update_user(user)
    return APIResponse.success(_renderer_response(renderer, user))


@router.delete("/{renderer_id}/install", response_model=APIResponse[RendererResponse])
async def uninstall_renderer(
    renderer_id: str,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[RendererResponse]:
    renderer = await _repository().get_accessible_by_id(renderer_id, current_user.id)
    if not renderer:
        raise HTTPException(status_code=404, detail="Renderer not found")
    if _is_owned(renderer, current_user.id):
        raise HTTPException(status_code=400, detail="Owned renderers are always in your library")
    user = await _stored_user(current_user, user_repository)
    user.installed_renderer_ids = [item for item in user.installed_renderer_ids if item != renderer.id]
    user = await user_repository.update_user(user)
    return APIResponse.success(_renderer_response(renderer, user))


@router.post("", response_model=APIResponse[RendererUpsertResponse])
async def create_renderer(
    request: RendererRequest,
    current_user: User = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[RendererUpsertResponse]:
    normalized_extensions = _validate_request(request)
    if request.is_global and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create global renderers")
    workspace_id = await permission_service.default_workspace_id(current_user)
    renderer = Renderer(
        name=request.name.strip(),
        description=request.description,
        kind=request.kind,
        extensions=normalized_extensions,
        scope=RendererScope.GLOBAL if request.is_global else RendererScope.USER,
        user_id=None if request.is_global else current_user.id,
        owner_user_id=current_user.id,
        workspace_id=workspace_id if not request.is_global else None,
        enabled=request.enabled,
        api_url=request.api_url,
        entry=request.entry,
        config=request.config,
    )
    saved = await _repository().save(renderer)
    await audit_service.record(
        actor_user_id=current_user.id,
        action="renderer.create",
        resource_type="renderer",
        resource_id=saved.id,
        workspace_id=saved.workspace_id,
        risk_level=AuditRiskLevel.MEDIUM,
        metadata={"kind": saved.kind, "scope": saved.scope, "extensions": saved.extensions},
    )
    return APIResponse.success(RendererUpsertResponse(renderer=_renderer_response(saved, current_user)))


@router.put("/{renderer_id}", response_model=APIResponse[RendererUpsertResponse])
async def update_renderer(
    renderer_id: str,
    request: RendererRequest,
    current_user: User = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[RendererUpsertResponse]:
    normalized_extensions = _validate_request(request)
    repository = _repository()
    renderer = await repository.get_accessible_by_id(renderer_id, current_user.id)
    if not renderer:
        raise HTTPException(status_code=404, detail="Renderer not found")
    await permission_service.require(current_user, "write", renderer)

    if request.is_global and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can promote renderers to global")

    renderer.name = request.name.strip()
    renderer.description = request.description
    renderer.kind = request.kind
    renderer.extensions = normalized_extensions
    renderer.scope = RendererScope.GLOBAL if request.is_global else RendererScope.USER
    renderer.user_id = None if request.is_global else current_user.id
    renderer.owner_user_id = current_user.id
    renderer.workspace_id = None if request.is_global else await permission_service.default_workspace_id(current_user)
    renderer.enabled = request.enabled
    renderer.api_url = request.api_url
    renderer.entry = request.entry
    renderer.config = request.config
    renderer.updated_at = datetime.now(UTC)

    saved = await repository.save(renderer)
    await audit_service.record(
        actor_user_id=current_user.id,
        action="renderer.update",
        resource_type="renderer",
        resource_id=saved.id,
        workspace_id=saved.workspace_id,
        risk_level=AuditRiskLevel.MEDIUM,
        metadata={"kind": saved.kind, "scope": saved.scope, "extensions": saved.extensions},
    )
    return APIResponse.success(RendererUpsertResponse(renderer=_renderer_response(saved, current_user)))


@router.delete("/{renderer_id}", response_model=APIResponse[RendererListResponse])
async def delete_renderer(
    renderer_id: str,
    current_user: User = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[RendererListResponse]:
    repository = _repository()
    renderer = await repository.get_accessible_by_id(renderer_id, current_user.id)
    if not renderer:
        raise HTTPException(status_code=404, detail="Renderer not found")
    await permission_service.require(current_user, "delete", renderer)
    await repository.delete(renderer_id)
    await audit_service.record(
        actor_user_id=current_user.id,
        action="renderer.delete",
        resource_type="renderer",
        resource_id=renderer_id,
        workspace_id=renderer.workspace_id,
        risk_level=AuditRiskLevel.MEDIUM,
        metadata={"scope": renderer.scope},
    )
    renderers = await repository.list_accessible(current_user.id)
    return APIResponse.success(
        RendererListResponse(renderers=[_renderer_response(item) for item in renderers])
    )
