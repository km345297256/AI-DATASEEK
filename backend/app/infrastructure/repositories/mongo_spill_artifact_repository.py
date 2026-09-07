from __future__ import annotations

from datetime import UTC, datetime

from pymongo import ReturnDocument

from app.domain.models.spill import SpillArtifactRecord
from app.domain.repositories.spill_artifact_repository import SpillArtifactRepository
from app.infrastructure.models.documents import SpillArtifactDocument


class MongoSpillArtifactRepository(SpillArtifactRepository):
    """Beanie-backed private spill locator index."""

    async def save(self, record: SpillArtifactRecord) -> None:
        await SpillArtifactDocument.from_domain(record).insert()

    async def find_by_artifact_id(
        self,
        artifact_id: str,
    ) -> SpillArtifactRecord | None:
        document = await SpillArtifactDocument.find_one(
            SpillArtifactDocument.artifact_id == artifact_id
        )
        return document.to_domain() if document else None

    async def list_by_owner(
        self,
        owner_user_id: str,
        owner_session_id: str,
    ) -> list[SpillArtifactRecord]:
        documents = await SpillArtifactDocument.find(
            SpillArtifactDocument.owner_user_id == owner_user_id,
            SpillArtifactDocument.owner_session_id == owner_session_id,
        ).sort("+created_at").to_list()
        return [document.to_domain() for document in documents]

    async def list_expired(
        self,
        before: datetime,
        *,
        limit: int,
    ) -> list[SpillArtifactRecord]:
        documents = await SpillArtifactDocument.find({
            "$or": [
                {"status": "deleting"},
                {"expires_at": {"$lte": before}},
            ],
        }).sort(
            "+cleanup_attempted_at",
            "+expires_at",
            "+artifact_id",
        ).limit(max(1, limit)).to_list()
        return [document.to_domain() for document in documents]

    async def mark_deleting(
        self,
        artifact_id: str,
        *,
        expected_storage_file_id: str,
    ) -> SpillArtifactRecord | None:
        document = await SpillArtifactDocument.get_pymongo_collection().find_one_and_update(
            {
                "artifact_id": artifact_id,
                "storage_file_id": expected_storage_file_id,
            },
            {
                "$set": {
                    "status": "deleting",
                    "cleanup_attempted_at": datetime.now(UTC),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            return None
        document.pop("_id", None)
        return SpillArtifactRecord.model_validate(document)

    async def delete(
        self,
        artifact_id: str,
        *,
        expected_storage_file_id: str,
    ) -> None:
        await SpillArtifactDocument.get_pymongo_collection().delete_one({
            "artifact_id": artifact_id,
            "storage_file_id": expected_storage_file_id,
        })
