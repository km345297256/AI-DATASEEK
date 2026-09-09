"""Private original-request identity plus one unique document per physical call.

Unlimited production metering never grows reservation maps in the lineage
document. A unique insert is the only execution admission, and usage is derived
from those records instead of a non-atomic cross-document counter. Settlement
is an idempotent single-document update. No TTL may erase a replay tombstone
while its original request remains addressable. Finite CAS is test-only.
"""
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.infrastructure.models.documents import SessionDocument


class MongoAnalysisBudgetRepository:
    def __init__(self, collection=None, operations_collection=None):
        self._collection_override = collection
        self._operations_override = operations_collection

    @property
    def collection(self):
        if self._collection_override is not None:
            return self._collection_override
        return SessionDocument.get_pymongo_collection().database["analysis_budgets"]

    @property
    def operations(self):
        if self._operations_override is not None:
            return self._operations_override
        return self.collection.database["analysis_usage_operations"]

    async def get(self, lineage_id: str) -> dict | None:
        return await self.collection.find_one({"_id": lineage_id})

    async def create_if_absent(self, document: dict) -> dict:
        try:
            await self.collection.insert_one(document)
            return document
        except DuplicateKeyError:
            found = await self.get(document["_id"])
            if found is None:
                raise RuntimeError("budget_creation_unconfirmed") from None
            return found

    async def compare_and_swap(self, lineage_id: str, version: int, document: dict, *, require_before_deadline: bool = False) -> bool:
        # Replace only this independent ledger, never the session or events.
        query = {"_id": lineage_id, "version": version,
                 "owner_id": document["owner_id"], "session_id": document["session_id"],
                 "scope_digest": document["scope_digest"]}
        if require_before_deadline:
            # Use the database's execution time, not the client's earlier read.
            # Settlement uses the same CAS without this gate: recording actual
            # usage after expiry grants no new permission.
            query["$expr"] = {"$gt": ["$deadline_at", "$$NOW"]}
        result = await self.collection.replace_one(query, document)
        return result.matched_count == 1

    async def reserve_operation(self, document: dict) -> tuple[dict, bool]:
        try:
            await self.operations.insert_one(document)
            return document, True
        except DuplicateKeyError:
            found = await self.operations.find_one({"_id": document["_id"]})
            if found is None:
                raise RuntimeError("usage_reservation_unconfirmed") from None
            return found, False

    async def aggregate_usage(self, lineage_id: str, user_id: str, session_id: str) -> dict:
        # The deterministic ID prefix uses the built-in _id index: no full
        # collection scan, extra index setup, or growing in-memory ID list.
        cursor = await self.operations.aggregate([
            {"$match": {"_id": {"$gte": lineage_id + ":", "$lt": lineage_id + ";"},
                        "lineage_id": lineage_id, "owner_id": user_id, "session_id": session_id,
                        "allowed": True}},
            {"$group": {"_id": None,
                "tool_batches_used": {"$sum": {"$cond": [{"$eq": ["$kind", "tool"]}, 1, 0]}},
                "model_calls": {"$sum": {"$cond": [{"$eq": ["$kind", "model"]}, 1, 0]}},
                "charged_tokens": {"$sum": {"$cond": [{"$eq": ["$kind", "model"]},
                    {"$ifNull": ["$actual_tokens", "$reserved_tokens"]}, 0]}},
            }},
        ], maxTimeMS=2500)
        rows = await cursor.to_list(length=1)
        row = rows[0] if rows else {}
        return {key: row.get(key, 0) for key in ("tool_batches_used", "model_calls", "charged_tokens")}

    async def settle_operation(self, operation_id: str, lineage_id: str, user_id: str,
                               session_id: str, actual_tokens: int | None) -> dict | None:
        identity = {"_id": operation_id, "lineage_id": lineage_id, "owner_id": user_id,
                    "session_id": session_id, "kind": "model", "allowed": True}
        if actual_tokens is not None:
            settled = await self.operations.find_one_and_update(
                {**identity, "actual_tokens": None}, {"$set": {"actual_tokens": actual_tokens}},
                return_document=ReturnDocument.AFTER)
            if settled is not None:
                return settled
        # Missing usage remains reserved. An already settled different value is
        # returned for the service to reject, never overwritten or double-added.
        return await self.operations.find_one(identity)
