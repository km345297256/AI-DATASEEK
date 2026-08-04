from typing import List
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.core.config import get_settings
from app.domain.models.audit import AuditRiskLevel
from app.domain.models.skill import Skill, SkillScope
from app.domain.models.user import User
from app.domain.services.audit_service import AuditService, get_audit_service
from app.domain.services.permission_service import PermissionService, get_permission_service
from app.domain.services.skills import SkillRegistry
from app.domain.services.skills.session_skill_creator import create_skill_from_session as create_session_skill
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.infrastructure.repositories.skill_repository import MongoSkillRepository
from app.interfaces.dependencies import get_current_user, get_user_repository
from app.domain.repositories.user_repository import UserRepository
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.skill import (
    CreateSkillRequest,
    CreateSkillResponse,
    SkillDetailResponse,
    SkillFileContent,
    SkillFileNode,
    SkillListResponse,
    SkillPreferencesResponse,
    SkillResponse,
    SkillUploadResponse,
    UpdateSkillFileRequest,
    UpdateSkillScopeRequest,
    UpdateSkillPreferencesRequest,
)

router = APIRouter(prefix="/skills", tags=["skills"])


def _registry(user_id: str) -> SkillRegistry:
    settings = get_settings()
    return SkillRegistry(
        settings.skills_dir,
        settings.skills_enabled,
        user_id=user_id,
        repository=MongoSkillRepository(),
        user_skills_dir=settings.user_skills_dir,
    )


def _is_owned_skill(skill: Skill, user_id: str) -> bool:
    return (skill.owner_user_id or skill.user_id) == user_id


def _is_installed_skill(skill: Skill, user: User) -> bool:
    return (
        _is_owned_skill(skill, user.id)
        or skill.id in user.installed_skill_ids
    )


def _skill_response(skill: Skill, user: User | None = None) -> SkillResponse:
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        triggers=skill.triggers,
        scope=skill.scope,
        user_id=skill.user_id,
        owner_user_id=skill.owner_user_id,
        workspace_id=skill.workspace_id,
        created_from_session_id=skill.created_from_session_id,
        installed=_is_installed_skill(skill, user) if user else False,
        source="official" if not (skill.owner_user_id or skill.user_id) else "personal",
    )


async def _stored_user(current_user: User, user_repository: UserRepository) -> User:
    user = await user_repository.get_user_by_id(current_user.id)
    if user:
        return user
    return await user_repository.create_user(current_user)


def _safe_skill_root(skill: Skill) -> Path:
    skill_file = Path(skill.path).resolve()
    root = skill_file.parent.resolve()
    if not skill_file.name == "SKILL.md":
        raise HTTPException(status_code=400, detail="Invalid skill path")
    return root


def _build_file_tree(root: Path, current: Path) -> SkillFileNode:
    relative_path = current.relative_to(root).as_posix() if current != root else ""
    if current.is_dir():
        children = [
            _build_file_tree(root, child)
            for child in sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            if not child.name.startswith(".")
        ]
        return SkillFileNode(
            name=current.name,
            path=relative_path,
            type="directory",
            children=children,
        )
    return SkillFileNode(name=current.name, path=relative_path, type="file")


def _read_skill_files(root: Path) -> List[SkillFileContent]:
    files: list[SkillFileContent] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        relative_path = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
            files.append(SkillFileContent(path=relative_path, content=content, binary=False))
        except UnicodeDecodeError:
            files.append(SkillFileContent(path=relative_path, content="(Binary file preview is not available)", binary=True))
    return files


def _safe_skill_file(root: Path, relative_path: str) -> Path:
    requested = Path(relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise HTTPException(status_code=400, detail="Unsafe skill file path")
    target = (root / requested).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unsafe skill file path") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Skill file not found")
    return target


@router.get("", response_model=APIResponse[SkillListResponse])
async def list_skills(
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[SkillListResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    await registry.sync_files_to_repository()
    user = await _stored_user(current_user, user_repository)
    installed = [skill for skill in registry.list_skills() if _is_installed_skill(skill, user)]
    return APIResponse.success(SkillListResponse(skills=[_skill_response(skill, user) for skill in installed]))


@router.get("/catalog", response_model=APIResponse[SkillListResponse])
async def list_skill_catalog(
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[SkillListResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    await registry.sync_files_to_repository()
    user = await _stored_user(current_user, user_repository)
    return APIResponse.success(
        SkillListResponse(skills=[_skill_response(skill, user) for skill in registry.list_skills()])
    )


@router.post("/{skill_id}/install", response_model=APIResponse[SkillResponse])
async def install_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[SkillResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    skill = registry.get_skill_by_id(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    user = await _stored_user(current_user, user_repository)
    if skill.id not in user.installed_skill_ids:
        user.installed_skill_ids.append(skill.id)
        user = await user_repository.update_user(user)
    return APIResponse.success(_skill_response(skill, user))


@router.delete("/{skill_id}/install", response_model=APIResponse[SkillResponse])
async def uninstall_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[SkillResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    skill = registry.get_skill_by_id(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if _is_owned_skill(skill, current_user.id):
        raise HTTPException(status_code=400, detail="Owned skills are always in your library")
    user = await _stored_user(current_user, user_repository)
    user.installed_skill_ids = [item for item in user.installed_skill_ids if item != skill.id]
    removed_name = skill.name.strip().lower()
    user.auto_enabled_skills = [
        name for name in user.auto_enabled_skills
        if name.strip().lower() != removed_name
    ]
    user = await user_repository.update_user(user)
    return APIResponse.success(_skill_response(skill, user))


@router.get("/preferences", response_model=APIResponse[SkillPreferencesResponse])
async def get_skill_preferences(
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[SkillPreferencesResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    stored_user = await _stored_user(current_user, user_repository)
    installed_by_name = {
        skill.name.strip().lower(): skill.name
        for skill in registry.list_skills()
        if _is_installed_skill(skill, stored_user)
    }
    valid_preferences: list[str] = []
    for name in stored_user.auto_enabled_skills:
        canonical = installed_by_name.get(name.strip().lower())
        if canonical and canonical not in valid_preferences:
            valid_preferences.append(canonical)
    if valid_preferences != stored_user.auto_enabled_skills:
        stored_user.auto_enabled_skills = valid_preferences
        stored_user = await user_repository.update_user(stored_user)
    return APIResponse.success(SkillPreferencesResponse(auto_enabled_skills=stored_user.auto_enabled_skills))


@router.put("/preferences", response_model=APIResponse[SkillPreferencesResponse])
async def update_skill_preferences(
    request: UpdateSkillPreferencesRequest,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[SkillPreferencesResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    await registry.sync_files_to_repository()
    stored_user = await _stored_user(current_user, user_repository)
    accessible_by_name = {
        skill.name.strip().lower(): skill.name
        for skill in registry.list_skills()
        if _is_installed_skill(skill, stored_user)
    }
    selected: list[str] = []
    unknown: list[str] = []
    for requested_name in request.auto_enabled_skills:
        normalized = requested_name.strip().lower()
        canonical_name = accessible_by_name.get(normalized)
        if not canonical_name:
            unknown.append(requested_name)
        elif canonical_name not in selected:
            selected.append(canonical_name)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Skills are not available: {', '.join(unknown)}")

    stored_user.auto_enabled_skills = selected
    stored_user = await user_repository.update_user(stored_user)
    return APIResponse.success(
        SkillPreferencesResponse(auto_enabled_skills=stored_user.auto_enabled_skills)
    )


@router.get("/{name}", response_model=APIResponse[SkillDetailResponse])
async def get_skill_detail(
    name: str,
    current_user: User = Depends(get_current_user),
) -> APIResponse[SkillDetailResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    skill = registry.get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    root = _safe_skill_root(skill)
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail="Skill files not found")

    tree = [_build_file_tree(root, child) for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())) if not child.name.startswith(".")]
    files = _read_skill_files(root)
    return APIResponse.success(
        SkillDetailResponse(skill=_skill_response(skill, current_user), tree=tree, files=files)
    )


@router.put("/{name}/files", response_model=APIResponse[SkillDetailResponse])
async def update_skill_file(
    name: str,
    request: UpdateSkillFileRequest,
    current_user: User = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[SkillDetailResponse]:
    registry = _registry(current_user.id)
    await registry.load()
    skill = registry.get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    await permission_service.require(current_user, "write", skill)

    root = _safe_skill_root(skill)
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail="Skill files not found")

    target = _safe_skill_file(root, request.path)
    try:
        target.write_text(request.content, encoding="utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=400, detail="Skill file content must be valid UTF-8 text") from exc

    await registry.load()
    updated_skill = registry.get_skill(name) or skill
    await audit_service.record(
        actor_user_id=current_user.id,
        action="skill.file.update",
        resource_type="skill",
        resource_id=updated_skill.id,
        workspace_id=updated_skill.workspace_id,
        risk_level=AuditRiskLevel.MEDIUM,
        metadata={"name": updated_skill.name, "path": request.path, "scope": updated_skill.scope},
    )
    tree = [_build_file_tree(root, child) for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())) if not child.name.startswith(".")]
    files = _read_skill_files(root)
    return APIResponse.success(
        SkillDetailResponse(skill=_skill_response(updated_skill, current_user), tree=tree, files=files)
    )


@router.patch("/{skill_id}/scope", response_model=APIResponse[SkillResponse])
async def update_skill_scope(
    skill_id: str,
    request: UpdateSkillScopeRequest,
    current_user: User = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[SkillResponse]:
    if request.scope not in {SkillScope.USER, SkillScope.GLOBAL}:
        raise HTTPException(status_code=400, detail="Skill scope must be user or global")

    registry = _registry(current_user.id)
    await registry.load()
    skill = registry.get_skill_by_id(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if (skill.owner_user_id or skill.user_id) != current_user.id:
        raise HTTPException(status_code=403, detail="Only the skill owner can change its scope")

    workspace_id = await permission_service.default_workspace_id(current_user)
    try:
        updated_skill = await registry.change_scope(
            skill,
            request.scope,
            current_user.id,
            workspace_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await audit_service.record(
        actor_user_id=current_user.id,
        action="skill.scope.update",
        resource_type="skill",
        resource_id=updated_skill.id,
        workspace_id=updated_skill.workspace_id,
        risk_level=AuditRiskLevel.HIGH,
        metadata={"name": updated_skill.name, "scope": updated_skill.scope},
    )
    return APIResponse.success(_skill_response(updated_skill, current_user))


@router.post("/upload", response_model=APIResponse[SkillUploadResponse])
async def upload_skill(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[SkillUploadResponse]:
    registry = _registry(current_user.id)
    workspace_id = await permission_service.default_workspace_id(current_user)
    filename = file.filename or "skill"
    lower = filename.lower()

    try:
        if lower.endswith(".md"):
            content = await file.read()
            skills: List[Skill] = [
                await registry.save_markdown_skill(
                    filename,
                    content,
                    scope=SkillScope.USER,
                    user_id=current_user.id,
                    workspace_id=workspace_id,
                )
            ]
        elif lower.endswith(".zip"):
            skills = await registry.save_zip_skills(
                file.file,
                scope=SkillScope.USER,
                user_id=current_user.id,
                workspace_id=workspace_id,
            )
        else:
            raise HTTPException(status_code=400, detail="Only .md and .zip skill uploads are supported")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not skills:
        raise HTTPException(status_code=400, detail="No valid skills were uploaded")

    for skill in skills:
        await audit_service.record(
            actor_user_id=current_user.id,
            action="skill.upload",
            resource_type="skill",
            resource_id=skill.id,
            workspace_id=skill.workspace_id,
            risk_level=AuditRiskLevel.MEDIUM,
            metadata={"name": skill.name, "scope": skill.scope, "filename": filename},
        )

    return APIResponse.success(
        SkillUploadResponse(skills=[_skill_response(skill, current_user) for skill in skills])
    )


@router.post("/from-session", response_model=APIResponse[CreateSkillResponse])
async def create_skill_from_session(
    request: CreateSkillRequest,
    current_user: User = Depends(get_current_user),
    permission_service: PermissionService = Depends(get_permission_service),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[CreateSkillResponse]:
    session_repository = MongoSessionRepository()
    registry = _registry(current_user.id)
    await permission_service.default_workspace_id(current_user)
    try:
        skill = await create_session_skill(
            session_id=request.session_id,
            user_id=current_user.id,
            session_repository=session_repository,
            registry=registry,
        )
    except ValueError as exc:
        if str(exc) == "Session not found":
            raise HTTPException(status_code=404, detail="Session not found") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not skill:
        raise HTTPException(status_code=404, detail="Session not found")
    await audit_service.record(
        actor_user_id=current_user.id,
        action="skill.create_from_session",
        resource_type="skill",
        resource_id=skill.id,
        workspace_id=skill.workspace_id,
        session_id=request.session_id,
        risk_level=AuditRiskLevel.MEDIUM,
        metadata={"name": skill.name, "scope": skill.scope},
    )
    return APIResponse.success(CreateSkillResponse(skill=_skill_response(skill, current_user)))
