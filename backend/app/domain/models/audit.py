from datetime import datetime, UTC
from enum import Enum
from typing import Any, Dict, Optional
import uuid

from pydantic import BaseModel, Field


class AuditStatus(str, Enum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"


class AuditRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AuditLog(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    actor_user_id: str
    workspace_id: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    status: AuditStatus = AuditStatus.SUCCESS
    risk_level: AuditRiskLevel = AuditRiskLevel.LOW
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
