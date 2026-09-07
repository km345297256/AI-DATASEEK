from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel
from pymongo import ReturnDocument

from app.domain.models.analysis_job import (
    ANALYSIS_JOB_ACTIVE_STATUSES,
    AnalysisJobRecord,
    AnalysisJobStatus,
)
from app.domain.repositories.analysis_job_repository import AnalysisJobRepository
from app.infrastructure.models.analysis_job import AnalysisJobDocument


_MUTABLE_FIELDS = frozenset({
    "status",
    "started_at",
    "finished_at",
    "cancel_requested_at",
    "lease_expires_at",
    "result_spill",
    "error_code",
})


def _mongo_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Enum):
        return value.value
    return value


def _to_domain(document: dict[str, Any] | None) -> AnalysisJobRecord | None:
    if document is None:
        return None
    document.pop("_id", None)
    return AnalysisJobRecord.model_validate(document)


class MongoAnalysisJobRepository(AnalysisJobRepository):
    async def insert(self, record: AnalysisJobRecord) -> AnalysisJobRecord:
        document = AnalysisJobDocument.from_domain(record)
        await document.insert()
        return document.to_domain()

    async def find_by_id(self, job_id: str) -> AnalysisJobRecord | None:
        document = await AnalysisJobDocument.find_one(
            AnalysisJobDocument.job_id == job_id
        )
        return document.to_domain() if document is not None else None

    async def find_for_owner(
        self,
        user_id: str,
        session_id: str,
        job_id: str,
    ) -> AnalysisJobRecord | None:
        document = await AnalysisJobDocument.find_one(
            AnalysisJobDocument.job_id == job_id,
            AnalysisJobDocument.user_id == user_id,
            AnalysisJobDocument.session_id == session_id,
        )
        return document.to_domain() if document is not None else None

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int,
    ) -> list[AnalysisJobRecord]:
        documents = await AnalysisJobDocument.find(
            AnalysisJobDocument.user_id == user_id,
            AnalysisJobDocument.session_id == session_id,
        ).sort("-created_at", "-job_id").limit(max(1, min(limit, 1000))).to_list()
        return [document.to_domain() for document in documents]

    async def list_active_by_runtime(
        self,
        runtime_id: str,
        *,
        limit: int,
    ) -> list[AnalysisJobRecord]:
        documents = await AnalysisJobDocument.find({
            "runtime_id": runtime_id,
            "status": {"$in": [status.value for status in ANALYSIS_JOB_ACTIVE_STATUSES]},
        }).sort("+lease_expires_at", "+job_id").limit(
            max(1, min(limit, 1000))
        ).to_list()
        return [document.to_domain() for document in documents]

    async def list_expired_active(
        self,
        before: datetime,
        *,
        limit: int,
    ) -> list[AnalysisJobRecord]:
        documents = await AnalysisJobDocument.find({
            "status": {"$in": [status.value for status in ANALYSIS_JOB_ACTIVE_STATUSES]},
            "lease_expires_at": {"$lte": before},
        }).sort("+lease_expires_at", "+job_id").limit(
            max(1, min(limit, 1000))
        ).to_list()
        return [document.to_domain() for document in documents]

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
        if not expected_statuses:
            raise ValueError("analysis job CAS requires an expected status")
        unexpected = set(updates) - _MUTABLE_FIELDS
        if unexpected:
            raise ValueError("analysis job CAS contains immutable fields")
        query: dict[str, Any] = {
            "job_id": job_id,
            "revision": expected_revision,
            "status": {"$in": [status.value for status in expected_statuses]},
        }
        if expected_runtime_id is not None:
            query["runtime_id"] = expected_runtime_id
        if lease_expires_at_lte is not None:
            query["lease_expires_at"] = {"$lte": lease_expires_at_lte}
        document = await AnalysisJobDocument.get_pymongo_collection().find_one_and_update(
            query,
            {
                "$set": {key: _mongo_value(value) for key, value in updates.items()},
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return _to_domain(document)

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        await AnalysisJobDocument.get_pymongo_collection().delete_many({
            "user_id": user_id,
            "session_id": session_id,
        })


__all__ = ["MongoAnalysisJobRepository"]
