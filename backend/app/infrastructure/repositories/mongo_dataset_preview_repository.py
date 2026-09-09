"""Small expiring references, separate from uploaded files and dataset bytes."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb


class MongoDatasetPreviewRepository:
    def __init__(self, collection=None):
        self._collection = collection
        self._index_ready = False
        self._index_lock = asyncio.Lock()

    @property
    def collection(self):
        if self._collection is not None:
            return self._collection
        return get_mongodb().client[get_settings().mongodb_database]["dataset_preview_references"]

    async def put(self, file_id: str, values: dict) -> dict:
        async with self._index_lock:
            if not self._index_ready:
                await self.collection.create_index("expires_at", expireAfterSeconds=0)
                self._index_ready = True
        # A deterministic owner-scoped ID makes repeated clicks idempotent.
        # No source path, byte copy, upload, or chat/session is persisted here.
        row = {**values, "expires_at": datetime.now(UTC) + timedelta(days=7)}
        await self.collection.update_one({"_id": file_id}, {"$set": row}, upsert=True)
        return {"_id": file_id, **row}

    async def get(self, file_id: str) -> dict | None:
        return await self.collection.find_one({"_id": file_id, "expires_at": {"$gt": datetime.now(UTC)}})
