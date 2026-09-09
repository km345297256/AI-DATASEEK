"""One preference document per owner; atomic field writes avoid lost updates."""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb


class MongoVisualizationRepository:
    def __init__(self, collection: Any = None):
        self._collection = collection

    @property
    def collection(self):
        if self._collection is not None:
            return self._collection
        return get_mongodb().client[get_settings().mongodb_database]["visualization_preferences"]

    async def get_states(self, user_id: str) -> dict[str, bool]:
        row = await self.collection.find_one({"_id": user_id}, {"states": 1})
        if not row or not isinstance(row.get("states"), dict):
            return {}
        return {key: value for key, value in row["states"].items() if type(value) is bool}

    async def set_state(self, user_id: str, plugin_id: str, enabled: bool) -> None:
        # Defense in depth: Mongo field names cannot be supplied by a caller.
        import re
        if not user_id or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", plugin_id) or type(enabled) is not bool:
            raise ValueError("Invalid visualization preference")
        await self.collection.update_one(
            {"_id": user_id}, {"$set": {f"states.{plugin_id}": enabled}}, upsert=True,
        )
