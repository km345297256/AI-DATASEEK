from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from app.domain.models.analysis_job import AnalysisJobRecord, AnalysisJobStatus


class AnalysisJobRepository(Protocol):
    """Atomic persistence boundary for AnalysisJob lifecycle transitions."""

    async def insert(self, record: AnalysisJobRecord) -> AnalysisJobRecord:
        ...

    async def find_by_id(self, job_id: str) -> AnalysisJobRecord | None:
        ...

    async def find_for_owner(
        self,
        user_id: str,
        session_id: str,
        job_id: str,
    ) -> AnalysisJobRecord | None:
        ...

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int,
    ) -> list[AnalysisJobRecord]:
        ...

    async def list_active_by_runtime(
        self,
        runtime_id: str,
        *,
        limit: int,
    ) -> list[AnalysisJobRecord]:
        ...

    async def list_expired_active(
        self,
        before: datetime,
        *,
        limit: int,
    ) -> list[AnalysisJobRecord]:
        ...

    async def compare_and_set(
        self,
        job_id: str,
        *,
        expected_revision: int,
        expected_statuses: frozenset[AnalysisJobStatus],
        updates: Mapping[str, Any],
        expected_runtime_id: str | None = None,
        lease_expires_at_lte: datetime | None = None,
    ) -> AnalysisJobRecord | None:
        ...

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        ...


__all__ = ["AnalysisJobRepository"]
