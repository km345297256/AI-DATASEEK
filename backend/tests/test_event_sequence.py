import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.models.event import (
    AgentEvent,
    DoneEvent,
    MessageEvent,
    MAX_EVENT_SEQUENCE,
)
from app.domain.models.session import Session, SessionStatus
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.event_recording import create_event_recording
from app.domain.services.lightweight_task_runner import LightweightTaskRunner
from app.infrastructure.models.documents import (
    ExecutionEnvironmentSnapshotDocument,
    SessionDocument,
    SessionEventDocument,
    SessionEventReservationDocument,
)
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.interfaces.schemas.event import EventMapper
from app.interfaces.schemas.session import ChatRequest


async def _async_value(value):
    return value


class MemoryReservationCollection:
    """Small atomic fake for reservation race tests."""

    def __init__(self, *, synchronize_initial_reads: int = 0):
        self.documents = []
        self._lock = asyncio.Lock()
        self._read_count = 0
        self._read_barrier = asyncio.Event()
        self._synchronize_initial_reads = synchronize_initial_reads

    @staticmethod
    def _matches(document, query):
        return all(document.get(key) == value for key, value in query.items())

    async def find_one(self, query, *_args, **_kwargs):
        existing = next(
            (
                dict(document)
                for document in self.documents
                if self._matches(document, query)
            ),
            None,
        )
        if (
            existing is None
            and self._synchronize_initial_reads
            and "producer_event_key" in query
        ):
            self._read_count += 1
            if self._read_count >= self._synchronize_initial_reads:
                self._read_barrier.set()
            await self._read_barrier.wait()
        return existing

    async def find_one_and_update(self, query, update, **_kwargs):
        async with self._lock:
            for document in self.documents:
                if self._matches(document, query):
                    return dict(document)
            document = dict(update["$setOnInsert"])
            self.documents.append(document)
            return dict(document)

    async def delete_many(self, query):
        self.documents = [
            document
            for document in self.documents
            if not self._matches(document, query)
        ]


def test_legacy_domain_event_defaults_to_v1_without_sequence():
    event = TypeAdapter(AgentEvent).validate_python({
        "type": "message",
        "id": "legacy-event",
        "role": "assistant",
        "message": "legacy result",
    })

    assert event.version == 1
    assert event.seq is None


@pytest.mark.asyncio
async def test_event_mapper_exposes_additive_sequence_and_version():
    event = MessageEvent(
        id="1700000000000-1",
        seq=12,
        role="assistant",
        message="result",
    )

    mapped = await EventMapper.event_to_sse_event(event)

    assert mapped.event == "message"
    assert mapped.data.event_id == "1700000000000-1"
    assert mapped.data.seq == 12
    assert mapped.data.version == 1

    done = await EventMapper.event_to_sse_event(DoneEvent(seq=13))
    assert done.data.seq == 13
    assert done.data.version == 1


@pytest.mark.asyncio
async def test_history_synthesizes_sequences_for_legacy_documents(monkeypatch):
    docs = [
        SimpleNamespace(
            seq=None,
            event={
                "type": "message",
                "id": "legacy-1",
                "role": "assistant",
                "message": "first",
            },
        ),
        SimpleNamespace(
            seq=None,
            event={
                "type": "message",
                "id": "legacy-2",
                "role": "assistant",
                "message": "second",
            },
        ),
        SimpleNamespace(
            seq=3,
            event={
                "type": "done",
                "id": "new-3",
                "seq": 3,
                "version": 1,
            },
        ),
    ]

    class Query:
        def sort(self, *_args):
            return self

        async def to_list(self):
            return docs

    monkeypatch.setattr(
        SessionEventDocument,
        "find",
        classmethod(lambda cls, *_args, **_kwargs: Query()),
    )

    events = await MongoSessionRepository().get_events("session-1")

    assert [event.id for event in events] == ["legacy-1", "legacy-2", "new-3"]
    assert [event.seq for event in events] == [1, 2, 3]
    assert [event.version for event in events] == [1, 1, 1]


@pytest.mark.asyncio
async def test_get_events_after_uses_sequence_index_for_versioned_history(monkeypatch):
    captured = {}
    docs = [
        SimpleNamespace(
            seq=4,
            event=MessageEvent(
                id="redis-4",
                seq=4,
                role="assistant",
                message="answer",
            ).model_dump(),
        ),
        SimpleNamespace(seq=7, event=DoneEvent(id="redis-7", seq=7).model_dump()),
    ]

    class Collection:
        async def find_one(self, query, *_args, **_kwargs):
            captured["probe"] = query
            return None

    class Query:
        def sort(self, *fields):
            captured["sort"] = fields
            return self

        async def to_list(self):
            return docs

    def find(_cls, query):
        captured["query"] = query
        return Query()

    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: Collection()),
    )
    monkeypatch.setattr(SessionEventDocument, "find", classmethod(find))

    events = await MongoSessionRepository().get_events_after("session-1", 3)

    assert [event.seq for event in events] == [4, 7]
    assert captured["query"] == {
        "session_id": "session-1",
        "seq": {"$gt": 3},
    }
    assert captured["sort"] == ("+seq", "+_id")


@pytest.mark.asyncio
async def test_get_events_after_keeps_legacy_synthesis_fallback(monkeypatch):
    class Collection:
        async def find_one(self, *_args, **_kwargs):
            return {"_id": "legacy"}

    repository = MongoSessionRepository()
    calls = []

    async def get_events(session_id):
        calls.append(session_id)
        return [
            MessageEvent(seq=1, role="assistant", message="old"),
            DoneEvent(seq=2),
        ]

    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: Collection()),
    )
    monkeypatch.setattr(repository, "get_events", get_events)

    events = await repository.get_events_after("session-1", 1)

    assert calls == ["session-1"]
    assert [event.seq for event in events] == [2]


@pytest.mark.asyncio
async def test_sequence_allocation_is_atomic_for_concurrent_events(monkeypatch):
    class EventCollection:
        async def find_one(self, *_args, **_kwargs):
            return None

    class SessionCollection:
        def __init__(self):
            self.seq = 20
            self.lock = asyncio.Lock()
            self.updates = []

        async def find_one_and_update(self, query, update, **_kwargs):
            async with self.lock:
                self.updates.append((query, update))
                self.seq += 1
                return {"event_seq": self.seq}

    event_collection = EventCollection()
    reservation_collection = MemoryReservationCollection()
    session_collection = SessionCollection()
    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: event_collection),
    )
    monkeypatch.setattr(
        SessionEventReservationDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: reservation_collection),
    )
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: session_collection),
    )
    repository = MongoSessionRepository()
    first = MessageEvent(role="assistant", message="first")
    second = MessageEvent(role="assistant", message="second")

    sequences = await asyncio.gather(
        repository.reserve_event_sequence("session-1", first),
        repository.reserve_event_sequence("session-1", second),
    )

    assert sorted(sequences) == [21, 22]
    assert {first.seq, second.seq} == {21, 22}
    assert all(update == {"$inc": {"event_seq": 1}} for _, update in session_collection.updates)


@pytest.mark.asyncio
async def test_legacy_sequence_floor_is_initialized_before_increment(monkeypatch):
    class EventCollection:
        async def find_one(self, query, *_args, **_kwargs):
            if "event_key" in query:
                return None
            return {"seq": 9}

        async def count_documents(self, _query):
            return 7

    class SessionCollection:
        def __init__(self):
            self.calls = []

        async def find_one_and_update(self, query, update, **_kwargs):
            self.calls.append((query, update))
            if len(self.calls) == 1:
                return None
            return {"event_seq": 10}

    event_collection = EventCollection()
    reservation_collection = MemoryReservationCollection()
    session_collection = SessionCollection()
    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: event_collection),
    )
    monkeypatch.setattr(
        SessionEventReservationDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: reservation_collection),
    )
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: session_collection),
    )
    event = DoneEvent()

    seq = await MongoSessionRepository().reserve_event_sequence("session-1", event)

    assert seq == 10
    pipeline = session_collection.calls[1][1]
    assert pipeline[0]["$set"]["event_seq"]["$add"][0]["$max"][1] == 9


@pytest.mark.asyncio
async def test_existing_event_id_reuses_persisted_sequence(monkeypatch):
    event = DoneEvent(id="stable-event")
    reservation_collection = MemoryReservationCollection()

    class EventCollection:
        async def find_one(self, query, *_args, **_kwargs):
            assert query["event_key"].endswith(":stable-event")
            stored = event.model_dump()
            stored["seq"] = 14
            return {"seq": 14, "event": stored}

    class SessionCollection:
        async def find_one_and_update(self, *_args, **_kwargs):
            raise AssertionError("idempotent allocation must not increment the counter")

    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: EventCollection()),
    )
    monkeypatch.setattr(
        SessionEventReservationDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: reservation_collection),
    )
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: SessionCollection()),
    )
    assert await MongoSessionRepository().reserve_event_sequence("session-1", event) == 14
    assert event.seq == 14


@pytest.mark.asyncio
async def test_same_producer_id_concurrent_reservations_adopt_one_sequence(monkeypatch):
    class EventCollection:
        async def find_one(self, *_args, **_kwargs):
            return None

    class SessionCollection:
        def __init__(self):
            self.seq = 40
            self.lock = asyncio.Lock()

        async def find_one_and_update(self, *_args, **_kwargs):
            async with self.lock:
                self.seq += 1
                return {"event_seq": self.seq}

    reservations = MemoryReservationCollection(synchronize_initial_reads=2)
    sessions = SessionCollection()
    monkeypatch.setattr(
        SessionEventReservationDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: reservations),
    )
    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: EventCollection()),
    )
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: sessions),
    )
    first = MessageEvent(id="same-operation", role="assistant", message="answer")
    second = MessageEvent(id="same-operation", role="assistant", message="answer")

    sequences = await asyncio.gather(
        MongoSessionRepository().reserve_event_sequence("session-1", first),
        MongoSessionRepository().reserve_event_sequence("session-1", second),
    )

    assert sequences[0] == sequences[1]
    assert first.seq == second.seq == sequences[0]
    assert sessions.seq == 42  # The losing candidate is an intentional gap.
    assert len(reservations.documents) == 1


@pytest.mark.asyncio
async def test_same_producer_id_with_different_payload_fails_closed(monkeypatch):
    class EventCollection:
        async def find_one(self, *_args, **_kwargs):
            return None

    class SessionCollection:
        def __init__(self):
            self.seq = 50
            self.lock = asyncio.Lock()

        async def find_one_and_update(self, *_args, **_kwargs):
            async with self.lock:
                self.seq += 1
                return {"event_seq": self.seq}

    reservations = MemoryReservationCollection(synchronize_initial_reads=2)
    sessions = SessionCollection()
    monkeypatch.setattr(
        SessionEventReservationDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: reservations),
    )
    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: EventCollection()),
    )
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: sessions),
    )
    first = MessageEvent(id="same-operation", role="assistant", message="first")
    second = MessageEvent(id="same-operation", role="assistant", message="changed")

    results = await asyncio.gather(
        MongoSessionRepository().reserve_event_sequence("session-1", first),
        MongoSessionRepository().reserve_event_sequence("session-1", second),
        return_exceptions=True,
    )

    assert sum(isinstance(result, int) for result in results) == 1
    failure = next(result for result in results if isinstance(result, Exception))
    assert isinstance(failure, ValueError)
    assert "different payload" in str(failure)
    assert len(reservations.documents) == 1


@pytest.mark.asyncio
async def test_session_save_cannot_roll_back_sequence_or_message_claims(monkeypatch):
    existing = SimpleNamespace(title="Manual title", title_manually_set=True)

    class SessionCollection:
        def __init__(self):
            self.update = None

        async def update_one(self, query, update):
            self.update = (query, update)
            return SimpleNamespace(matched_count=1)

    collection = SessionCollection()
    monkeypatch.setattr(
        SessionDocument,
        "find_one",
        classmethod(lambda cls, *_args, **_kwargs: _async_value(existing)),
    )
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    session = Session(
        id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        title="stale automatic title",
    )

    await MongoSessionRepository().save(session)

    query, update = collection.update
    assert query == {"session_id": "session-1"}
    assert update["$set"]["title"] == "Manual title"
    assert update["$set"]["title_manually_set"] is True
    assert "event_seq" not in update["$set"]
    assert "client_message_ids" not in update["$set"]


@pytest.mark.asyncio
async def test_session_delete_cascades_event_reservations_before_parent(monkeypatch):
    from app.infrastructure.models.model_trace import ModelTraceDocument
    order = []

    class ChildCollection:
        def __init__(self, name):
            self.name = name

        async def delete_many(self, query):
            assert query == {"session_id": "session-1"}
            order.append(self.name)

    class StoredSession:
        async def delete(self):
            order.append("session")

    monkeypatch.setattr(
        SessionDocument,
        "find_one",
        classmethod(lambda cls, *_args, **_kwargs: _async_value(StoredSession())),
    )
    monkeypatch.setattr(
        ExecutionEnvironmentSnapshotDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: ChildCollection("snapshots")),
    )
    monkeypatch.setattr(
        SessionEventReservationDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: ChildCollection("reservations")),
    )
    monkeypatch.setattr(
        SessionEventDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: ChildCollection("events")),
    )

    monkeypatch.setattr(ModelTraceDocument, "get_pymongo_collection", classmethod(lambda cls: ChildCollection("model_traces")))
    await MongoSessionRepository().delete("session-1")

    assert order == ["model_traces", "snapshots", "reservations", "events", "session"]


@pytest.mark.asyncio
async def test_runner_assigns_sequence_before_redis_payload():
    order = []
    producer_ids = []

    class Repository:
        async def reserve_event_sequence(self, session_id, event):
            assert session_id == "session-1"
            order.append("reserve")
            producer_ids.append(event.bind_producer_event_id())
            event.seq = 31
            return 31

        async def add_event(self, session_id, event):
            assert session_id == "session-1"
            assert event.seq == 31
            assert event.id == producer_ids[0]
            assert event.bind_producer_event_id() == producer_ids[0]
            order.append("persist")

        async def record_event_transport_alias(self, session_id, event):
            assert session_id == "session-1"
            assert event.seq == 31
            assert event.id == "1700000000000-4"
            assert event.bind_producer_event_id() == producer_ids[0]
            order.append("alias")

    class Queue:
        async def put(self, payload):
            order.append("redis")
            decoded = json.loads(payload)
            assert decoded["seq"] == 31
            assert decoded["id"] == producer_ids[0]
            assert "producer_event_id" not in decoded
            assert "_producer_event_id" not in decoded
            return "1700000000000-4"

    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id = "session-1"
    runner._session_repository = Repository()
    task = SimpleNamespace(output_stream=Queue())

    await runner._publish(task, DoneEvent())

    assert order == ["reserve", "persist", "redis", "alias"]


def test_private_producer_identity_is_absent_from_recordings():
    event = DoneEvent(id="private-producer-operation", seq=1)
    assert event.bind_producer_event_id() == "private-producer-operation"
    event.id = "1700000000000-4"

    encoded_event = event.model_dump_json()
    recording = create_event_recording(
        session_id="session-1",
        events=[event],
    ).to_jsonl()

    assert "private-producer-operation" not in encoded_event
    assert "private-producer-operation" not in recording
    assert "_producer_event_id" not in recording
    assert '"id":"1700000000000-4"' in recording


class ResumeRepository:
    def __init__(self, replay_events):
        self.session = Session(
            id="session-1",
            user_id="user-1",
            agent_id="agent-1",
            task_id="task-1",
            status=SessionStatus.RUNNING,
        )
        self.replay_events = replay_events

    async def find_by_id_and_user_id(self, session_id, user_id):
        assert (session_id, user_id) == ("session-1", "user-1")
        return self.session

    async def update_unread_message_count(self, _session_id, _count):
        return None

    async def get_events_after(self, _session_id, seq):
        return [event for event in self.replay_events if (event.seq or 0) > seq]

    async def add_event(self, *_args):
        return None


def resume_service(repository, task):
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = repository

    async def get_task(_session):
        return task

    service._get_task = get_task
    return service


@pytest.mark.asyncio
async def test_resume_replays_persisted_output_but_not_user_messages():
    repository = ResumeRepository([
        MessageEvent(seq=2, role="user", message="question"),
        MessageEvent(seq=3, role="assistant", message="answer"),
        DoneEvent(seq=4),
    ])
    service = resume_service(repository, SimpleNamespace(done=True))

    events = [
        event
        async for event in service.chat(
            "session-1",
            "user-1",
            latest_event_id="1700000000000-1",
            latest_event_seq=1,
        )
    ]

    assert [event.seq for event in events] == [3, 4]
    assert [event.type for event in events] == ["message", "done"]


@pytest.mark.asyncio
async def test_resume_deduplicates_persisted_and_redis_overlap():
    replayed = MessageEvent(
        id="1700000000000-2",
        seq=2,
        role="assistant",
        message="answer",
    )
    repository = ResumeRepository([replayed])

    class Queue:
        def __init__(self):
            self.calls = []
            self.items = [
                ("1700000000000-2", replayed.model_dump_json()),
                ("1700000000000-3", DoneEvent(seq=3).model_dump_json()),
            ]

        async def get(self, start_id, block_ms):
            self.calls.append((start_id, block_ms))
            return self.items.pop(0)

    class Task:
        def __init__(self):
            self.output_stream = Queue()

        @property
        def done(self):
            return not self.output_stream.items

    task = Task()
    service = resume_service(repository, task)

    events = [
        event
        async for event in service.chat(
            "session-1",
            "user-1",
            latest_event_id="1700000000000-1",
            latest_event_seq=1,
        )
    ]

    assert [event.seq for event in events] == [2, 3]
    assert task.output_stream.calls == [
        ("0-0", 1000),
        ("1700000000000-2", 1000),
    ]


@pytest.mark.asyncio
async def test_resume_drains_redis_when_task_finishes_after_history_query():
    assistant = MessageEvent(
        seq=2,
        role="assistant",
        message="answer produced during handoff",
    )

    class Queue:
        def __init__(self):
            self.calls = []
            self.items = [
                ("1700000000000-2", assistant.model_dump_json()),
                ("1700000000000-3", DoneEvent(seq=3).model_dump_json()),
            ]

        async def get(self, start_id, block_ms):
            self.calls.append((start_id, block_ms))
            if not self.items:
                return None, None
            return self.items.pop(0)

    task = SimpleNamespace(done=False, output_stream=Queue())

    class RacingRepository(ResumeRepository):
        async def get_events_after(self, _session_id, _seq):
            # Simulate the runner completing after Mongo was queried but before
            # chat() attaches to the task's Redis output stream.
            task.done = True
            return []

    service = resume_service(RacingRepository([]), task)
    events = [
        event
        async for event in service.chat(
            "session-1",
            "user-1",
            latest_event_seq=1,
        )
    ]

    assert [event.seq for event in events] == [2, 3]
    assert task.output_stream.calls == [
        ("0-0", None),
        ("1700000000000-2", None),
    ]


@pytest.mark.asyncio
async def test_resume_rechecks_redis_when_task_finishes_at_read_timeout():
    assistant = MessageEvent(
        seq=2,
        role="assistant",
        message="answer written just after timeout",
    )

    class Queue:
        def __init__(self):
            self.calls = []
            self.items = []

        async def get(self, start_id, block_ms):
            self.calls.append((start_id, block_ms))
            if len(self.calls) == 1:
                task.done = True
                self.items.extend([
                    ("1700000000000-2", assistant.model_dump_json()),
                    ("1700000000000-3", DoneEvent(seq=3).model_dump_json()),
                ])
                return None, None
            return self.items.pop(0) if self.items else (None, None)

    task = SimpleNamespace(done=False)
    task.output_stream = Queue()
    service = resume_service(ResumeRepository([]), task)

    events = [
        event
        async for event in service.chat(
            "session-1",
            "user-1",
            latest_event_seq=1,
        )
    ]

    assert [event.seq for event in events] == [2, 3]
    assert task.output_stream.calls == [
        ("0-0", 1000),
        ("0-0", None),
        ("1700000000000-2", None),
    ]


def test_chat_request_sequence_is_optional_and_bounded():
    assert ChatRequest(message="hello").event_seq is None
    assert ChatRequest(message="hello", event_seq=0).event_seq == 0
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", event_seq=-1)
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", event_seq=True)
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", event_seq=1.0)
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", event_seq=MAX_EVENT_SEQUENCE + 1)


@pytest.mark.asyncio
async def test_stream_error_is_sequenced_before_being_yielded():
    class FailingRepository:
        def __init__(self):
            self.persisted = []

        async def find_by_id_and_user_id(self, *_args):
            raise RuntimeError("database unavailable")

        async def add_event(self, _session_id, event):
            event.seq = 1
            self.persisted.append(event)

    repository = FailingRepository()
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = repository

    events = [
        event
        async for event in service.chat("session-1", "user-1")
    ]

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].seq == 1
    assert repository.persisted == events
