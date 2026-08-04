from pydantic import BaseModel, Field
from datetime import datetime, UTC
from typing import Optional
from enum import Enum
import uuid


class SandboxStatus(str, Enum):
    WARM = "warm"
    ASSIGNED = "assigned"
    PAUSED = "paused"
    DESTROYED = "destroyed"


class SandboxRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    container_name: str
    container_ip: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    status: SandboxStatus = SandboxStatus.WARM
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    assigned_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    destroyed_at: Optional[datetime] = None
