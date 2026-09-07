from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from app.domain.models.tool_approval import ToolApprovalRecord, ToolApprovalStatus


class ToolApprovalRepository(Protocol):
    """Atomic persistence boundary for invocation-level approvals."""

    async def insert(self, record: ToolApprovalRecord) -> ToolApprovalRecord:
        ...

    async def find_by_id(self, approval_id: str) -> ToolApprovalRecord | None:
        ...

    async def find_for_owner(
        self,
        user_id: str,
        session_id: str,
        approval_id: str,
    ) -> ToolApprovalRecord | None:
        ...

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int,
    ) -> list[ToolApprovalRecord]:
        ...

    async def list_unconsumed_by_runtime(
        self,
        runtime_id: str,
        *,
        limit: int,
    ) -> list[ToolApprovalRecord]:
        ...

    async def list_expired_unconsumed(
        self,
        before: datetime,
        *,
        limit: int,
    ) -> list[ToolApprovalRecord]:
        ...

    async def compare_and_set(
        self,
        approval_id: str,
        *,
        expected_revision: int,
        expected_statuses: frozenset[ToolApprovalStatus],
        updates: Mapping[str, Any],
        expected_user_id: str | None = None,
        expected_session_id: str | None = None,
        expected_runtime_id: str | None = None,
        expires_at_lte: datetime | None = None,
        expires_at_gt: datetime | None = None,
    ) -> ToolApprovalRecord | None:
        ...

    async def consume_approved(
        self,
        approval_id: str,
        *,
        expected_user_id: str,
        expected_session_id: str,
        expected_runtime_id: str,
        expected_call_digest: str,
        unexpired_at: datetime,
    ) -> ToolApprovalRecord | None:
        """Atomically consume one matching, unexpired approved invocation."""
        ...

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        ...


__all__ = ["ToolApprovalRepository"]
