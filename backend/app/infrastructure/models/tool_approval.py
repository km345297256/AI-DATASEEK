from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.domain.models.tool_approval import (
    ToolApprovalEffect,
    ToolApprovalRecord,
    ToolApprovalStatus,
)


class ToolApprovalDocument(Document):
    """Durable state for one invocation-level approval decision."""

    schema_version: int = 1
    approval_id: str
    revision: int = 1
    status: ToolApprovalStatus
    tool_name: str
    effects: list[ToolApprovalEffect] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    arguments_preview: dict[str, Any] = Field(default_factory=dict)
    credential_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    execution_snapshot_id: str | None = None
    catalog_revision: str | None = None
    user_id: str
    session_id: str
    task_id: str
    runtime_id: str
    call_digest: str

    @classmethod
    def from_domain(cls, record: ToolApprovalRecord) -> "ToolApprovalDocument":
        return cls.model_validate(record.model_dump(mode="python"))

    def to_domain(self) -> ToolApprovalRecord:
        return ToolApprovalRecord.model_validate(self.model_dump(exclude={"id"}))

    class Settings:
        name = "tool_approvals"
        indexes = [
            IndexModel([("approval_id", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("session_id", ASCENDING),
                    ("created_at", DESCENDING),
                ],
                name="tool_approval_owner_created",
            ),
            IndexModel(
                [
                    ("runtime_id", ASCENDING),
                    ("status", ASCENDING),
                    ("expires_at", ASCENDING),
                ],
                name="tool_approval_runtime_status_expiry",
            ),
            IndexModel(
                [("status", ASCENDING), ("expires_at", ASCENDING)],
                name="tool_approval_status_expiry",
            ),
            IndexModel([("task_id", ASCENDING)], name="tool_approval_task"),
        ]


__all__ = ["ToolApprovalDocument"]
