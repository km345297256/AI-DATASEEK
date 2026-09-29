from datetime import datetime, UTC
from typing import Optional
import uuid

from pydantic import BaseModel, Field


class TokenUsageRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Provider observations only; these are subsets/details, never additions
    # to prompt_tokens or total_tokens. None means unreported or unverifiable.
    cache_read_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_write_tokens: int | None = Field(default=None, ge=0, strict=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
