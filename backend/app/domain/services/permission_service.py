from enum import Enum
from typing import Any, Optional

from app.domain.models.user import User, UserRole
from app.domain.models.workspace import WorkspaceRole
from app.infrastructure.models.documents import WorkspaceDocument, WorkspaceMemberDocument
from app.domain.models.workspace import personal_workspace_id


class ResourceScope(str, Enum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    PRIVATE = "private"
    SHARED = "shared"
    USER = "user"  # Backward-compatible private scope.


_ROLE_PERMISSIONS: dict[WorkspaceRole, set[str]] = {
    WorkspaceRole.OWNER: {"read", "use", "write", "delete", "manage"},
    WorkspaceRole.ADMIN: {"read", "use", "write", "delete", "manage"},
    WorkspaceRole.DEVELOPER: {"read", "use", "write"},
    WorkspaceRole.OPERATOR: {"read", "use"},
    WorkspaceRole.VIEWER: {"read"},
    WorkspaceRole.GUEST: {"read"},
}


class PermissionService:
    async def ensure_personal_workspace(self, user: User) -> str:
        workspace_id = personal_workspace_id(user.id)
        workspace = await WorkspaceDocument.find_one(WorkspaceDocument.workspace_id == workspace_id)
        if not workspace:
            workspace = WorkspaceDocument(
                workspace_id=workspace_id,
                name=f"{user.fullname}'s Workspace",
                owner_user_id=user.id,
                is_personal=True,
            )
            await workspace.insert()

        member = await WorkspaceMemberDocument.find_one(
            WorkspaceMemberDocument.workspace_id == workspace_id,
            WorkspaceMemberDocument.user_id == user.id,
        )
        if not member:
            await WorkspaceMemberDocument(
                workspace_id=workspace_id,
                user_id=user.id,
                role=WorkspaceRole.OWNER,
            ).insert()
        return workspace_id

    async def default_workspace_id(self, user: User) -> str:
        return await self.ensure_personal_workspace(user)

    async def can(self, user: User, action: str, resource: Any = None, workspace_id: Optional[str] = None) -> bool:
        if user.role == UserRole.ADMIN:
            return True

        if resource is not None:
            scope = getattr(resource, "scope", None)
            owner_user_id = getattr(resource, "owner_user_id", None) or getattr(resource, "user_id", None)
            resource_workspace_id = getattr(resource, "workspace_id", None)

            if owner_user_id == user.id:
                return True
            if scope == ResourceScope.GLOBAL or scope == "global":
                return action in {"read", "use"}
            if scope in {ResourceScope.PRIVATE, ResourceScope.USER, "private", "user"}:
                return owner_user_id == user.id
            if scope in {ResourceScope.WORKSPACE, ResourceScope.SHARED, "workspace", "shared"}:
                workspace_id = resource_workspace_id or workspace_id

        if not workspace_id:
            return False

        member = await WorkspaceMemberDocument.find_one(
            WorkspaceMemberDocument.workspace_id == workspace_id,
            WorkspaceMemberDocument.user_id == user.id,
        )
        if not member:
            return False

        permissions = _ROLE_PERMISSIONS.get(member.role, set())
        return action in permissions

    async def require(self, user: User, action: str, resource: Any = None, workspace_id: Optional[str] = None) -> None:
        from app.application.errors.exceptions import UnauthorizedError

        if not await self.can(user, action, resource, workspace_id):
            raise UnauthorizedError("Not authorized")


def get_permission_service() -> PermissionService:
    return PermissionService()
