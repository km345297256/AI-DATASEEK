from __future__ import annotations

from typing import Protocol

from app.domain.models.credential import CredentialRecord


class CredentialRepositoryConflictError(RuntimeError):
    """An active binding already occupies the requested owner/tool/slot."""


class CredentialRepository(Protocol):
    """Encrypted-record persistence boundary for the credential vault."""

    async def insert(self, record: CredentialRecord) -> CredentialRecord:
        ...

    async def find_for_owner(
        self,
        user_id: str,
        reference: str,
    ) -> CredentialRecord | None:
        ...

    async def find_active_binding(
        self,
        user_id: str,
        tool_name: str,
        slot: str,
    ) -> CredentialRecord | None:
        ...

    async def list_for_owner(
        self,
        user_id: str,
        *,
        limit: int,
    ) -> list[CredentialRecord]:
        ...

    async def compare_and_set_revoke(
        self,
        user_id: str,
        reference: str,
        *,
        expected_revision: int,
    ) -> CredentialRecord | None:
        ...


__all__ = ["CredentialRepository", "CredentialRepositoryConflictError"]
