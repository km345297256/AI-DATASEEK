from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.models.spill import SpillArtifactRecord


class SpillArtifactRepository(Protocol):
    """Private locator index kept outside browser-visible session state."""

    async def save(self, record: SpillArtifactRecord) -> None:
        ...

    async def find_by_artifact_id(
        self,
        artifact_id: str,
    ) -> SpillArtifactRecord | None:
        ...

    async def list_by_owner(
        self,
        owner_user_id: str,
        owner_session_id: str,
    ) -> list[SpillArtifactRecord]:
        ...

    async def list_expired(
        self,
        before: datetime,
        *,
        limit: int,
    ) -> list[SpillArtifactRecord]:
        ...

    async def mark_deleting(
        self,
        artifact_id: str,
        *,
        expected_storage_file_id: str,
    ) -> SpillArtifactRecord | None:
        """CAS-revoke the expected mapping while retaining cleanup metadata."""
        ...

    async def delete(
        self,
        artifact_id: str,
        *,
        expected_storage_file_id: str,
    ) -> None:
        """Delete only when the locator still maps to the expected object."""
        ...
