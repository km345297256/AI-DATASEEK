import asyncio
from typing import Optional, List
from datetime import datetime, UTC
from pymongo.errors import ConnectionFailure
from pydantic import TypeAdapter
from app.domain.models.session import Session, SessionStatus, SessionSummary
from app.domain.models.file import FileInfo
from app.domain.repositories.session_repository import SessionRepository
from app.domain.models.event import BaseEvent, AgentEvent
from app.infrastructure.models.documents import SessionDocument, SessionEventDocument
import logging

logger = logging.getLogger(__name__)

SESSION_EVENT_WRITE_ATTEMPTS = 3
SESSION_EVENT_RETRY_DELAYS = (0.1, 0.3)

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
    
    async def save(self, session: Session) -> None:
        """Save or update a session"""
        mongo_session = await SessionDocument.find_one(
            SessionDocument.session_id == session.id
        )
        
        if not mongo_session:
            mongo_session = SessionDocument.from_domain(session)
            await mongo_session.save()
            return
        
        # A stale in-memory session must not undo a title explicitly chosen by the user.
        manual_title = mongo_session.title if mongo_session.title_manually_set else None
        mongo_session.update_from_domain(session)
        if manual_title is not None:
            mongo_session.title = manual_title
            mongo_session.title_manually_set = True
        await mongo_session.save()


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

    async def add_event(self, session_id: str, event: BaseEvent) -> None:
        """Add an event to a session"""
        event_key = f"{session_id}:{event.id}"
        document = {
            "session_id": session_id,
            "event_key": event_key,
            "event": event.model_dump(),
            "created_at": datetime.now(UTC),
        }
        collection = SessionEventDocument.get_pymongo_collection()
        for attempt in range(SESSION_EVENT_WRITE_ATTEMPTS):
            try:
                await collection.update_one(
                    {"event_key": event_key},
                    {"$setOnInsert": document},
                    upsert=True,
                )
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

    async def get_events(self, session_id: str) -> List[AgentEvent]:
        """Get all events for a session ordered by creation time"""
        docs = await SessionEventDocument.find(
            SessionEventDocument.session_id == session_id
        ).sort("+created_at").to_list()
        adapter = TypeAdapter(AgentEvent)
        return [adapter.validate_python(d.event) for d in docs]
    
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
            SessionDocument.session_id == session_id
        )
        if mongo_session:
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
