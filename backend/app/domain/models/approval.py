from datetime import datetime, UTC
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    requester_user_id: str
    workspace_id: Optional[str] = None
    resource_type: str
    resource_id: Optional[str] = None
    requested_permissions: List[str] = Field(default_factory=list)
    reason: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer_user_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    decision_note: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
