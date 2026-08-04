from datetime import datetime, UTC
from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid


class SkillScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    WORKSPACE = "workspace"
    PRIVATE = "private"
    SHARED = "shared"


class Skill(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    description: str = ""
    triggers: List[str] = Field(default_factory=list)
    priority: int = 0
    max_context_chars: int = 6000
    content: str
    path: str
    scripts: List[str] = Field(default_factory=list)
    references: List[str] = Field(default_factory=list)
    templates: List[str] = Field(default_factory=list)
    scope: SkillScope = SkillScope.GLOBAL
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    created_from_session_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
