from datetime import datetime, UTC
from enum import Enum
from typing import Dict, List, Optional, Any
import uuid

from pydantic import BaseModel, Field


class RendererScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    WORKSPACE = "workspace"
    PRIVATE = "private"
    SHARED = "shared"


class RendererKind(str, Enum):
    BUILTIN = "builtin"
    API = "api"
    COMPONENT = "component"


class Renderer(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    description: str = ""
    kind: RendererKind = RendererKind.API
    extensions: List[str] = Field(default_factory=list)
    scope: RendererScope = RendererScope.USER
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    enabled: bool = True
    api_url: Optional[str] = None
    entry: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
