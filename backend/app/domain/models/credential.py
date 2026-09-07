from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CREDENTIAL_REFERENCE_PATTERN = r"^cred_[0-9a-f]{32}$"
CREDENTIAL_PROVIDER_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
CREDENTIAL_TOOL_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
CREDENTIAL_SLOT_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class CredentialErrorCode(str, Enum):
    """Stable value-free failure codes safe for API, Agent, and logs."""

    VAULT_UNAVAILABLE = "credential_vault_unavailable"
    BINDING_CONFLICT = "credential_binding_conflict"
    BINDING_MISSING = "credential_binding_missing"
    BINDING_CHANGED = "credential_binding_changed"
    CONTRACT_INVALID = "credential_contract_invalid"
    STATE_CONFLICT = "credential_state_conflict"
    STATE_UNAVAILABLE = "credential_state_unavailable"


class CredentialView(BaseModel):
    """The only credential representation allowed outside the vault boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    reference: str = Field(
        pattern=CREDENTIAL_REFERENCE_PATTERN,
    )
    provider: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=CREDENTIAL_PROVIDER_PATTERN,
    )
    tool_name: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=CREDENTIAL_TOOL_NAME_PATTERN,
    )
    slot: str = Field(
        strict=True,
        min_length=1,
        max_length=32,
        pattern=CREDENTIAL_SLOT_PATTERN,
    )
    revision: int = Field(default=1, strict=True, ge=1)
    revoked: bool = Field(default=False, strict=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        normalized = _as_utc(value)
        assert normalized is not None
        return normalized


class CredentialRequirement(BaseModel):
    """One credential slot declared by a trusted tool catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: str = Field(
        strict=True,
        min_length=1,
        max_length=32,
        pattern=CREDENTIAL_SLOT_PATTERN,
    )
    provider: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=CREDENTIAL_PROVIDER_PATTERN,
    )


class CredentialRecord(CredentialView):
    """Private encrypted persistence model; never return it from an API."""

    reference: str = Field(
        default_factory=lambda: f"cred_{uuid.uuid4().hex}",
        pattern=CREDENTIAL_REFERENCE_PATTERN,
    )
    user_id: str = Field(strict=True, min_length=1, max_length=512)
    encrypted_secret: str | None = Field(
        default=None,
        min_length=1,
        max_length=16_384,
        repr=False,
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None

    @field_validator("updated_at", "revoked_at")
    @classmethod
    def normalize_private_datetimes(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_secret_lifecycle(self) -> "CredentialRecord":
        if not self.revoked and self.encrypted_secret is None:
            raise ValueError("active credential must contain encrypted material")
        if self.revoked and self.revoked_at is None:
            raise ValueError("revoked credential must contain a revocation time")
        return self

    def public_view(self) -> CredentialView:
        public_fields = set(CredentialView.model_fields)
        return CredentialView.model_validate(
            self.model_dump(include=public_fields, mode="python")
        )


def to_view(record: CredentialRecord) -> CredentialView:
    return record.public_view()


__all__ = [
    "CREDENTIAL_PROVIDER_PATTERN",
    "CREDENTIAL_REFERENCE_PATTERN",
    "CREDENTIAL_SLOT_PATTERN",
    "CREDENTIAL_TOOL_NAME_PATTERN",
    "CredentialErrorCode",
    "CredentialRecord",
    "CredentialRequirement",
    "CredentialView",
    "to_view",
]
