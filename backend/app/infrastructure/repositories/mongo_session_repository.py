import asyncio
import hashlib
import hmac
import json
from typing import Optional, List
from datetime import datetime, UTC
from pymongo import ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError
from pydantic import TypeAdapter, ValidationError
from app.domain.models.session import Session, SessionStatus, SessionSummary
from app.domain.models.file import FileInfo
from app.domain.repositories.session_repository import SessionRepository
from app.domain.models.event import BaseEvent, AgentEvent, MAX_EVENT_SEQUENCE
from app.domain.models.execution_environment import ExecutionEnvironmentSnapshot
from app.domain.models.session_history import SessionHistoryPage, page_legacy_history
from app.domain.services.execution_history import ExecutionHistory
from app.infrastructure.models.documents import (
    ExecutionEnvironmentSnapshotDocument,
    SessionDocument,
    SessionEventDocument,
    SessionEventReservationDocument,
)
import logging

logger = logging.getLogger(__name__)

SESSION_EVENT_WRITE_ATTEMPTS = 3
SESSION_EVENT_RETRY_DELAYS = (0.1, 0.3)
CLIENT_MESSAGE_ID_HISTORY_LIMIT = 512
EXECUTION_HISTORY_CACHE_BYTES = 4 * 1024 * 1024

SESSION_LIST_PROJECTION = {
    "session_id": 1,
    "user_id": 1,
    "title": 1,
    "unread_message_count": 1,
    "latest_message": 1,
    "latest_message_at": 1,
    "status": 1,
    "is_shared": 1,
    "collaborator_user_ids": 1,
}

class MongoSessionRepository(SessionRepository):
    """MongoDB implementation of SessionRepository"""

    async def save_analysis_checkpoint(self, session_id: str, checkpoint: dict) -> None:
        source_seq = checkpoint.get("source_seq")
        if type(source_seq) is not int or not 0 < source_seq <= MAX_EVENT_SEQUENCE:
            raise ValueError("Checkpoint requires a durable source sequence")
        result = await SessionDocument.get_pymongo_collection().update_one(
            {"session_id": session_id, "user_id": checkpoint.get("owner_id"),
             "latest_user_event_seq": source_seq, "latest_user_input_fence_seq": source_seq},
            {"$set": {"analysis_checkpoint": checkpoint}})
        if not result.matched_count:
            raise ValueError("Checkpoint source is no longer the current accepted input")

    async def get_analysis_checkpoint(self, session_id: str, checkpoint_id: str) -> dict | None:
        document = await SessionDocument.get_pymongo_collection().find_one(
            {"session_id": session_id, "analysis_checkpoint.id": checkpoint_id,
             "analysis_checkpoint.expires_at": {"$gt": datetime.now(UTC)}},
            {"analysis_checkpoint": 1})
        return document.get("analysis_checkpoint") if document else None

    async def claim_analysis_checkpoint(self, session_id: str, checkpoint_id: str,
                                        user_id: str, client_message_id: str,
                                        *, expected_source_seq: int) -> dict | None:
        if type(expected_source_seq) is not int or not 0 < expected_source_seq <= MAX_EVENT_SEQUENCE:
            return None
        document = await SessionDocument.get_pymongo_collection().find_one_and_update(
            {"session_id": session_id, "user_id": user_id,
             "latest_user_event_seq": expected_source_seq,
             "latest_user_input_fence_seq": expected_source_seq,
             "analysis_checkpoint.id": checkpoint_id,
             "analysis_checkpoint.owner_id": user_id,
             "analysis_checkpoint.source_seq": expected_source_seq,
             "analysis_checkpoint.expires_at": {"$gt": datetime.now(UTC)},
             "analysis_checkpoint.claimed_by": {"$in": [None, client_message_id]}},
            {"$set": {"analysis_checkpoint.claimed_by": client_message_id}},
            return_document=ReturnDocument.AFTER)
        return document.get("analysis_checkpoint") if document else None

    async def is_analysis_checkpoint_current(self, session_id: str, checkpoint_id: str,
                                             user_id: str, client_message_id: str,
                                             *, source_seq: int, resume_event_seq: int) -> bool:
        """Recheck the claim against the current durable input immediately before use.

        Both markers share the session document with the claim. Any newer input
        starts fencing before its event commit, so no cross-collection gap can
        make a stale continuation look current. Runtime input-delivery guards
        still own cancellation if a new input arrives after this read.
        """
        if (type(source_seq) is not int or type(resume_event_seq) is not int
                or not 0 < source_seq < resume_event_seq <= MAX_EVENT_SEQUENCE):
            return False
        document = await SessionDocument.get_pymongo_collection().find_one(
            {"session_id": session_id, "user_id": user_id,
             "latest_user_event_seq": resume_event_seq,
             "latest_user_input_fence_seq": resume_event_seq,
             "analysis_checkpoint.id": checkpoint_id,
             "analysis_checkpoint.owner_id": user_id,
             "analysis_checkpoint.source_seq": source_seq,
             "analysis_checkpoint.claimed_by": client_message_id,
             "analysis_checkpoint.expires_at": {"$gt": datetime.now(UTC)}},
            {"_id": 1})
        return document is not None

    async def clear_analysis_checkpoint(self, session_id: str) -> None:
        # Admission cleanup must not erase a newer checkpoint saved by a fast
        # worker between the accepted event and this cleanup call.
        await SessionDocument.get_pymongo_collection().update_one(
            {"session_id": session_id, "$expr": {
                "$lt": ["$analysis_checkpoint.source_seq", "$latest_user_input_fence_seq"]}},
            {"$unset": {"analysis_checkpoint": ""}})
    
    async def save(self, session: Session) -> None:
        """Save or update a session"""
        mongo_session = await SessionDocument.find_one(
            {"session_id": session.id}
        )
        
        if not mongo_session:
            mongo_session = SessionDocument.from_domain(session)
            await mongo_session.save()
            return

        # A stale in-memory session must not undo a title explicitly chosen by the user.
        manual_title = mongo_session.title if mongo_session.title_manually_set else None
        update_data = session.model_dump(exclude={"id", "created_at"})
        update_data["updated_at"] = datetime.now(UTC)
        if manual_title is not None:
            update_data["title"] = manual_title
            update_data["title_manually_set"] = True
        # Storage-only atomic fields such as ``event_seq`` and
        # ``client_message_ids`` are intentionally absent. Replacing the whole
        # document here could roll either field back from a stale read.
        result = await SessionDocument.get_pymongo_collection().update_one(
            {"session_id": session.id},
            {"$set": update_data},
        )
        if not result.matched_count:
            raise ValueError(f"Session {session.id} not found")

    async def compare_and_set_task_id(
        self, session_id: str, *, expected_task_id: str | None,
        task_id: str | None, dataset_ids: list[str] | None = None,
    ) -> bool:
        update = {"task_id": task_id, "updated_at": datetime.now(UTC)}
        if dataset_ids is not None:
            update["dataset_ids"] = list(dataset_ids)
        result = await SessionDocument.get_pymongo_collection().update_one(
            {"session_id": session_id, "task_id": expected_task_id},
            {"$set": update},
        )
        return bool(result.matched_count)


    async def find_by_id(self, session_id: str) -> Optional[Session]:
        """Find a session by its ID"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        )
        return mongo_session.to_domain() if mongo_session else None
    
    async def find_by_user_id(self, user_id: str) -> List[Session]:
        """Find all sessions for a specific user"""
        mongo_sessions = await SessionDocument.find(
            {"$or": [{"user_id": user_id}, {"collaborator_user_ids": user_id}]}
        ).sort("-latest_message_at").to_list()
        return [mongo_session.to_domain() for mongo_session in mongo_sessions]

    async def find_summaries_by_user_id(self, user_id: str) -> List[SessionSummary]:
        """Find lightweight session summaries for a user (excludes events/files)"""
        collection = SessionDocument.get_pymongo_collection()
        cursor = collection.find(
            {"$or": [{"user_id": user_id}, {"collaborator_user_ids": user_id}]},
            SESSION_LIST_PROJECTION,
        ).sort("latest_message_at", -1)
        summaries = []
        async for doc in cursor:
            summaries.append(SessionSummary(
                id=doc["session_id"],
                user_id=doc["user_id"],
                title=doc.get("title"),
                unread_message_count=doc.get("unread_message_count", 0),
                latest_message=doc.get("latest_message"),
                latest_message_at=doc.get("latest_message_at"),
                status=doc.get("status", SessionStatus.PENDING),
                is_shared=doc.get("is_shared", False),
                collaborator_user_ids=doc.get("collaborator_user_ids", []),
            ))
        return summaries

    async def find_dataset_summaries_by_user_id(
        self,
        user_id: str,
        dataset_id: str,
    ) -> List[SessionSummary]:
        """Return accessible sessions whose persisted user event selected a dataset."""
        event_collection = SessionEventDocument.get_pymongo_collection()
        session_ids = await event_collection.distinct(
            "session_id",
            {
                "event.type": "message",
                "event.role": "user",
                "event.metadata.dataset_ids": dataset_id,
            },
        )
        if not session_ids:
            return []

        collection = SessionDocument.get_pymongo_collection()
        cursor = collection.find(
            {
                "session_id": {"$in": session_ids},
                "$or": [{"user_id": user_id}, {"collaborator_user_ids": user_id}],
            },
            SESSION_LIST_PROJECTION,
        ).sort("latest_message_at", -1)
        summaries = []
        async for doc in cursor:
            summaries.append(SessionSummary(
                id=doc["session_id"],
                user_id=doc["user_id"],
                title=doc.get("title"),
                unread_message_count=doc.get("unread_message_count", 0),
                latest_message=doc.get("latest_message"),
                latest_message_at=doc.get("latest_message_at"),
                status=doc.get("status", SessionStatus.PENDING),
                is_shared=doc.get("is_shared", False),
                collaborator_user_ids=doc.get("collaborator_user_ids", []),
            ))
        return summaries
    
    async def find_by_id_and_user_id(self, session_id: str, user_id: str) -> Optional[Session]:
        """Find a session by ID and user ID (for authorization)"""
        mongo_session = await SessionDocument.find_one(
            {
                "session_id": session_id,
                "$or": [{"user_id": user_id}, {"collaborator_user_ids": user_id}],
            }
        )
        return mongo_session.to_domain() if mongo_session else None

    async def find_owned_by_id_and_user_id(self, session_id: str, user_id: str) -> Optional[Session]:
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session_id,
            SessionDocument.user_id == user_id,
        )
        return mongo_session.to_domain() if mongo_session else None

    async def update_collaborators(self, session_id: str, collaborator_user_ids: List[str]) -> None:
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"collaborator_user_ids": collaborator_user_ids, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")
    
    async def update_title(self, session_id: str, title: str) -> None:
        """Update an automatically generated title unless the user renamed it."""
        result = await SessionDocument.find_one(
            {
                "session_id": session_id,
                "title_manually_set": {"$ne": True},
            }
        ).update(
            {"$set": {"title": title, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            session = await SessionDocument.find_one(SessionDocument.session_id == session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found")
            logger.info("Skipped automatic title update for manually renamed session %s", session_id)

    async def update_title_manually(self, session_id: str, title: str) -> None:
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {
                "title": title,
                "title_manually_set": True,
                "updated_at": datetime.now(UTC),
            }}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def update_latest_message(self, session_id: str, message: str, timestamp: datetime) -> None:
        """Update the latest message of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"latest_message": message, "latest_message_at": timestamp, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    @staticmethod
    def _stored_event_sequence(document: Optional[dict]) -> Optional[int]:
        if not document:
            return None
        value = document.get("seq")
        if not isinstance(value, int):
            nested = document.get("event")
            value = nested.get("seq") if isinstance(nested, dict) else None
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and 0 < value <= MAX_EVENT_SEQUENCE
            else None
        )

    @staticmethod
    def _producer_event_key(producer_event_id: str) -> str:
        """Return the bounded, storage-only identity used by reservations."""
        return hashlib.sha256(producer_event_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _event_payload_digest(event: BaseEvent | dict) -> str:
        """Hash semantic event content, excluding transport/allocation fields.

        Redis cursor IDs, allocated sequences, and producer-local timestamps do
        not change what an event means. Excluding them lets an idempotent retry
        built in another process prove that it carries the same payload.
        """
        if isinstance(event, BaseEvent):
            normalized = event.model_dump(
                mode="json",
                exclude={"id", "seq", "timestamp"},
            )
        else:
            parsed = TypeAdapter(AgentEvent).validate_python(event)
            normalized = parsed.model_dump(
                mode="json",
                exclude={"id", "seq", "timestamp"},
            )
        try:
            encoded = json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("Event payload is not canonically serializable") from exc
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _adopt_event_reservation(
        cls,
        event: BaseEvent,
        reservation: dict,
        payload_digest: str,
    ) -> int:
        reserved_digest = reservation.get("payload_digest")
        if not isinstance(reserved_digest, str) or not hmac.compare_digest(
            reserved_digest,
            payload_digest,
        ):
            raise ValueError(
                "Event producer identity was reused with a different payload"
            )
        seq = cls._stored_event_sequence(reservation)
        if seq is None:
            raise RuntimeError("Stored event reservation has an invalid sequence")
        if event.seq is not None and event.seq != seq:
            raise ValueError("Event sequence conflicts with its durable reservation")
        event.seq = seq
        return seq

    async def _persist_event_reservation(
        self,
        *,
        session_id: str,
        producer_event_key: str,
        payload_digest: str,
        seq: int,
        allow_legacy_sequence_alias: bool = False,
    ) -> dict:
        """Atomically insert-or-read one immutable logical-event reservation."""
        collection = SessionEventReservationDocument.get_pymongo_collection()
        query = {
            "session_id": session_id,
            "producer_event_key": producer_event_key,
        }
        document = {
            **query,
            "payload_digest": payload_digest,
            "seq": seq,
            "created_at": datetime.now(UTC),
        }
        for attempt in range(SESSION_EVENT_WRITE_ATTEMPTS):
            try:
                reservation = await collection.find_one_and_update(
                    query,
                    {"$setOnInsert": document},
                    upsert=True,
                    projection={"seq": 1, "payload_digest": 1, "producer_event_key": 1},
                    return_document=ReturnDocument.AFTER,
                )
                if reservation is None:
                    raise RuntimeError("Event reservation was not persisted")
                return reservation
            except DuplicateKeyError:
                # Concurrent upserts for one logical ID can race at the unique
                # index. Read the winner and adopt it. A legacy event may also
                # already own the sequence under its pre-migration identity.
                reservation = await collection.find_one(
                    query,
                    {"seq": 1, "payload_digest": 1, "producer_event_key": 1},
                )
                if reservation is not None:
                    return reservation
                if allow_legacy_sequence_alias:
                    reservation = await collection.find_one(
                        {"session_id": session_id, "seq": seq},
                        {"seq": 1, "payload_digest": 1, "producer_event_key": 1},
                    )
                    if reservation is not None:
                        return reservation
                raise RuntimeError("Event reservation sequence collision")
            except ConnectionFailure:
                if attempt == SESSION_EVENT_WRITE_ATTEMPTS - 1:
                    raise
                delay = SESSION_EVENT_RETRY_DELAYS[attempt]
                logger.warning(
                    "Event reservation persistence interrupted; retrying in %.1fs (%d/%d)",
                    delay,
                    attempt + 1,
                    SESSION_EVENT_WRITE_ATTEMPTS,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("Event reservation was not persisted")

    async def _allocate_event_sequence(
        self,
        session_id: str,
        event_collection,
    ) -> int:
        """Allocate a never-reused session sequence; abandoned values are gaps."""
        session_collection = SessionDocument.get_pymongo_collection()
        session_doc = await session_collection.find_one_and_update(
            {
                "session_id": session_id,
                "event_seq": {"$gt": 0, "$lt": MAX_EVENT_SEQUENCE},
            },
            {"$inc": {"event_seq": 1}},
            projection={"event_seq": 1},
            return_document=ReturnDocument.AFTER,
        )
        if session_doc is None:
            # Rolling upgrades can encounter sessions whose old events have no
            # sequence. Establish a floor above both their count and any
            # partially migrated value before the first increment. The update
            # pipeline remains atomic if multiple workers initialize together.
            legacy_count = await event_collection.count_documents(
                {"session_id": session_id}
            )
            latest = await event_collection.find_one(
                {"session_id": session_id, "seq": {"$type": "number"}},
                {"seq": 1},
                sort=[("seq", -1)],
            )
            latest_seq = self._stored_event_sequence(latest) or 0
            history_floor = max(legacy_count, latest_seq)
            if history_floor >= MAX_EVENT_SEQUENCE:
                raise RuntimeError("Session event sequence space is exhausted")
            session_doc = await session_collection.find_one_and_update(
                {
                    "session_id": session_id,
                    "$or": [
                        {"event_seq": {"$exists": False}},
                        {"event_seq": {"$lte": 0}},
                    ],
                },
                [{
                    "$set": {
                        "event_seq": {
                            "$add": [
                                {
                                    "$max": [
                                        {"$ifNull": ["$event_seq", 0]},
                                        history_floor,
                                    ]
                                },
                                1,
                            ]
                        }
                    }
                }],
                projection={"event_seq": 1},
                return_document=ReturnDocument.AFTER,
            )
            if session_doc is None:
                # Another allocator may have initialized the counter after our
                # fast-path miss. Retry the bounded atomic increment once.
                session_doc = await session_collection.find_one_and_update(
                    {
                        "session_id": session_id,
                        "event_seq": {"$gt": 0, "$lt": MAX_EVENT_SEQUENCE},
                    },
                    {"$inc": {"event_seq": 1}},
                    projection={"event_seq": 1},
                    return_document=ReturnDocument.AFTER,
                )
        if session_doc is None:
            existing_session = await session_collection.find_one(
                {"session_id": session_id},
                {"event_seq": 1},
            )
            if existing_session is None:
                raise ValueError(f"Session {session_id} not found")
            raise RuntimeError("Session event sequence space is exhausted")
        seq = session_doc.get("event_seq")
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 1
            or seq > MAX_EVENT_SEQUENCE
        ):
            raise RuntimeError("Session event sequence allocation failed")
        return seq

    async def reserve_event_sequence(self, session_id: str, event: BaseEvent) -> int:
        """Persist an immutable logical-event reservation before publication.

        Concurrent callers may consume more than one candidate counter value,
        but the atomic reservation upsert chooses one winner and every matching
        caller adopts its sequence. Gaps are intentional and never reused.
        """
        producer_event_id = event.bind_producer_event_id()
        producer_event_key = self._producer_event_key(producer_event_id)
        payload_digest = self._event_payload_digest(event)
        reservation_collection = (
            SessionEventReservationDocument.get_pymongo_collection()
        )
        reservation = await reservation_collection.find_one(
            {
                "session_id": session_id,
                "producer_event_key": producer_event_key,
            },
            {"seq": 1, "payload_digest": 1, "producer_event_key": 1},
        )
        if reservation is not None:
            return self._adopt_event_reservation(event, reservation, payload_digest)

        legacy_event_key = f"{session_id}:{producer_event_id}"
        event_collection = SessionEventDocument.get_pymongo_collection()
        legacy_event = await event_collection.find_one(
            {"event_key": legacy_event_key},
            {"seq": 1, "event": 1, "payload_digest": 1},
        )
        legacy_seq = self._stored_event_sequence(legacy_event)
        if legacy_event is not None and legacy_seq is not None:
            stored_digest = legacy_event.get("payload_digest")
            if not isinstance(stored_digest, str):
                stored_payload = legacy_event.get("event")
                if not isinstance(stored_payload, dict):
                    raise RuntimeError("Stored legacy event has no valid payload")
                stored_digest = self._event_payload_digest(stored_payload)
            if not hmac.compare_digest(stored_digest, payload_digest):
                raise ValueError(
                    "Event producer identity was reused with a different payload"
                )
            reservation = await self._persist_event_reservation(
                session_id=session_id,
                producer_event_key=producer_event_key,
                payload_digest=payload_digest,
                seq=legacy_seq,
                allow_legacy_sequence_alias=True,
            )
            return self._adopt_event_reservation(event, reservation, payload_digest)

        if event.seq is not None:
            # Only the allocator may mint a new sequence. A pre-populated value
            # without a reservation could otherwise bypass the session counter.
            raise ValueError("Event sequence has no durable reservation")

        candidate_seq = await self._allocate_event_sequence(
            session_id,
            event_collection,
        )
        reservation = await self._persist_event_reservation(
            session_id=session_id,
            producer_event_key=producer_event_key,
            payload_digest=payload_digest,
            seq=candidate_seq,
        )
        return self._adopt_event_reservation(event, reservation, payload_digest)

    @classmethod
    def _validate_persisted_event(
        cls,
        document: dict,
        *,
        expected_seq: int,
        payload_digest: str,
    ) -> None:
        stored_seq = cls._stored_event_sequence(document)
        if stored_seq != expected_seq:
            raise RuntimeError("Persisted event conflicts with its reservation")
        stored_digest = document.get("payload_digest")
        if not isinstance(stored_digest, str):
            stored_payload = document.get("event")
            if not isinstance(stored_payload, dict):
                raise RuntimeError("Persisted event has no valid payload")
            stored_digest = cls._event_payload_digest(stored_payload)
        if not hmac.compare_digest(stored_digest, payload_digest):
            raise ValueError(
                "Event producer identity was reused with a different payload"
            )

    async def add_event(self, session_id: str, event: BaseEvent) -> None:
        """Idempotently materialize the event selected by its reservation."""
        await self._add_event(session_id, event)

    async def add_input_event(self, session_id: str, event: BaseEvent, admission) -> None:
        """Commit acceptance and the immutable user event in one Mongo write."""
        from app.domain.models.event import MessageEvent
        from app.domain.models.input_admission import InputAdmission
        if not isinstance(event, MessageEvent) or event.role != "user":
            raise ValueError("Only a user message can be accepted for delivery")
        if not isinstance(admission, InputAdmission) or admission.state != "pending":
            raise ValueError("New input admission must be pending")
        await self._add_event(session_id, event, admission=admission)

    async def _add_event(self, session_id: str, event: BaseEvent, *, admission=None) -> None:
        producer_event_id = event.bind_producer_event_id()
        producer_event_key = self._producer_event_key(producer_event_id)
        payload_digest = self._event_payload_digest(event)
        await self.reserve_event_sequence(session_id, event)
        if event.seq is None:  # Narrow the type after the repository contract.
            raise RuntimeError("Event reservation did not assign a sequence")
        # Fence before the event write, not after it: otherwise another process
        # could claim an old checkpoint in the cross-collection commit gap.
        await self._mark_user_event_progress(session_id, event, durable=False)

        event_key = f"{session_id}:{event.id}"
        legacy_producer_key = f"{session_id}:{producer_event_id}"
        collection = SessionEventDocument.get_pymongo_collection()
        existing = await collection.find_one(
            {
                "session_id": session_id,
                "$or": [
                    {"producer_event_key": producer_event_key},
                    {"event_key": event_key},
                    {"event_key": legacy_producer_key},
                ],
            },
            {"seq": 1, "event": 1, "payload_digest": 1},
        )
        if existing is not None:
            self._validate_persisted_event(
                existing,
                expected_seq=event.seq,
                payload_digest=payload_digest,
            )
            await self._mark_user_event_progress(session_id, event, durable=True)
            return

        document = {
            "session_id": session_id,
            "producer_event_key": producer_event_key,
            "payload_digest": payload_digest,
            "event_key": event_key,
            "seq": event.seq,
            "version": event.version,
            "event": event.model_dump(),
            "created_at": datetime.now(UTC),
        }
        if admission is not None:
            document["input_admission"] = admission.model_dump(mode="python")
        for attempt in range(SESSION_EVENT_WRITE_ATTEMPTS):
            try:
                persisted = await collection.find_one_and_update(
                    {
                        "session_id": session_id,
                        "producer_event_key": producer_event_key,
                    },
                    {"$setOnInsert": document},
                    upsert=True,
                    projection={"seq": 1, "event": 1, "payload_digest": 1},
                    return_document=ReturnDocument.AFTER,
                )
                if persisted is None:
                    raise RuntimeError("Session event was not persisted")
                self._validate_persisted_event(
                    persisted,
                    expected_seq=event.seq,
                    payload_digest=payload_digest,
                )
                await self._mark_user_event_progress(session_id, event, durable=True)
                return
            except DuplicateKeyError:
                # The producer, legacy event-key, or sequence unique index may
                # have selected a concurrent winner. Only an identical payload
                # at the reserved sequence is an idempotent success.
                existing = await collection.find_one(
                    {
                        "session_id": session_id,
                        "$or": [
                            {"producer_event_key": producer_event_key},
                            {"event_key": event_key},
                            {"event_key": legacy_producer_key},
                            {"seq": event.seq},
                        ],
                    },
                    {"seq": 1, "event": 1, "payload_digest": 1},
                )
                if existing is None:
                    raise
                self._validate_persisted_event(
                    existing,
                    expected_seq=event.seq,
                    payload_digest=payload_digest,
                )
                await self._mark_user_event_progress(session_id, event, durable=True)
                return
            except ConnectionFailure:
                if attempt == SESSION_EVENT_WRITE_ATTEMPTS - 1:
                    raise
                delay = SESSION_EVENT_RETRY_DELAYS[attempt]
                logger.warning(
                    "Session event persistence interrupted for %s; retrying in %.1fs (%d/%d)",
                    event_key,
                    delay,
                    attempt + 1,
                    SESSION_EVENT_WRITE_ATTEMPTS,
                )
                await asyncio.sleep(delay)

    async def _mark_user_event_progress(self, session_id: str, event: BaseEvent, *, durable: bool) -> None:
        from app.domain.models.event import MessageEvent
        if not isinstance(event, MessageEvent) or event.role != "user":
            return
        if type(event.seq) is not int or not 0 < event.seq <= MAX_EVENT_SEQUENCE:
            raise ValueError("User input fence requires a reserved sequence")
        field = "latest_user_event_seq" if durable else "latest_user_input_fence_seq"
        result = await SessionDocument.get_pymongo_collection().update_one(
            {"session_id": session_id}, {"$max": {field: event.seq}})
        if not result.matched_count:
            raise ValueError("Session not found while fencing user input")

    async def record_event_transport_alias(self, session_id: str, event: BaseEvent) -> None:
        """Update only the non-semantic Redis cursor after durable publication.

        Event IDs are explicitly excluded from the immutable payload digest;
        producer identity and seq remain unchanged throughout this update.
        """
        await SessionEventDocument.get_pymongo_collection().update_one(
            {"session_id": session_id,
             "producer_event_key": self._producer_event_key(event.bind_producer_event_id())},
            {"$set": {"event.id": event.id}})

    async def resolve_event_sequence(self, session_id: str, event_id: str) -> int | None:
        """Bridge event-id-only clients onto durable replay, including a crash
        between Mongo commit and Redis cursor/alias publication.
        """
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
            return None
        document = await SessionEventDocument.get_pymongo_collection().find_one(
            {"session_id": session_id, "$or": [
                {"event.id": event_id}, {"event_key": f"{session_id}:{event_id}"},
                {"producer_event_key": self._producer_event_key(event_id)},
            ]}, {"seq": 1, "event.seq": 1})
        return self._stored_event_sequence(document)

    async def claim_client_message_id(self, session_id: str, client_message_id: str) -> bool:
        """Atomically reserve a message ID within a session."""
        collection = SessionDocument.get_pymongo_collection()
        result = await collection.update_one(
            {
                "session_id": session_id,
                "client_message_ids": {"$ne": client_message_id},
            },
            {
                "$push": {
                    "client_message_ids": {
                        "$each": [client_message_id],
                        "$slice": -CLIENT_MESSAGE_ID_HISTORY_LIMIT,
                    }
                },
                "$set": {"updated_at": datetime.now(UTC)},
            },
        )
        if result.matched_count:
            return True
        session_exists = await collection.count_documents(
            {"session_id": session_id},
            limit=1,
        )
        if not session_exists:
            raise ValueError(f"Session {session_id} not found")
        return False

    async def release_client_message_id(self, session_id: str, client_message_id: str) -> None:
        """Release an unqueued message ID so a caller can safely retry it."""
        collection = SessionDocument.get_pymongo_collection()
        result = await collection.update_one(
            {"session_id": session_id},
            {
                "$pull": {"client_message_ids": client_message_id},
                "$set": {"updated_at": datetime.now(UTC)},
            },
        )
        if not result.matched_count:
            raise ValueError(f"Session {session_id} not found")

    async def get_events(self, session_id: str) -> List[AgentEvent]:
        """Get events in sequence order, synthesizing legacy sequence values."""
        docs = await SessionEventDocument.find(
            {"session_id": session_id}
        ).sort("+created_at", "+_id").to_list()
        adapter = TypeAdapter(AgentEvent)
        events = [adapter.validate_python(d.event) for d in docs]

        # Pre-versioning documents have neither top-level nor embedded seq.
        # Their stable historical order becomes 1..N.  The allocator initializes
        # new sessions above the legacy document count, so new values do not
        # collide with this compatibility projection.
        used_sequences = {
            seq
            for seq in (
                self._stored_event_sequence({
                    "seq": getattr(doc, "seq", None),
                    "event": doc.event,
                })
                for doc in docs
            )
            if seq is not None
        }
        next_legacy_seq = 1
        for doc, event in zip(docs, events):
            stored_seq = self._stored_event_sequence({
                "seq": getattr(doc, "seq", None),
                "event": doc.event,
            })
            if stored_seq is not None:
                event.seq = stored_seq
                continue
            while next_legacy_seq in used_sequences:
                next_legacy_seq += 1
            event.seq = next_legacy_seq
            used_sequences.add(next_legacy_seq)
            next_legacy_seq += 1
        return sorted(events, key=lambda item: item.seq or 0)

    async def get_execution_history(self, session_id: str, *, before_seq: int) -> ExecutionHistory:
        """Fold only new event bodies; never use a cache as the source of truth.

        Reserved sequence gaps may be filled by late commits. An indexed prefix
        count validates cache membership before AND after reading the suffix;
        immutable event payloads make this sufficient without changing writers.
        This avoids full-body transfers/decoding, not all O(N) database work:
        the prefix count still scans index entries. Legacy histories retain their
        original sequence synthesis. Oversized evidence bypasses persistence,
        never truncates the execution view or modifies the canonical log.
        """
        if type(before_seq) is not int or not 0 < before_seq <= MAX_EVENT_SEQUENCE:
            raise ValueError("invalid execution history cursor")
        events = SessionEventDocument.get_pymongo_collection()
        sessions = SessionDocument.get_pymongo_collection()

        async def rebuild_legacy():
            state = ExecutionHistory()
            for event in await self.get_events(session_id):
                if (event.seq or 0) < before_seq:
                    state.fold(event)
            return state

        legacy = await events.find_one({"session_id": session_id, "$or": [
            {"seq": {"$not": {"$type": ["int", "long"]}}},
            {"seq": {"$lte": 0}}, {"seq": {"$gt": MAX_EVENT_SEQUENCE}},
        ]}, {"_id": 1})
        if legacy is not None:
            return await rebuild_legacy()
        document = await sessions.find_one({"session_id": session_id}, {"execution_history_projection": 1})
        if document is None:
            raise ValueError("Session not found")
        state = ExecutionHistory()
        raw = document.get("execution_history_projection")
        if isinstance(raw, dict):
            try:
                candidate = ExecutionHistory.model_validate(raw)
                if candidate.seq < before_seq:
                    count = await events.count_documents({"session_id": session_id, "seq": {"$lte": candidate.seq}})
                    if count == candidate.event_count:
                        state = candidate
            except (ValidationError, ValueError, TypeError):
                pass  # Unknown/corrupt cache versions rebuild from the log.
        adapter = TypeAdapter(AgentEvent)
        for attempt in range(2):
            cursor = events.find({"session_id": session_id, "seq": {"$gt": state.seq, "$lt": before_seq}},
                                 {"seq": 1, "event": 1}).sort("seq", 1).batch_size(256)
            async for stored in cursor:
                event = adapter.validate_python(stored["event"])
                if event.seq is not None and event.seq != stored["seq"]:
                    raise RuntimeError("Persisted event sequence envelope is inconsistent")
                event.seq = stored["seq"]
                state.fold(event)
            count = await events.count_documents({"session_id": session_id, "seq": {"$lte": state.seq}})
            if count == state.event_count:
                break
            if attempt == 1:
                return await rebuild_legacy()
            state = ExecutionHistory()  # A late prefix commit invalidated the cut.
        row = state.model_dump(mode="json")
        if len(json.dumps(row, ensure_ascii=False).encode("utf-8")) <= EXECUTION_HISTORY_CACHE_BYTES:
            try:
                # Cache fields are storage-only; ordinary Session saves and all
                # browser projections omit them. A slower fold cannot replace
                # a newer valid watermark. An unknown version is replaceable.
                await sessions.update_one({"session_id": session_id, "$or": [
                    {"execution_history_projection.version": {"$ne": 2}},
                    {"execution_history_projection.seq": {"$lt": state.seq}},
                    {"execution_history_projection.seq": state.seq,
                     "execution_history_projection.event_count": {"$lte": state.event_count}},
                ]}, {"$set": {"execution_history_projection": row}})
            except Exception as error:
                logger.warning("Execution history cache write skipped error_type=%s", type(error).__name__)
        return state

    async def get_events_after(self, session_id: str, seq: int) -> List[AgentEvent]:
        """Return replay events, using the sequence index when history permits.

        Any missing or non-integral top-level sequence identifies legacy/mixed
        history and keeps the original full-history synthesis path. Fully
        versioned sessions query only ``seq > watermark`` through the compound
        ``(session_id, seq)`` index.
        """
        collection = SessionEventDocument.get_pymongo_collection()
        legacy_or_invalid = await collection.find_one(
            {
                "session_id": session_id,
                "$or": [
                    {"seq": {"$not": {"$type": ["int", "long"]}}},
                    {"seq": {"$lte": 0}},
                    {"seq": {"$gt": MAX_EVENT_SEQUENCE}},
                ],
            },
            {"_id": 1},
        )
        if legacy_or_invalid is not None:
            events = await self.get_events(session_id)
            return [event for event in events if (event.seq or 0) > seq]

        docs = await SessionEventDocument.find(
            {"session_id": session_id, "seq": {"$gt": seq}}
        ).sort("+seq", "+_id").to_list()
        adapter = TypeAdapter(AgentEvent)
        events: List[AgentEvent] = []
        for document in docs:
            event = adapter.validate_python(document.event)
            stored_seq = self._stored_event_sequence({
                "seq": getattr(document, "seq", None),
                "event": document.event,
            })
            if stored_seq is None:
                # Defensive compatibility for a write racing the probe above.
                all_events = await self.get_events(session_id)
                return [item for item in all_events if (item.seq or 0) > seq]
            if event.seq is not None and event.seq != stored_seq:
                raise RuntimeError("Persisted event sequence envelope is inconsistent")
            event.seq = stored_seq
            events.append(event)
        return events

    async def get_history_page(
        self, session_id: str, *, turns: int = 5, before_seq: int | None = None,
    ) -> SessionHistoryPage:
        """Indexed whole-turn window, with the original legacy seq projection.

        This deliberately limits *turns*, not individual events: splitting a
        tool call/result or a plan lifecycle would change replay semantics. An
        unusually large single turn still loads fully; callers must not claim
        a hard byte/event cap for this endpoint.
        """
        if type(turns) is not int or not 1 <= turns <= 20:
            raise ValueError("turns must be between 1 and 20")
        if before_seq is not None and (type(before_seq) is not int or not 1 <= before_seq <= MAX_EVENT_SEQUENCE):
            raise ValueError("invalid history cursor")
        collection = SessionEventDocument.get_pymongo_collection()
        legacy = await collection.find_one({"session_id": session_id, "$or": [
            {"seq": {"$not": {"$type": ["int", "long"]}}},
            {"seq": {"$lte": 0}}, {"seq": {"$gt": MAX_EVENT_SEQUENCE}},
        ]}, {"_id": 1})
        if legacy is not None:
            return page_legacy_history(await self.get_events(session_id), turns=turns, before_seq=before_seq)

        upper_query: dict = {"session_id": session_id}
        if before_seq is not None:
            upper_query["seq"] = {"$lt": before_seq}
        # Freeze the upper watermark so events appended while this page is
        # being assembled are handled by the normal SSE continuation exactly.
        latest = await collection.find_one(upper_query, {"seq": 1}, sort=[("seq", -1)])
        if latest is None:
            return SessionHistoryPage([], False, None)
        watermark = latest["seq"]
        boundaries = await collection.find({
            "session_id": session_id, "seq": {"$lte": watermark},
            "event.type": "message", "event.role": "user",
        }, {"seq": 1}).sort("seq", -1).limit(turns + 1).to_list(length=turns + 1)
        has_more = len(boundaries) > turns
        start = boundaries[turns - 1]["seq"] if has_more else 1
        documents = await collection.find({
            "session_id": session_id, "seq": {"$gte": start, "$lte": watermark},
        }, {"seq": 1, "event": 1}).sort("seq", 1).to_list(length=None)
        adapter = TypeAdapter(AgentEvent)
        events = []
        for document in documents:
            event = adapter.validate_python(document["event"])
            if event.seq is not None and event.seq != document["seq"]:
                raise RuntimeError("Persisted event sequence envelope is inconsistent")
            event.seq = document["seq"]
            events.append(event)
        return SessionHistoryPage(events, has_more, start if has_more else None)

    async def add_execution_snapshot(
        self,
        snapshot: ExecutionEnvironmentSnapshot,
    ) -> None:
        """Insert once by task id; an identical retry is a successful no-op."""
        collection = ExecutionEnvironmentSnapshotDocument.get_pymongo_collection()
        try:
            await collection.update_one(
                {
                    "task_id": snapshot.task_id,
                    "session_id": snapshot.session_id,
                    "fingerprint": snapshot.fingerprint,
                    "trigger_event_seq": snapshot.trigger_event_seq,
                },
                {"$setOnInsert": snapshot.model_dump(mode="python")},
                upsert=True,
            )
        except DuplicateKeyError as exc:
            # A unique task id already exists with a different fingerprint.
            # Provenance is immutable, so never replace the original record.
            raise ValueError("Task execution snapshot is immutable") from exc

    async def get_execution_snapshots(
        self,
        session_id: str,
    ) -> List[ExecutionEnvironmentSnapshot]:
        docs = await ExecutionEnvironmentSnapshotDocument.find(
            ExecutionEnvironmentSnapshotDocument.session_id == session_id
        ).sort("+captured_at").to_list()
        return [document.to_domain() for document in docs]
    
    async def add_file(self, session_id: str, file_info: FileInfo) -> None:
        """Add a file to a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$push": {"files": file_info.model_dump()}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")
    
    async def remove_file(self, session_id: str, file_id: str) -> None:
        """Remove a file from a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$pull": {"files": {"file_id": file_id}}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def get_file_by_path(self, session_id: str, file_path: str) -> Optional[FileInfo]:
        """Get file by path from a session"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        )
        if not mongo_session:
            raise ValueError(f"Session {session_id} not found")
        
        # Search for file with matching path
        for file_info in mongo_session.files:
            if file_info.file_path == file_path:
                return file_info
        return None

    async def delete(self, session_id: str) -> None:
        """Delete a session"""
        mongo_session = await SessionDocument.find_one(
            {"session_id": session_id}
        )
        if mongo_session:
            from app.infrastructure.repositories.mongo_model_trace_repository import get_model_trace_repository
            await get_model_trace_repository().delete_session(session_id)
            # Delete internal provenance/reservations before the parent so a
            # failed child cleanup cannot leave browser-invisible records after
            # the user deletes the session.
            await ExecutionEnvironmentSnapshotDocument.get_pymongo_collection().delete_many(
                {"session_id": session_id}
            )
            await SessionEventReservationDocument.get_pymongo_collection().delete_many(
                {"session_id": session_id}
            )
            await SessionEventDocument.get_pymongo_collection().delete_many(
                {"session_id": session_id}
            )
            await mongo_session.delete()

    async def get_all(self) -> List[Session]:
        """Get all sessions"""
        mongo_sessions = await SessionDocument.find().sort("-latest_message_at").to_list()
        return [mongo_session.to_domain() for mongo_session in mongo_sessions]
    
    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        """Update the status of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"status": status, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def update_unread_message_count(self, session_id: str, count: int) -> None:
        """Update the unread message count of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"unread_message_count": count, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def increment_unread_message_count(self, session_id: str) -> None:
        """Atomically increment the unread message count of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$inc": {"unread_message_count": 1}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def decrement_unread_message_count(self, session_id: str) -> None:
        """Atomically decrement the unread message count of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$inc": {"unread_message_count": -1}, "$set": {"updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")

    async def update_shared_status(self, session_id: str, is_shared: bool) -> None:
        """Update the shared status of a session"""
        result = await SessionDocument.find_one(
            SessionDocument.session_id == session_id
        ).update(
            {"$set": {"is_shared": is_shared, "updated_at": datetime.now(UTC)}}
        )
        if not result:
            raise ValueError(f"Session {session_id} not found")
