from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_TOOL_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
_PERMISSION_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
_CREDENTIAL_REF_PATTERN = r"^cred_[0-9a-f]{32}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

_MAX_PREVIEW_KEYS = 32
_MAX_PREVIEW_DEPTH = 8
_MAX_PREVIEW_COLLECTION_ITEMS = 128
_MAX_PREVIEW_KEY_LENGTH = 128
_MAX_PREVIEW_STRING_LENGTH = 4_096
_MAX_PREVIEW_BYTES = 12_000


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_preview_value(value: Any, *, depth: int) -> None:
    if depth > _MAX_PREVIEW_DEPTH:
        raise ValueError("tool approval argument preview is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > _MAX_PREVIEW_STRING_LENGTH:
            raise ValueError("tool approval argument preview string is too long")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("tool approval argument preview contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > _MAX_PREVIEW_COLLECTION_ITEMS:
            raise ValueError("tool approval argument preview collection is too large")
        for item in value:
            _validate_preview_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > _MAX_PREVIEW_COLLECTION_ITEMS:
            raise ValueError("tool approval argument preview collection is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > _MAX_PREVIEW_KEY_LENGTH:
                raise ValueError("tool approval argument preview has an invalid key")
            if any(ord(character) < 0x20 for character in key):
                raise ValueError("tool approval argument preview has an invalid key")
            _validate_preview_value(item, depth=depth + 1)
        return
    raise ValueError("tool approval argument preview must contain JSON values")


class ToolApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ToolApprovalEffect(str, Enum):
    SANDBOX_READ = "sandbox_read"
    SANDBOX_WRITE = "sandbox_write"
    NETWORK = "network"
    CREDENTIAL_USE = "credential_use"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


TOOL_APPROVAL_UNCONSUMED_STATUSES = frozenset({
    ToolApprovalStatus.PENDING,
    ToolApprovalStatus.APPROVED,
})
TOOL_APPROVAL_TERMINAL_STATUSES = frozenset({
    ToolApprovalStatus.REJECTED,
    ToolApprovalStatus.CONSUMED,
    ToolApprovalStatus.EXPIRED,
    ToolApprovalStatus.CANCELLED,
})


PermissionSlug = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=128, pattern=_PERMISSION_PATTERN),
]
CredentialRef = Annotated[
    str,
    Field(strict=True, pattern=_CREDENTIAL_REF_PATTERN),
]


class ToolApprovalView(BaseModel):
    """Browser-safe decision state for one exact tool invocation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    approval_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        pattern=r"^[0-9a-f]{32}$",
    )
    revision: int = Field(default=1, strict=True, ge=1)
    status: ToolApprovalStatus = ToolApprovalStatus.PENDING
    tool_name: str = Field(strict=True, pattern=_TOOL_NAME_PATTERN)
    effects: list[ToolApprovalEffect] = Field(default_factory=list, max_length=5)
    permissions: list[PermissionSlug] = Field(default_factory=list, max_length=32)
    arguments_preview: dict[str, Any] = Field(default_factory=dict)
    credential_refs: list[CredentialRef] = Field(default_factory=list, max_length=32)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    decided_at: datetime | None = None
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

    @field_validator("effects", "permissions", "credential_refs")
    @classmethod
    def validate_unique_lists(cls, value: list[Any]) -> list[Any]:
        if len(value) != len(set(value)):
            raise ValueError("tool approval list values must be unique")
        return value

    @field_validator("arguments_preview")
    @classmethod
    def validate_arguments_preview(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > _MAX_PREVIEW_KEYS:
            raise ValueError("tool approval argument preview has too many keys")
        _validate_preview_value(value, depth=0)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("tool approval argument preview must be bounded JSON") from error
        if len(encoded) > _MAX_PREVIEW_BYTES:
            raise ValueError("tool approval argument preview is too large")
        return value

    @field_validator("created_at", "expires_at", "decided_at")
    @classmethod
    def normalize_datetimes(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_timestamps(self) -> "ToolApprovalView":
        if self.expires_at <= self.created_at:
            raise ValueError("tool approval expiry must follow creation")
        if self.decided_at is not None and self.decided_at < self.created_at:
            raise ValueError("tool approval decision cannot predate creation")
        return self


class ToolApprovalRecord(ToolApprovalView):
    """Private persistence record; never return this model through an API."""

    user_id: str = Field(strict=True, min_length=1, max_length=512)
    session_id: str = Field(strict=True, min_length=1, max_length=512)
    task_id: str = Field(strict=True, min_length=1, max_length=128)
    runtime_id: str = Field(strict=True, pattern=r"^[0-9a-f]{32}$")
    call_digest: str = Field(strict=True, pattern=_SHA256_PATTERN)

    def public_view(self) -> ToolApprovalView:
        public_fields = set(ToolApprovalView.model_fields)
        return ToolApprovalView.model_validate(
            self.model_dump(include=public_fields, mode="python")
        )


def to_view(record: ToolApprovalRecord) -> ToolApprovalView:
    return record.public_view()


__all__ = [
    "TOOL_APPROVAL_TERMINAL_STATUSES",
    "TOOL_APPROVAL_UNCONSUMED_STATUSES",
    "ToolApprovalEffect",
    "ToolApprovalRecord",
    "ToolApprovalStatus",
    "ToolApprovalView",
    "to_view",
]
