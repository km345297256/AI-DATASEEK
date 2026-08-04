from datetime import datetime, UTC
from enum import Enum
from typing import Optional
import uuid

from pydantic import BaseModel, Field


class WorkspaceRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    DEVELOPER = "developer"
    OPERATOR = "operator"
    VIEWER = "viewer"
    GUEST = "guest"


class Workspace(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    owner_user_id: str
    is_personal: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkspaceMember(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    workspace_id: str
    user_id: str
    role: WorkspaceRole = WorkspaceRole.OWNER
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def personal_workspace_id(user_id: str) -> str:
    return f"personal-{user_id}"
