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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
