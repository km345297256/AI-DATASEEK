from pydantic import BaseModel, Field
from typing import List, Optional
from app.domain.models.skill import SkillScope


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    triggers: List[str] = []
    scope: SkillScope = SkillScope.GLOBAL
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    created_from_session_id: Optional[str] = None
    installed: bool = False
    source: str = "personal"


class SkillListResponse(BaseModel):
    skills: List[SkillResponse] = []


class SkillPreferencesResponse(BaseModel):
    auto_enabled_skills: List[str] = Field(default_factory=list)


class UpdateSkillPreferencesRequest(BaseModel):
    auto_enabled_skills: List[str] = Field(default_factory=list)


class SkillFileNode(BaseModel):
    name: str
    path: str
    type: str
    children: List["SkillFileNode"] = Field(default_factory=list)


class SkillFileContent(BaseModel):
    path: str
    content: str
    binary: bool = False


class SkillDetailResponse(BaseModel):
    skill: SkillResponse
    tree: List[SkillFileNode] = Field(default_factory=list)
    files: List[SkillFileContent] = Field(default_factory=list)


class UpdateSkillFileRequest(BaseModel):
    path: str
    content: str


class UpdateSkillScopeRequest(BaseModel):
    scope: SkillScope


class SkillUploadResponse(BaseModel):
    skills: List[SkillResponse] = []


class CreateSkillRequest(BaseModel):
    session_id: str


class CreateSkillResponse(BaseModel):
    skill: SkillResponse
