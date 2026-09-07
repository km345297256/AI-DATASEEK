import asyncio
from functools import lru_cache

from app.core.config import get_settings
from app.domain.models.model_trace import ModelTraceRecord
from app.infrastructure.models.model_trace import ModelTraceDocument


class MongoModelTraceRepository:
    async def put(self, record: ModelTraceRecord) -> None:
        async with asyncio.timeout(get_settings().model_trace_store_timeout_seconds):
            await ModelTraceDocument.get_pymongo_collection().update_one(
                {"trace_id": record.trace_id, "user_id": record.user_id, "session_id": record.session_id},
                {"$set": {"record": record.model_dump(mode="python")}}, upsert=True,
            )

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        task_id: str | None = None,
        limit: int = 100,
        before: str | None = None,
    ):
        query = {"user_id": user_id, "session_id": session_id}
        if task_id is not None:
            query["record.task_id"] = task_id
        async with asyncio.timeout(get_settings().model_trace_store_timeout_seconds):
            if before is not None:
                cursor = await ModelTraceDocument.find_one({
                    "trace_id": before,
                    "user_id": user_id,
                    "session_id": session_id,
                })
                # Missing and inaccessible cursors deliberately have the same
                # empty result so this lookup cannot reveal another owner.
                if cursor is None:
                    return []
                query["$or"] = [
                    {"record.created_at": {"$lt": cursor.record.created_at}},
                    {
                        "record.created_at": cursor.record.created_at,
                        "trace_id": {"$lt": cursor.trace_id},
                    },
                ]
            documents = await ModelTraceDocument.find(query).sort(
                "-record.created_at",
                "-trace_id",
            ).limit(min(max(limit, 1), 200)).to_list()
        return [document.record.public_view() for document in documents]

    async def delete_session(self, session_id: str) -> None:
        async with asyncio.timeout(get_settings().model_trace_store_timeout_seconds):
            await ModelTraceDocument.get_pymongo_collection().delete_many({"session_id": session_id})


@lru_cache()
def get_model_trace_repository() -> MongoModelTraceRepository:
    return MongoModelTraceRepository()
