from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel
from pymongo import ReturnDocument

from app.domain.models.tool_approval import (
    TOOL_APPROVAL_UNCONSUMED_STATUSES,
    ToolApprovalRecord,
    ToolApprovalStatus,
)
from app.domain.repositories.tool_approval_repository import ToolApprovalRepository
from app.infrastructure.models.tool_approval import ToolApprovalDocument


_MUTABLE_FIELDS = frozenset({"status", "decided_at"})


def _mongo_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Enum):
        return value.value
    return value


def _to_domain(document: dict[str, Any] | None) -> ToolApprovalRecord | None:
    if document is None:
        return None
    document.pop("_id", None)
    return ToolApprovalRecord.model_validate(document)


class MongoToolApprovalRepository(ToolApprovalRepository):
    async def insert(self, record: ToolApprovalRecord) -> ToolApprovalRecord:
        document = ToolApprovalDocument.from_domain(record)
        await document.insert()
        return document.to_domain()

    async def find_by_id(self, approval_id: str) -> ToolApprovalRecord | None:
        document = await ToolApprovalDocument.find_one(
            ToolApprovalDocument.approval_id == approval_id
        )
        return document.to_domain() if document is not None else None

    async def find_for_owner(
        self,
        user_id: str,
        session_id: str,
        approval_id: str,
    ) -> ToolApprovalRecord | None:
        document = await ToolApprovalDocument.find_one(
            ToolApprovalDocument.approval_id == approval_id,
            ToolApprovalDocument.user_id == user_id,
            ToolApprovalDocument.session_id == session_id,
        )
        return document.to_domain() if document is not None else None

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int,
    ) -> list[ToolApprovalRecord]:
        documents = await ToolApprovalDocument.find(
            ToolApprovalDocument.user_id == user_id,
            ToolApprovalDocument.session_id == session_id,
        ).sort("-created_at", "-approval_id").limit(
            max(1, min(limit, 1000))
        ).to_list()
        return [document.to_domain() for document in documents]

    async def list_unconsumed_by_runtime(
        self,
        runtime_id: str,
        *,
        limit: int,
    ) -> list[ToolApprovalRecord]:
        documents = await ToolApprovalDocument.find({
            "runtime_id": runtime_id,
            "status": {
                "$in": [status.value for status in TOOL_APPROVAL_UNCONSUMED_STATUSES]
            },
        }).sort("+expires_at", "+approval_id").limit(
            max(1, min(limit, 1000))
        ).to_list()
        return [document.to_domain() for document in documents]

    async def list_expired_unconsumed(
        self,
        before: datetime,
        *,
        limit: int,
    ) -> list[ToolApprovalRecord]:
        documents = await ToolApprovalDocument.find({
            "status": {
                "$in": [status.value for status in TOOL_APPROVAL_UNCONSUMED_STATUSES]
            },
            "expires_at": {"$lte": before},
        }).sort("+expires_at", "+approval_id").limit(
            max(1, min(limit, 1000))
        ).to_list()
        return [document.to_domain() for document in documents]

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
        if not expected_statuses:
            raise ValueError("tool approval CAS requires an expected status")
        unexpected = set(updates) - _MUTABLE_FIELDS
        if unexpected:
            raise ValueError("tool approval CAS contains immutable fields")
        if expires_at_lte is not None and expires_at_gt is not None:
            raise ValueError("tool approval CAS expiry predicates conflict")

        query: dict[str, Any] = {
            "approval_id": approval_id,
            "revision": expected_revision,
            "status": {"$in": [status.value for status in expected_statuses]},
        }
        if expected_user_id is not None:
            query["user_id"] = expected_user_id
        if expected_session_id is not None:
            query["session_id"] = expected_session_id
        if expected_runtime_id is not None:
            query["runtime_id"] = expected_runtime_id
        if expires_at_lte is not None:
            query["expires_at"] = {"$lte": expires_at_lte}
        if expires_at_gt is not None:
            query["expires_at"] = {"$gt": expires_at_gt}

        document = await ToolApprovalDocument.get_pymongo_collection().find_one_and_update(
            query,
            {
                "$set": {key: _mongo_value(value) for key, value in updates.items()},
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return _to_domain(document)

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
        document = await ToolApprovalDocument.get_pymongo_collection().find_one_and_update(
            {
                "approval_id": approval_id,
                "user_id": expected_user_id,
                "session_id": expected_session_id,
                "runtime_id": expected_runtime_id,
                "call_digest": expected_call_digest,
                "status": ToolApprovalStatus.APPROVED.value,
                "expires_at": {"$gt": unexpired_at},
            },
            {
                "$set": {"status": ToolApprovalStatus.CONSUMED.value},
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return _to_domain(document)

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        await ToolApprovalDocument.get_pymongo_collection().delete_many({
            "user_id": user_id,
            "session_id": session_id,
        })


__all__ = ["MongoToolApprovalRepository"]
