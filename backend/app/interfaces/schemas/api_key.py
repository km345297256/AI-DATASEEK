from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, field_validator
from app.domain.models.api_key import APIKeyScope, APIKeyStatus


class CreateAPIKeyRequest(BaseModel):
    name: str
    scopes: List[APIKeyScope] = [APIKeyScope.FULL]
    expires_in_days: Optional[int] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not v or len(v.strip()) < 1:
            raise ValueError("Name is required")
        if len(v) > 100:
            raise ValueError("Name must be 100 characters or fewer")
        return v.strip()

    @field_validator("expires_in_days")
    @classmethod
    def validate_expires(cls, v):
        if v is not None and v < 1:
            raise ValueError("expires_in_days must be at least 1")
        return v


class APIKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    scopes: List[APIKeyScope]
    status: APIKeyStatus
    expires_at: Optional[datetime]
    last_used_at: Optional[datetime]
    created_at: datetime


class CreateAPIKeyResponse(APIKeyResponse):
    key: str  # raw key, shown only on creation
