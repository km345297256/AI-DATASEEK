"""Focused administration routes for AI-DataSeek.

The administration interface intentionally contains only resource usage and
configuration, task inspection, and Skill/MCP governance. User-facing plugin
installation remains in the dedicated Skill, MCP and Renderer modules.
"""

from datetime import UTC, datetime
import logging
from pathlib import Path
import shutil
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, Query

from app.application.errors.exceptions import BadRequestError, NotFoundError, UnauthorizedError
from app.application.services.agent_service import AgentService
from app.application.services.resource_configuration_service import ResourceConfigurationService
from app.core.config import get_settings
from app.domain.models.audit import AuditRiskLevel
from app.domain.models.mcp_config import MCPScope
from app.domain.models.session import SessionStatus
from app.domain.models.skill import SkillScope
from app.domain.models.user import User, UserRole
from app.domain.services.audit_service import AuditService, get_audit_service
from app.domain.services.resource_usage_service import ResourceUsageService, get_resource_usage_service
from app.domain.services.user_name_service import UserNameService
from app.infrastructure.models.documents import SessionDocument, SkillDocument, UserDocument
from app.infrastructure.repositories.mongo_mcp_repository import MongoMCPRepository
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.schemas.admin import (
    AdminMCPServerListResponse,
    AdminMCPServerResponse,
    AdminMCPServerUpdateRequest,
    AdminSkillListResponse,
    AdminSkillResponse,
    AdminSkillUpdateRequest,
    AdminTaskListResponse,
    AdminTaskResponse,
    ResourceUsageOverviewResponse,
    SandboxResourceConfigurationResponse,
    SandboxResourceConfigurationUpdateRequest,
)
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.event import EventMapper
from app.interfaces.schemas.session import SharedSessionResponse


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


def _require_admin(user: User) -> None:
    if user.role != UserRole.ADMIN:
        raise UnauthorizedError("Admin access required")


async def _user_names(user_ids: set[str]) -> dict[str, str]:
    return await UserNameService.resolve(user_ids)


def _skill_response(doc: SkillDocument, owner_names: dict[str, str]) -> AdminSkillResponse:
    owner_id = doc.owner_user_id or doc.user_id or ""
    return AdminSkillResponse(
        id=doc.skill_id,
        name=doc.name,
        description=doc.description,
        triggers=doc.triggers,
        scope=doc.scope,
        user_id=doc.user_id,
        owner_user_id=doc.owner_user_id,
        owner_fullname=owner_names.get(owner_id),
        workspace_id=doc.workspace_id,
        path=doc.path,
        created_from_session_id=doc.created_from_session_id,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _mcp_response(name: str, server, owner_names: dict[str, str]) -> AdminMCPServerResponse:
    owner_id = server.owner_user_id or server.user_id or ""
    return AdminMCPServerResponse(
        name=name,
        transport=server.transport,
        enabled=server.enabled,
        description=server.description,
        command=server.command,
        args=server.args,
        url=server.url,
        scope=server.scope,
        user_id=server.user_id,
        owner_user_id=server.owner_user_id,
        owner_fullname=owner_names.get(owner_id),
        workspace_id=server.workspace_id,
        risk_level=server.risk_level,
    )


@router.get("/resource-usage", response_model=APIResponse[ResourceUsageOverviewResponse])
async def get_resource_usage(
    start_at: Optional[datetime] = Query(default=None),
    end_at: Optional[datetime] = Query(default=None),
    include_sandboxes: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    resource_usage_service: ResourceUsageService = Depends(get_resource_usage_service),
) -> APIResponse[ResourceUsageOverviewResponse]:
    _require_admin(current_user)
    overview = await resource_usage_service.get_overview(
        start_at=start_at,
        end_at=end_at,
        include_sandboxes=include_sandboxes,
    )
    return APIResponse.success(ResourceUsageOverviewResponse(**overview))


@router.get(
    "/resource-config",
    response_model=APIResponse[SandboxResourceConfigurationResponse],
)
async def get_resource_configuration(
    current_user: User = Depends(get_current_user),
) -> APIResponse[SandboxResourceConfigurationResponse]:
    _require_admin(current_user)
    config = await ResourceConfigurationService().get()
    return APIResponse.success(SandboxResourceConfigurationResponse(**config))


@router.patch(
    "/resource-config",
    response_model=APIResponse[SandboxResourceConfigurationResponse],
)
async def update_resource_configuration(
    request: SandboxResourceConfigurationUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> APIResponse[SandboxResourceConfigurationResponse]:
    _require_admin(current_user)
    config = await ResourceConfigurationService().update(
        **request.model_dump(exclude_none=True),
    )
    return APIResponse.success(SandboxResourceConfigurationResponse(**config))


@router.get("/tasks", response_model=APIResponse[AdminTaskListResponse])
async def list_tasks(
    query: Optional[str] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    status: Optional[SessionStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> APIResponse[AdminTaskListResponse]:
    _require_admin(current_user)
    docs = await SessionDocument.find().sort("-updated_at").to_list()
    if user_id:
        docs = [doc for doc in docs if doc.user_id == user_id]
    if status:
        docs = [doc for doc in docs if doc.status == status]
    if query:
        needle = query.lower()
        docs = [
            doc
            for doc in docs
            if needle in doc.session_id.lower()
            or needle in doc.user_id.lower()
            or needle in (doc.task_id or "").lower()
            or needle in (doc.title or "").lower()
            or needle in (doc.latest_message or "").lower()
        ]
    total = len(docs)
    page_docs = docs[offset : offset + limit]
    user_names = await _user_names({doc.user_id for doc in page_docs})
    tasks = [
        AdminTaskResponse(
            session_id=doc.session_id,
            user_id=doc.user_id,
            user_fullname=user_names.get(doc.user_id),
            agent_id=doc.agent_id,
            task_id=doc.task_id,
            sandbox_id=doc.sandbox_id,
            title=doc.title,
            latest_message=doc.latest_message,
            latest_message_at=doc.latest_message_at,
            status=doc.status,
            unread_message_count=doc.unread_message_count,
            is_shared=bool(doc.is_shared),
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )
        for doc in page_docs
    ]
    return APIResponse.success(AdminTaskListResponse(tasks=tasks, total=total))


@router.get("/tasks/{session_id}/replay", response_model=APIResponse[SharedSessionResponse])
async def get_task_replay(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> APIResponse[SharedSessionResponse]:
    _require_admin(current_user)
    session = await SessionDocument.find_one(SessionDocument.session_id == session_id)
    if not session:
        raise NotFoundError("Task not found")
    events = await agent_service.get_session_events(session_id)
    return APIResponse.success(
        SharedSessionResponse(
            session_id=session.session_id,
            title=session.title,
            status=session.status,
            events=await EventMapper.events_to_sse_events(events),
            is_shared=bool(session.is_shared),
        )
    )


@router.get("/skills", response_model=APIResponse[AdminSkillListResponse])
async def list_skills(
    query: Optional[str] = Query(default=None),
    scope: Optional[SkillScope] = Query(default=None),
    owner_user_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> APIResponse[AdminSkillListResponse]:
    _require_admin(current_user)
    docs = await SkillDocument.find().sort("-updated_at").to_list()
    if scope:
        docs = [doc for doc in docs if doc.scope == scope]
    if owner_user_id:
        docs = [doc for doc in docs if (doc.owner_user_id or doc.user_id) == owner_user_id]
    if query:
        needle = query.lower()
        docs = [
            doc
            for doc in docs
            if needle in doc.skill_id.lower()
            or needle in doc.name.lower()
            or needle in (doc.description or "").lower()
            or needle in (doc.path or "").lower()
            or any(needle in trigger.lower() for trigger in doc.triggers)
        ]
    total = len(docs)
    page_docs = docs[offset : offset + limit]
    names = await _user_names(
        {
            owner
            for doc in page_docs
            for owner in [doc.owner_user_id or doc.user_id]
            if owner
        }
    )
    return APIResponse.success(
        AdminSkillListResponse(
            skills=[_skill_response(doc, names) for doc in page_docs],
            total=total,
        )
    )


@router.patch("/skills/{skill_id}", response_model=APIResponse[AdminSkillResponse])
async def update_skill(
    skill_id: str,
    request: AdminSkillUpdateRequest,
    current_user: User = Depends(get_current_user),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[AdminSkillResponse]:
    _require_admin(current_user)
    doc = await SkillDocument.find_one(SkillDocument.skill_id == skill_id)
    if not doc:
        raise NotFoundError("Skill not found")
    if request.scope is not None:
        doc.scope = request.scope
        if request.scope == SkillScope.GLOBAL:
            doc.user_id = None
            doc.owner_user_id = None
            doc.workspace_id = None
    if request.user_id is not None:
        doc.user_id = request.user_id or None
    if request.owner_user_id is not None:
        doc.owner_user_id = request.owner_user_id or None
    if request.workspace_id is not None:
        doc.workspace_id = request.workspace_id or None
    doc.updated_at = datetime.now(UTC)
    await doc.save()
    await audit_service.record(
        actor_user_id=current_user.id,
        action="admin.skill.update",
        resource_type="skill",
        resource_id=skill_id,
        workspace_id=doc.workspace_id,
        risk_level=AuditRiskLevel.HIGH,
        metadata={
            "name": doc.name,
            "scope": doc.scope,
            "owner_user_id": doc.owner_user_id,
            "user_id": doc.user_id,
        },
    )
    names = await _user_names(
        {owner for owner in [doc.owner_user_id or doc.user_id] if owner}
    )
    return APIResponse.success(_skill_response(doc, names))


def _managed_skill_package(path: str) -> Optional[Path]:
    skill_file = Path(path).resolve()
    if not skill_file.exists():
        return None
    if skill_file.name != "SKILL.md" or not skill_file.is_file():
        raise BadRequestError("Invalid skill package path")
    settings = get_settings()
    allowed_roots = {
        Path(settings.skills_dir).resolve(),
        Path(settings.user_skills_dir).resolve(),
    }
    package_dir = skill_file.parent
    if not any(package_dir.is_relative_to(root) for root in allowed_roots):
        raise BadRequestError("Skill package is outside managed skill directories")
    return package_dir


def _stage_skill_package_for_deletion(
    package_dir: Optional[Path],
) -> Optional[tuple[Path, Path]]:
    if package_dir is None:
        return None
    trash_root = package_dir.parent / ".skill-trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    staged_dir = trash_root / uuid.uuid4().hex
    shutil.move(str(package_dir), str(staged_dir))
    return package_dir, staged_dir


def _rollback_staged_skill_package(staged: Optional[tuple[Path, Path]]) -> None:
    if not staged:
        return
    package_dir, staged_dir = staged
    if staged_dir.exists() and not package_dir.exists():
        shutil.move(str(staged_dir), str(package_dir))


def _purge_staged_skill_package(staged: Optional[tuple[Path, Path]]) -> None:
    if not staged:
        return
    _, staged_dir = staged
    trash_root = staged_dir.parent
    shutil.rmtree(staged_dir, ignore_errors=True)
    try:
        trash_root.rmdir()
    except OSError:
        pass


async def _clear_deleted_skill_references(skill_id: str, skill_name: str) -> None:
    normalized_name = skill_name.strip().lower()
    for user in await UserDocument.find().to_list():
        installed = [item for item in user.installed_skill_ids if item != skill_id]
        automatic = [
            name
            for name in user.auto_enabled_skills
            if name.strip().lower() != normalized_name
        ]
        if installed == user.installed_skill_ids and automatic == user.auto_enabled_skills:
            continue
        user.installed_skill_ids = installed
        user.auto_enabled_skills = automatic
        await user.save()


@router.delete("/skills/{skill_id}", response_model=APIResponse[dict])
async def delete_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[dict]:
    _require_admin(current_user)
    doc = await SkillDocument.find_one(SkillDocument.skill_id == skill_id)
    if not doc:
        raise NotFoundError("Skill not found")
    metadata = {"name": doc.name, "scope": doc.scope, "path": doc.path}
    workspace_id = doc.workspace_id
    staged = _stage_skill_package_for_deletion(_managed_skill_package(doc.path))
    try:
        await doc.delete()
    except Exception:
        _rollback_staged_skill_package(staged)
        raise
    _purge_staged_skill_package(staged)
    try:
        await _clear_deleted_skill_references(skill_id, doc.name)
    except Exception:
        logger.exception("Failed to clear user references for deleted skill %s", skill_id)
    await audit_service.record(
        actor_user_id=current_user.id,
        action="admin.skill.delete",
        resource_type="skill",
        resource_id=skill_id,
        workspace_id=workspace_id,
        risk_level=AuditRiskLevel.HIGH,
        metadata=metadata,
    )
    return APIResponse.success({})


@router.get("/mcp/servers", response_model=APIResponse[AdminMCPServerListResponse])
async def list_mcp_servers(
    query: Optional[str] = Query(default=None),
    scope: Optional[MCPScope] = Query(default=None),
    owner_user_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> APIResponse[AdminMCPServerListResponse]:
    _require_admin(current_user)
    config = await MongoMCPRepository().get_mcp_config()
    items = list(sorted(config.mcpServers.items()))
    if scope:
        items = [(name, server) for name, server in items if server.scope == scope]
    if owner_user_id:
        items = [
            (name, server)
            for name, server in items
            if (server.owner_user_id or server.user_id) == owner_user_id
        ]
    if query:
        needle = query.lower()
        items = [
            (name, server)
            for name, server in items
            if needle in name.lower()
            or needle in (server.description or "").lower()
            or needle in (server.command or "").lower()
            or needle in (server.url or "").lower()
        ]
    total = len(items)
    page_items = items[offset : offset + limit]
    names = await _user_names(
        {
            owner
            for _, server in page_items
            for owner in [server.owner_user_id or server.user_id]
            if owner
        }
    )
    return APIResponse.success(
        AdminMCPServerListResponse(
            servers=[_mcp_response(name, server, names) for name, server in page_items],
            total=total,
        )
    )


@router.patch("/mcp/servers/{name}", response_model=APIResponse[AdminMCPServerResponse])
async def update_mcp_server(
    name: str,
    request: AdminMCPServerUpdateRequest,
    current_user: User = Depends(get_current_user),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[AdminMCPServerResponse]:
    _require_admin(current_user)
    repository = MongoMCPRepository()
    config = await repository.get_mcp_config()
    server = config.mcpServers.get(name)
    if not server:
        raise NotFoundError("MCP server not found")
    if request.enabled is not None:
        server.enabled = request.enabled
    if request.scope is not None:
        server.scope = request.scope
        if request.scope == MCPScope.GLOBAL:
            server.user_id = None
            server.owner_user_id = None
            server.workspace_id = None
    if request.risk_level is not None:
        server.risk_level = request.risk_level
    if request.user_id is not None:
        server.user_id = request.user_id or None
    if request.owner_user_id is not None:
        server.owner_user_id = request.owner_user_id or None
    if request.workspace_id is not None:
        server.workspace_id = request.workspace_id or None
    config.mcpServers[name] = server
    await repository.save_mcp_config(config)
    await audit_service.record(
        actor_user_id=current_user.id,
        action="admin.mcp.update",
        resource_type="mcp_server",
        resource_id=name,
        workspace_id=server.workspace_id,
        risk_level=AuditRiskLevel.HIGH,
        metadata={
            "scope": server.scope,
            "enabled": server.enabled,
            "risk_level": server.risk_level,
        },
    )
    names = await _user_names(
        {owner for owner in [server.owner_user_id or server.user_id] if owner}
    )
    return APIResponse.success(_mcp_response(name, server, names))


@router.delete("/mcp/servers/{name}", response_model=APIResponse[dict])
async def delete_mcp_server(
    name: str,
    current_user: User = Depends(get_current_user),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[dict]:
    _require_admin(current_user)
    repository = MongoMCPRepository()
    config = await repository.get_mcp_config()
    server = config.mcpServers.get(name)
    if not server:
        raise NotFoundError("MCP server not found")
    config.mcpServers.pop(name, None)
    await repository.save_mcp_config(config)
    await audit_service.record(
        actor_user_id=current_user.id,
        action="admin.mcp.delete",
        resource_type="mcp_server",
        resource_id=name,
        workspace_id=server.workspace_id,
        risk_level=AuditRiskLevel.HIGH,
        metadata={
            "scope": server.scope,
            "enabled": server.enabled,
            "risk_level": server.risk_level,
        },
    )
    return APIResponse.success({})
