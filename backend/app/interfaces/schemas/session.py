from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from app.interfaces.schemas.event import AgentSSEEvent
from app.domain.models.session import SessionStatus


class CreateSessionRequest(BaseModel):
    agent_profile_id: Optional[str] = None


class UpdateSessionTitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Session title cannot be empty")
        return title


class ChatRequest(BaseModel):
    """Chat request schema"""
    timestamp: Optional[int] = None
    message: Optional[str] = None
    agent_profile_id: Optional[str] = None
    attachments: Optional[List[dict]] = None
    skills: Optional[List[str]] = None
    mcp_servers: Optional[List[str]] = None
    dataset_ids: Optional[List[str]] = None
    event_id: Optional[str] = None


class ShellViewRequest(BaseModel):
    """Shell view request schema"""
    session_id: str


class CreateSessionResponse(BaseModel):
    """Create session response schema"""
    session_id: str


class GetSessionResponse(BaseModel):
    """Get session response schema"""
    session_id: str
    title: Optional[str] = None
    title_manually_set: bool = False
    status: SessionStatus
    events: List[AgentSSEEvent] = []
    is_shared: bool = False
    is_owner: bool = False
    collaborators: List["SessionCollaboratorUser"] = []


class ListSessionItem(BaseModel):
    """List session item schema"""
    session_id: str
    title: Optional[str] = None
    latest_message: Optional[str] = None
    latest_message_at: Optional[int] = None
    status: SessionStatus
    unread_message_count: int
    is_shared: bool = False
    is_owner: bool = False


class ListSessionResponse(BaseModel):
    """List session response schema"""
    sessions: List[ListSessionItem]


class ConsoleRecord(BaseModel):
    """Console record schema"""
    ps1: str
    command: str
    output: str


class ShellViewResponse(BaseModel):
    """Shell view response schema"""
    output: str
    session_id: str
    console: Optional[List[ConsoleRecord]] = None


class ShareSessionResponse(BaseModel):
    """Share session response schema"""
    session_id: str
    is_shared: bool


class SessionCollaboratorUser(BaseModel):
    id: str
    fullname: str
    email: str


class SessionCollaboratorsResponse(BaseModel):
    collaborators: List[SessionCollaboratorUser] = []


class SessionCollaboratorsUpdateRequest(BaseModel):
    user_ids: List[str] = []


class UserSearchResponse(BaseModel):
    users: List[SessionCollaboratorUser] = []


class SharedSessionResponse(BaseModel):
    """Shared session response schema (for public access)"""
    session_id: str
    title: Optional[str] = None
    status: SessionStatus
    events: List[AgentSSEEvent] = []
    is_shared: bool
