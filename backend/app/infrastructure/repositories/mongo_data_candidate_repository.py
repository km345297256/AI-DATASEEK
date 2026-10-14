"""Append-only provenance records, partitioned by owner and session using the _id index."""
from pymongo.errors import DuplicateKeyError
from app.infrastructure.models.documents import SessionDocument
from app.domain.models.data_candidate import identity


class MongoDataCandidateRepository:
    def __init__(self, collection=None):
        self._collection = collection

    @property
    def collection(self):
        if self._collection is not None:
            return self._collection
        return SessionDocument.get_pymongo_collection().database["data_provenance"]

    @staticmethod
    def prefix(owner: str, session: str, kind: str) -> str:
        return identity([owner, session, kind]) + ":"

    async def put(self, owner: str, session: str, kind: str, record_id: str, payload: dict) -> dict:
        key = self.prefix(owner, session, kind) + record_id
        document = {"_id": key, "owner_id": owner, "session_id": session, "payload": payload}
        try:
            await self.collection.insert_one(document)
            return payload
        except DuplicateKeyError:
            existing = await self.get(owner, session, kind, record_id)
            if existing is None:
                raise RuntimeError("provenance_insert_unconfirmed") from None
            return existing

    async def get(self, owner: str, session: str, kind: str, record_id: str) -> dict | None:
        row = await self.collection.find_one({"_id": self.prefix(owner, session, kind) + record_id,
                                              "owner_id": owner, "session_id": session})
        return row["payload"] if row else None

    async def page(self, owner: str, session: str, kind: str, cursor: str | None = None) -> list[dict]:
        prefix = self.prefix(owner, session, kind)
        lower = {"$gt": prefix + cursor} if cursor else {"$gte": prefix}
        rows = await self.collection.find({"_id": {**lower, "$lt": prefix[:-1] + ";"},
                                            "owner_id": owner, "session_id": session}).sort("_id", 1).limit(21).to_list(length=21)
        return [row["payload"] for row in rows]


    async def save_acquisition(self, owner, session, accession, payload):
        # A replaceable pointer to a verified owned file, separate from immutable provenance.
        key = self.prefix(owner, session, "acquired_package") + accession
        await self.collection.replace_one(
            {"_id": key, "owner_id": owner, "session_id": session},
            {"_id": key, "owner_id": owner, "session_id": session, "payload": payload}, upsert=True)
