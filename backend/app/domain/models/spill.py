from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


SPILL_LOCATOR_PREFIX = "spill://artifact/"
SPILL_LOCATOR_PATTERN = r"^spill://artifact/[0-9a-f]{32}$"


class SpillArtifactOwner(BaseModel):
    """Private ownership boundary for one spilled tool result."""

    user_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=512)


class SpillArtifactSource(BaseModel):
    """Diagnostic provenance supplied to a store, never used for access control."""

    tool_name: str = Field(default="", max_length=512)
    tool_call_id: str = Field(default="", max_length=512)
    label: str = Field(default="tool-output", max_length=128)


class SpillArtifactSaveRequest(BaseModel):
    """Complete UTF-8 text passed across the storage capability seam."""

    owner: SpillArtifactOwner
    source: SpillArtifactSource
    content: str
    media_type: str = Field(default="text/plain; charset=utf-8", max_length=128)


class SpillArtifactRef(BaseModel):
    """Model-safe reference to a private, durable spill artifact."""

    schema_version: Literal[1] = 1
    locator: str = Field(pattern=SPILL_LOCATOR_PATTERN)
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(max_length=128)
    retrieval_hint: str = Field(max_length=512)


class SpillArtifactChunk(BaseModel):
    """A bounded UTF-8 page returned to an Agent."""

    schema_version: Literal[1] = 1
    locator: str = Field(pattern=SPILL_LOCATOR_PATTERN)
    content: str
    start_byte: int = Field(ge=0)
    next_byte: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    eof: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(max_length=128)


class SpillArtifactNotice(BaseModel):
    """Bounded replacement shared by the model, SSE projection, and replay."""

    schema_version: Literal[1] = 1
    status: Literal["stored", "unavailable"]
    reference: SpillArtifactRef | None = None
    preview: str
    original_bytes: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)
    omitted_bytes: int = Field(ge=0)


class SpillArtifactRecord(BaseModel):
    """Internal mapping from an opaque locator to the configured file store."""

    artifact_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    storage_file_id: str = Field(min_length=1, max_length=512)
    # A private storage principal distinct from the browser/API user id. Old
    # records may omit it and fall back to owner_user_id during migration.
    storage_user_id: str | None = Field(default=None, min_length=1, max_length=512)
    owner_user_id: str = Field(min_length=1, max_length=512)
    owner_session_id: str = Field(min_length=1, max_length=512)
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(max_length=128)
    source_tool_ref: str = Field(pattern=r"^tool:sha256:[0-9a-f]{12}$")
    source_call_ref: str = Field(pattern=r"^call:sha256:[0-9a-f]{12}$")
    # Cleanup tombstones created after an indeterminate metadata commit retain
    # the original locator id. The reaper must reconcile that mapping before
    # deciding whether the uploaded object is an orphan.
    reconcile_artifact_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
    )
    status: Literal["active", "deleting"] = "active"
    cleanup_attempted_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime

    @property
    def locator(self) -> str:
        return f"{SPILL_LOCATOR_PREFIX}{self.artifact_id}"
