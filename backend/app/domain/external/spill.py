from __future__ import annotations

from typing import Protocol

from app.domain.models.spill import (
    SpillArtifactChunk,
    SpillArtifactOwner,
    SpillArtifactRef,
    SpillArtifactSaveRequest,
    SpillImageSaveRequest,
    SpillImageContent,
)


class SpillArtifactStore(Protocol):
    """Backend-neutral capability for private spilled tool results."""

    async def save_text(self, request: SpillArtifactSaveRequest) -> SpillArtifactRef:
        """Persist the complete text and return an opaque reference."""
        ...

    async def save_image(self, request: SpillImageSaveRequest) -> SpillArtifactRef:
        """Store accepted normalized image bytes under the same private lifecycle."""
        ...

    async def read_image(self, locator: str, owner: SpillArtifactOwner, *, max_bytes: int) -> SpillImageContent:
        """Read a complete bounded image after checking user, session and integrity."""
        ...

    async def read_text(
        self,
        locator: str,
        owner: SpillArtifactOwner,
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> SpillArtifactChunk:
        """Read one bounded UTF-8 page after enforcing the owner boundary."""
        ...

    async def delete_owner(self, owner: SpillArtifactOwner) -> int:
        """Make every spill owned by a deleted session inaccessible."""
        ...

    async def reap_expired(self, *, limit: int = 100) -> int:
        """Delete a bounded batch whose backend-specific retention expired."""
        ...
