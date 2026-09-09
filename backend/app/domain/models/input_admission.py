"""Private delivery state for an accepted user event; never an SSE payload."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models.event import MessageEvent


InputState = Literal["pending", "claimed", "running", "completed", "interrupted", "cancelled"]
ACTIVE_INPUT_STATES = ("claimed", "running")


def input_key(event: MessageEvent) -> str:
    return hashlib.sha256(event.bind_producer_event_id().encode()).hexdigest()


def terminal_event_id(key: str, kind: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"ai-dataseek:input:{key}:terminal:{kind}"))


class InputAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    actor_user_id: str
    session_generation: int = Field(default=0, ge=0)
    state: InputState = "pending"
    revision: int = Field(default=1, ge=1)
    runtime_id: str | None = None
    task_id: str | None = None
    lease_expires_at: datetime | None = None
    retry_after: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attempts: int = Field(default=0, ge=0)
    terminal_kind: Literal["done", "wait", "error"] | None = None
    notified: bool = False

    @field_validator("lease_expires_at", "retry_after")
    @classmethod
    def utc_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AcceptedInput(BaseModel):
    """Server-only projection. The user payload already lives in session_events."""
    session_id: str
    key: str
    event: MessageEvent
    admission: InputAdmission
