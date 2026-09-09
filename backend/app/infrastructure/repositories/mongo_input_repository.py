from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.domain.models.event import MessageEvent
from app.domain.models.input_admission import AcceptedInput, InputAdmission, input_key, terminal_event_id
from app.infrastructure.models.documents import SessionDocument, SessionEventDocument, UserDocument


class MongoInputRepository:
    def __init__(self, session_repository):
        self._sessions = session_repository

    @staticmethod
    def _record(document: dict | None) -> AcceptedInput | None:
        if not document or not document.get("input_admission"):
            return None
        return AcceptedInput(session_id=document["session_id"], key=document["producer_event_key"],
                             event=MessageEvent.model_validate(document["event"]),
                             admission=InputAdmission.model_validate(document["input_admission"]))

    async def accept(self, session_id: str, actor_user_id: str, event: MessageEvent, *, generation: int | None = None) -> AcceptedInput | None:
        if generation is None:
            generation = await self.generation(session_id)
        await self._sessions.add_input_event(session_id, event, InputAdmission(actor_user_id=actor_user_id,
                                                                             session_generation=generation))
        # An existing pre-upgrade event is deliberately not made executable.
        return await self.get(session_id, input_key(event))

    async def get(self, session_id: str, key: str) -> AcceptedInput | None:
        return self._record(await SessionEventDocument.get_pymongo_collection().find_one(
            {"session_id": session_id, "producer_event_key": key}))

    async def claim(self, record: AcceptedInput, runtime_id: str, expires: datetime) -> AcceptedInput | None:
        if record.admission.state != "pending":
            return None
        collection = SessionEventDocument.get_pymongo_collection()
        # Do not overtake a previously accepted turn in the same session.
        earlier = await collection.find_one({"session_id": record.session_id, "seq": {"$lt": record.event.seq},
            "input_admission.state": {"$in": ["pending", "claimed", "running"]}}, {"_id": 1})
        if earlier:
            return None
        try:
            return await self.transition(record, {"state": "claimed", "runtime_id": runtime_id,
                "lease_expires_at": expires, "attempts": record.admission.attempts + 1})
        except DuplicateKeyError:
            # The partial unique index elects one active input per session.
            return None

    async def transition(self, record: AcceptedInput, updates: dict, *, require_live: bool = False) -> AcceptedInput | None:
        admission = record.admission
        query = {"session_id": record.session_id, "producer_event_key": record.key,
                 "input_admission.revision": admission.revision, "input_admission.state": admission.state,
                 "input_admission.runtime_id": admission.runtime_id}
        if require_live:
            query["input_admission.lease_expires_at"] = {"$gt": datetime.now(UTC)}
        values = {f"input_admission.{key}": value for key, value in updates.items()}
        state = updates.get("state", admission.state)
        operation = {"$set": values, "$inc": {"input_admission.revision": 1}}
        if state in {"claimed", "running"}:
            values["input_active_session"] = record.session_id
        else:
            operation["$unset"] = {"input_active_session": ""}
        document = await SessionEventDocument.get_pymongo_collection().find_one_and_update(
            query, operation, return_document=ReturnDocument.AFTER)
        return self._record(document)

    async def candidates(self, now: datetime, limit: int = 100) -> list[AcceptedInput]:
        cursor = SessionEventDocument.get_pymongo_collection().find({"$or": [
            {"input_admission.state": "pending", "input_admission.retry_after": {"$lte": now}},
            {"input_admission.state": {"$in": ["claimed", "running"]}, "input_admission.lease_expires_at": {"$lte": now}},
            {"input_admission.state": {"$in": ["interrupted", "cancelled"]}, "input_admission.notified": False},
        ]}).sort([("created_at", 1), ("seq", 1)]).limit(limit)
        return [record for doc in await cursor.to_list(length=limit) if (record := self._record(doc)) is not None]

    async def unsettled(self, session_id: str) -> AcceptedInput | None:
        return self._record(await SessionEventDocument.get_pymongo_collection().find_one(
            {"session_id": session_id, "$or": [
                {"input_admission.state": {"$in": ["pending", "claimed", "running"]}},
                {"input_admission.state": {"$in": ["interrupted", "cancelled"]}, "input_admission.notified": False},
            ]}, sort=[("seq", 1)]))

    async def latest(self, session_id: str) -> AcceptedInput | None:
        return self._record(await SessionEventDocument.get_pymongo_collection().find_one(
            {"session_id": session_id, "input_admission": {"$type": "object"}}, sort=[("seq", -1)]))

    async def terminal_kind(self, record: AcceptedInput) -> str | None:
        identities = {hashlib.sha256(terminal_event_id(record.key, kind).encode()).hexdigest(): kind
                      for kind in ("done", "wait", "error")}
        document = await SessionEventDocument.get_pymongo_collection().find_one(
            {"session_id": record.session_id, "producer_event_key": {"$in": list(identities)}},
            {"producer_event_key": 1})
        return identities[document["producer_event_key"]] if document else None

    async def matches_terminal(self, session_id: str, key: str, seq: int, kind: str) -> bool:
        identity = hashlib.sha256(terminal_event_id(key, kind).encode()).hexdigest()
        return await SessionEventDocument.get_pymongo_collection().find_one(
            {"session_id": session_id, "producer_event_key": identity, "seq": seq}, {"_id": 1}) is not None

    async def authorized(self, record: AcceptedInput) -> bool:
        actor = record.admission.actor_user_id
        generation = record.admission.session_generation
        generation_filter = ({"input_generation": generation} if generation else
            {"$or": [{"input_generation": 0}, {"input_generation": {"$exists": False}}]})
        session = await SessionDocument.get_pymongo_collection().find_one({"session_id": record.session_id,
            "input_delivery_disabled": {"$ne": True}, "$and": [generation_filter,
                {"$or": [{"user_id": actor}, {"collaborator_user_ids": actor}]}]}, {"_id": 1})
        if not session:
            return False
        user = await UserDocument.get_pymongo_collection().find_one({"user_id": actor,
            "is_active": {"$ne": False}, "registration_status": {"$nin": ["pending", "rejected"]}}, {"role": 1})
        if not user:
            return False
        return not (record.event.metadata or {}).get("mcp_access_all") or user.get("role") == "admin"

    async def cancel_session(self, session_id: str) -> None:
        # Fence requests that were waiting for an old runner or an attachment
        # lookup when Stop was pressed, including requests in another process.
        await SessionDocument.get_pymongo_collection().update_one({"session_id": session_id},
            {"$inc": {"input_generation": 1}})
        await SessionEventDocument.get_pymongo_collection().update_many(
            {"session_id": session_id, "input_admission.state": {"$in": ["pending", "claimed", "running"]}},
            {"$set": {"input_admission.state": "cancelled", "input_admission.lease_expires_at": datetime.now(UTC),
                      "input_admission.notified": False}, "$inc": {"input_admission.revision": 1},
             "$unset": {"input_active_session": ""}})

    async def generation(self, session_id: str) -> int:
        session = await SessionDocument.get_pymongo_collection().find_one({"session_id": session_id}, {"input_generation": 1})
        if session is None:
            raise RuntimeError("Session not found")
        return int(session.get("input_generation", 0))

    async def disable_session(self, session_id: str) -> None:
        # Durable deletion barrier: a monitor in another process must not
        # resurrect pending work while the owner removes the sandbox/files.
        await SessionDocument.get_pymongo_collection().update_one({"session_id": session_id},
            {"$set": {"input_delivery_disabled": True}})
