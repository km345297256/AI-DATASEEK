from pydantic import BaseModel, Field
from datetime import datetime, UTC
from typing import List, Optional
from enum import Enum
import uuid
from app.domain.models.file import FileInfo


class SessionStatus(str, Enum):
    """Session status enum"""
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"


class SessionSummary(BaseModel):
    """Lightweight session model for list views (excludes heavy events/files)"""
    id: str
    user_id: str
    title: Optional[str] = None
    unread_message_count: int = 0
    latest_message: Optional[str] = None
    latest_message_at: Optional[datetime] = None
    status: SessionStatus = SessionStatus.PENDING
    is_shared: bool = False
    collaborator_user_ids: List[str] = []


class Session(BaseModel):
    """Session model"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str  # User ID that owns this session
    sandbox_id: Optional[str] = Field(default=None)  # Identifier for the sandbox environment
    agent_id: str
    task_id: Optional[str] = None
    llm_overrides: Optional[dict] = None
    dataset_ids: List[str] = Field(default_factory=list)
    sandbox_dataset_ids: List[str] = Field(default_factory=list)
    title: Optional[str] = None
    title_manually_set: bool = False
    unread_message_count: int = 0
    latest_message: Optional[str] = None
    latest_message_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    files: List[FileInfo] = []
    status: SessionStatus = SessionStatus.PENDING
    is_shared: bool = False  # Whether this session is shared publicly
    collaborator_user_ids: List[str] = []  # Users allowed to collaborate on this session
