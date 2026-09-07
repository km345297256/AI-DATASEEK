from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.domain.models.credential import CredentialRecord
from app.domain.repositories.credential_repository import (
    CredentialRepository,
    CredentialRepositoryConflictError,
)
from app.infrastructure.models.credential import CredentialDocument


def _to_domain(document: dict[str, Any] | None) -> CredentialRecord | None:
    if document is None:
        return None
    document.pop("_id", None)
    return CredentialRecord.model_validate(document)


class MongoCredentialRepository(CredentialRepository):
    async def insert(self, record: CredentialRecord) -> CredentialRecord:
        document = CredentialDocument.from_domain(record)
        try:
            await document.insert()
        except DuplicateKeyError:
            raise CredentialRepositoryConflictError() from None
        return document.to_domain()

    async def find_for_owner(
        self,
        user_id: str,
        reference: str,
    ) -> CredentialRecord | None:
        document = await CredentialDocument.find_one(
            CredentialDocument.user_id == user_id,
            CredentialDocument.reference == reference,
        )
        return document.to_domain() if document is not None else None

    async def find_active_binding(
        self,
        user_id: str,
        tool_name: str,
        slot: str,
    ) -> CredentialRecord | None:
        document = await CredentialDocument.find_one(
            CredentialDocument.user_id == user_id,
            CredentialDocument.tool_name == tool_name,
            CredentialDocument.slot == slot,
            CredentialDocument.revoked == False,  # noqa: E712
        )
        return document.to_domain() if document is not None else None

    async def list_for_owner(
        self,
        user_id: str,
        *,
        limit: int,
    ) -> list[CredentialRecord]:
        documents = await CredentialDocument.find(
            CredentialDocument.user_id == user_id
        ).sort("-created_at", "-reference").limit(
            max(1, min(limit, 200))
        ).to_list()
        return [document.to_domain() for document in documents]

    async def compare_and_set_revoke(
        self,
        user_id: str,
        reference: str,
        *,
        expected_revision: int,
    ) -> CredentialRecord | None:
        now = datetime.now(UTC)
        document = await CredentialDocument.get_pymongo_collection().find_one_and_update(
            {
                "user_id": user_id,
                "reference": reference,
                "revision": expected_revision,
                "revoked": False,
            },
            {
                "$set": {
                    "revoked": True,
                    "revoked_at": now,
                    "updated_at": now,
                },
                "$unset": {"encrypted_secret": ""},
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return _to_domain(document)


__all__ = ["MongoCredentialRepository"]
