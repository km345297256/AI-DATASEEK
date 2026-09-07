from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.domain.models.analysis_job import (
    AnalysisJobErrorCode,
    AnalysisJobRecord,
    AnalysisJobStatus,
)
from app.domain.models.spill import SpillArtifactRef


class AnalysisJobDocument(Document):
    """Dedicated durable state for one tool execution job."""

    schema_version: int = 1
    job_id: str
    revision: int = 1
    status: AnalysisJobStatus
    tool_name: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancellable: bool = True
    timeout_seconds: float | None = None
    execution_snapshot_id: str | None = None
    catalog_revision: str | None = None
    result_spill: SpillArtifactRef | None = None
    error_code: AnalysisJobErrorCode | None = None
    user_id: str
    session_id: str
    task_id: str
    runtime_id: str
    tool_call_ref: str
    lease_expires_at: datetime

    @classmethod
    def from_domain(cls, record: AnalysisJobRecord) -> "AnalysisJobDocument":
        return cls.model_validate(record.model_dump(mode="python"))

    def to_domain(self) -> AnalysisJobRecord:
        return AnalysisJobRecord.model_validate(self.model_dump(exclude={"id"}))

    class Settings:
        name = "analysis_jobs"
        indexes = [
            IndexModel([("job_id", ASCENDING)], unique=True),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("session_id", ASCENDING),
                    ("created_at", DESCENDING),
                ],
                name="analysis_job_owner_created",
            ),
            IndexModel(
                [
                    ("runtime_id", ASCENDING),
                    ("status", ASCENDING),
                    ("lease_expires_at", ASCENDING),
                ],
                name="analysis_job_runtime_lease",
            ),
            IndexModel(
                [("status", ASCENDING), ("lease_expires_at", ASCENDING)],
                name="analysis_job_status_lease",
            ),
            IndexModel([("task_id", ASCENDING)], name="analysis_job_task"),
        ]


__all__ = ["AnalysisJobDocument"]
