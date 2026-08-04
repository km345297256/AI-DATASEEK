from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.domain.models.renderer import RendererKind, RendererScope


class RendererResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    kind: RendererKind
    extensions: List[str] = Field(default_factory=list)
    scope: RendererScope
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    enabled: bool = True
    api_url: Optional[str] = None
    entry: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    installed: bool = False
    source: str = "personal"


class RendererListResponse(BaseModel):
    renderers: List[RendererResponse] = Field(default_factory=list)


class RendererRequest(BaseModel):
    name: str
    description: str = ""
    kind: RendererKind = RendererKind.API
    extensions: List[str] = Field(default_factory=list)
    enabled: bool = True
    api_url: Optional[str] = None
    entry: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    is_global: bool = False


class RendererUpsertResponse(BaseModel):
    renderer: RendererResponse
