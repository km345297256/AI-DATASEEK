from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.services.context_budget import CompactionRecord


class MemoryChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: Literal["storage_bound", "tool_result_limit", "tool_arguments_compact", "history_repair", "step_reset", "durable_projection"]
    before_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")
    messages_before: int = Field(ge=0)
    messages_after: int = Field(ge=0)
    bytes_before: int = Field(ge=0)
    bytes_after: int = Field(ge=0)


class ModelRequestTimings(BaseModel):
    """Local monotonic phase durations, never provider TTFT or billing data."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    memory_flush_ms: float | None = Field(default=None, ge=0)
    context_prepare_ms: float | None = Field(default=None, ge=0)
    request_identity_ms: float | None = Field(default=None, ge=0)
    admission_store_ms: float | None = Field(default=None, ge=0)
    provider_call_ms: float | None = Field(default=None, ge=0)
    usage_settlement_ms: float | None = Field(default=None, ge=0)


class ModelTraceView(BaseModel):
    """Metadata only: no prompts, content, credentials or provider endpoints."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    trace_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    task_id: str = Field(max_length=128)
    kind: Literal["model_request", "memory_change"] = "model_request"
    status: Literal["started", "succeeded", "failed", "cancelled", "budget_exceeded", "recorded"] = "started"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    role: str = Field(default="auxiliary", max_length=64)
    logical_call_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    call_index: int = Field(default=0, ge=0)
    driver_version: str = Field(default="langchain-driver/v1", max_length=64)
    provider: str = Field(default="unknown", max_length=256)
    model_name: str = Field(default="unknown", max_length=256)
    estimator_version: str = Field(default="utf8_bytes_div3_v1", max_length=64)
    is_estimate: Literal[True] = True
    input_tokens_before: int = Field(default=0, ge=0)
    input_tokens_after: int = Field(default=0, ge=0)
    tool_tokens: int = Field(default=0, ge=0)
    input_limit: int = Field(default=0, ge=0)
    reserved_output_tokens: int = Field(default=0, ge=0)
    task_tokens_charged: int = Field(default=0, ge=0)
    # None means unlimited; zero must never masquerade as an absent quota.
    task_token_limit: int | None = Field(default=None, ge=0)
    task_call_limit: int | None = Field(default=None, ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    actual_total_tokens: int | None = Field(default=None, ge=0)
    usage_source: Literal["provider", "reservation", "none"] = "none"
    error_code: Literal["context_budget_exceeded", "task_token_budget_exceeded", "task_call_budget_exceeded", "analysis_budget_deadline_exceeded", "provider_error", "cancelled", "runtime_closed", "trace_store_unavailable"] | None = None
    request_hmac_before: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_hmac_after: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compactions: list[CompactionRecord] = Field(default_factory=list, max_length=256)
    memory_change: MemoryChange | None = None
    timings: ModelRequestTimings = Field(default_factory=ModelRequestTimings)


class ScheduledModelRetry(BaseModel):
    """Private timing decision, with no raw headers, endpoints or error text."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    failed_attempt: int = Field(ge=1)
    next_attempt: int = Field(ge=2)
    maximum_attempts: int = Field(ge=2)
    delay_seconds: float = Field(ge=0)
    reason: Literal[
        "retry_after_seconds", "retry_after_date", "exponential_jitter",
        "invalid_retry_after_jitter", "expired_retry_after_jitter",
    ]
    scheduled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ModelTraceRecord(ModelTraceView):
    user_id: str
    session_id: str
    # Private audit metadata only; adding this must not expand the public API.
    scheduled_retry: ScheduledModelRetry | None = None

    def public_view(self) -> ModelTraceView:
        return ModelTraceView.model_validate(self.model_dump(exclude={"user_id", "session_id", "scheduled_retry"}))
