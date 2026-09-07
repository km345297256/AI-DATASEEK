from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.domain.models.spill import SpillArtifactRef


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class AnalysisJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


class AnalysisJobErrorCode(str, Enum):
    """Public, bounded failure categories; exception text is never persisted."""

    CANCELLED = "cancelled"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_FAILED = "tool_failed"
    WORKER_INTERRUPTED = "worker_interrupted"


ANALYSIS_JOB_ACTIVE_STATUSES = frozenset({
    AnalysisJobStatus.QUEUED,
    AnalysisJobStatus.RUNNING,
    AnalysisJobStatus.CANCELLING,
})
ANALYSIS_JOB_TERMINAL_STATUSES = frozenset({
    AnalysisJobStatus.SUCCEEDED,
    AnalysisJobStatus.FAILED,
    AnalysisJobStatus.CANCELLED,
    AnalysisJobStatus.TIMED_OUT,
    AnalysisJobStatus.INTERRUPTED,
})


class AnalysisJobView(BaseModel):
    """Browser-safe state for one durable tool execution."""

    schema_version: Literal[1] = 1
    job_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        pattern=r"^[0-9a-f]{32}$",
    )
    revision: int = Field(default=1, strict=True, ge=1)
    status: AnalysisJobStatus = AnalysisJobStatus.QUEUED
    tool_name: str = Field(min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancellable: bool = True
    timeout_seconds: float | None = Field(default=None, gt=0, le=86_400)
    execution_snapshot_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    catalog_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    result_spill: SpillArtifactRef | None = None
    error_code: AnalysisJobErrorCode | None = None

    @field_validator("timeout_seconds")
    @classmethod
    def validate_finite_timeout(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("analysis job timeout must be finite")
        return value

    @field_validator(
        "created_at",
        "started_at",
        "finished_at",
        "cancel_requested_at",
    )
    @classmethod
    def normalize_public_datetimes(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


class AnalysisJobRecord(AnalysisJobView):
    """Private persistence record; never serialize this class to the browser."""

    user_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=512)
    task_id: str = Field(min_length=1, max_length=128)
    runtime_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    tool_call_ref: str = Field(pattern=r"^call:sha256:[0-9a-f]{12}$")
    lease_expires_at: datetime

    @field_validator("lease_expires_at")
    @classmethod
    def normalize_lease_datetime(cls, value: datetime) -> datetime:
        normalized = _as_utc(value)
        assert normalized is not None
        return normalized

    def public_view(self) -> AnalysisJobView:
        public_fields = set(AnalysisJobView.model_fields)
        return AnalysisJobView.model_validate(
            self.model_dump(include=public_fields, mode="python")
        )


def to_view(record: AnalysisJobRecord) -> AnalysisJobView:
    return record.public_view()


__all__ = [
    "ANALYSIS_JOB_ACTIVE_STATUSES",
    "ANALYSIS_JOB_TERMINAL_STATUSES",
    "AnalysisJobErrorCode",
    "AnalysisJobRecord",
    "AnalysisJobStatus",
    "AnalysisJobView",
    "to_view",
]
