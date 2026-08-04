from typing import Optional, List
from datetime import datetime, UTC
from pydantic import BaseModel
from enum import Enum


class APIKeyScope(str, Enum):
    FULL = "full"


class APIKeyStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class APIKey(BaseModel):
    id: str
    user_id: str
    name: str
    key_prefix: str
    key_hash: str
    scopes: List[APIKeyScope] = [APIKeyScope.FULL]
    status: APIKeyStatus = APIKeyStatus.ACTIVE
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    created_at: datetime = datetime.now(UTC)
    updated_at: datetime = datetime.now(UTC)

    def is_valid(self) -> bool:
        if self.status != APIKeyStatus.ACTIVE:
            return False
        if self.expires_at and datetime.now(UTC) > self.expires_at:
            return False
        return True

    def revoke(self):
        self.status = APIKeyStatus.REVOKED
        self.updated_at = datetime.now(UTC)

    def update_last_used(self):
        self.last_used_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
