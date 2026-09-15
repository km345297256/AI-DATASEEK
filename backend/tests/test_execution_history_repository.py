"""Incremental execution cache membership, cut consistency and privacy tests."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import MessageEvent
from app.domain.services.execution_history import ExecutionHistory
from app.infrastructure.models.documents import SessionDocument, SessionEventDocument
from app.infrastructure.repositories import mongo_session_repository as module


def get(document, key):
    value = document
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(document, item) for item in expected):
                return False
            continue
        value = get(document, key)
        if not isinstance(expected, dict):
            if value != expected:
                return False
            continue
        for op, bound in expected.items():
            if op == "$ne" and value == bound:
                return False
            if op == "$not" and "$type" in bound and type(value) is int:
                return False
            if op == "$lt" and (value is None or not value < bound):
                return False
            if op == "$lte" and (value is None or not value <= bound):
                return False
            if op == "$gt" and (value is None or not value > bound):
                return False
    return True


class Cursor:
    def __init__(self, collection, documents):
        self.collection = collection
        self.documents = documents

    def sort(self, key, order):
        self.documents.sort(key=lambda item: item[key], reverse=order < 0)
        return self

    def batch_size(self, size):
        assert size == 256
        return self

    async def __aiter__(self):
        for document in self.documents:
            self.collection.body_reads += 1
            yield copy.deepcopy(document)
        if self.collection.after_read:
            callback, self.collection.after_read = self.collection.after_read, None
            callback()


class Events:
    def __init__(self, documents):
        self.documents = documents
        self.body_reads = 0
        self.queries = []
        self.after_read = None

    async def find_one(self, query, projection):
        self.queries.append(query)
        return next((item for item in self.documents if matches(item, query)), None)

    def find(self, query, projection):
        self.queries.append(query)
        return Cursor(self, [item for item in self.documents if matches(item, query)])

    async def count_documents(self, query):
        self.queries.append(query)
        return sum(matches(item, query) for item in self.documents)


class Sessions:
    def __init__(self):
        self.document = {"session_id": "s", "user_id": "owner"}
        self.updates = []

    async def find_one(self, query, projection):
        return copy.deepcopy(self.document) if matches(self.document, query) else None

    async def update_one(self, query, change):
        self.updates.append(change)
        match = matches(self.document, query)
        if match:
            self.document.update(copy.deepcopy(change["$set"]))
        return SimpleNamespace(matched_count=int(match))


def doc(seq, *, session_id="s", message=None):
    event = MessageEvent(seq=seq, message=message or f"message {seq}", role="user" if seq % 2 else "assistant")
    return {"session_id": session_id, "seq": seq, "event": event.model_dump(mode="json")}


def repository(monkeypatch, documents):
    events, sessions = Events(documents), Sessions()
    monkeypatch.setattr(SessionEventDocument, "get_pymongo_collection", classmethod(lambda cls: events))
    monkeypatch.setattr(SessionDocument, "get_pymongo_collection", classmethod(lambda cls: sessions))
    repo = module.MongoSessionRepository()
    repo.get_events = AsyncMock(side_effect=AssertionError("unexpected full-history fallback"))
    return repo, events, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1000, 10000, 50000])
async def test_warm_cache_reads_only_new_bodies_and_excludes_future_inputs(monkeypatch, size):
    repo, events, sessions = repository(monkeypatch, [doc(i) for i in range(1, size + 1)])
    first = await repo.get_execution_history("s", before_seq=size)
    assert (first.seq, first.event_count, events.body_reads) == (size - 1, size - 1, size - 1)
    events.documents.extend(doc(i) for i in range(size + 1, size + 12))
    events.documents.append(doc(1, session_id="other", message="private other session"))
    events.body_reads = 0
    second = await repo.get_execution_history("s", before_seq=size + 10)
    assert events.body_reads == 10
    assert second.seq == size + 9
    assert len(second.recent_messages) == 6
    assert "private other session" not in second.model_dump_json()
    assert all(query["session_id"] == "s" for query in events.queries)
    assert all(set(update["$set"]) == {"execution_history_projection"} for update in sessions.updates)


@pytest.mark.asyncio
async def test_late_commit_inside_cached_prefix_invalidates_membership(monkeypatch):
    repo, events, _ = repository(monkeypatch, [doc(1), doc(3)])
    await repo.get_execution_history("s", before_seq=4)
    events.documents.append(doc(2, message="late committed answer"))
    events.body_reads = 0
    state = await repo.get_execution_history("s", before_seq=4)
    assert events.body_reads == 3
    assert state.event_count == 3
    assert "late committed answer" in state.model_dump_json()


@pytest.mark.asyncio
async def test_prefix_race_during_suffix_read_rebuilds_same_request(monkeypatch):
    repo, events, _ = repository(monkeypatch, [doc(1), doc(3)])
    await repo.get_execution_history("s", before_seq=4)
    events.after_read = lambda: events.documents.append(doc(2, message="racing answer"))
    state = await repo.get_execution_history("s", before_seq=4)
    assert state.event_count == 3
    assert "racing answer" in state.model_dump_json()


@pytest.mark.asyncio
async def test_earlier_cut_does_not_reuse_future_projection(monkeypatch):
    repo, _, sessions = repository(monkeypatch, [doc(i) for i in range(1, 10)])
    await repo.get_execution_history("s", before_seq=10)
    earlier = await repo.get_execution_history("s", before_seq=5)
    assert earlier.seq == 4
    assert all(event.seq < 5 for event in earlier.conversation)
    assert sessions.document["execution_history_projection"]["seq"] == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [{"version": 999}, {"version": 1, "seq": -1}, {"seq": 8, "event_count": 123}])
async def test_invalid_or_nonmatching_cache_rebuilds(monkeypatch, cache):
    repo, _, sessions = repository(monkeypatch, [doc(i) for i in range(1, 5)])
    sessions.document["execution_history_projection"] = cache
    state = await repo.get_execution_history("s", before_seq=5)
    assert state.seq == 4
    assert state.event_count == 4


@pytest.mark.asyncio
async def test_cache_size_limit_does_not_truncate_history_or_rewrite_log(monkeypatch):
    repo, events, sessions = repository(monkeypatch, [doc(i) for i in range(1, 10)])
    original = copy.deepcopy(events.documents)
    monkeypatch.setattr(module, "EXECUTION_HISTORY_CACHE_BYTES", 1)
    state = await repo.get_execution_history("s", before_seq=10)
    assert state.seq == 9
    assert state.event_count == 9
    assert not sessions.updates
    assert original == events.documents


@pytest.mark.asyncio
async def test_cache_write_failure_is_not_execution_failure(monkeypatch):
    repo, _, sessions = repository(monkeypatch, [doc(1)])
    sessions.update_one = AsyncMock(side_effect=RuntimeError("cache unavailable"))
    state = await repo.get_execution_history("s", before_seq=2)
    assert state.seq == 1


@pytest.mark.asyncio
async def test_legacy_uses_original_synthesis_without_caching(monkeypatch):
    repo, _, sessions = repository(monkeypatch, [{"session_id": "s", "seq": None, "event": {}}])
    repo.get_events = AsyncMock(return_value=[MessageEvent(seq=i, role="user", message=str(i)) for i in range(1, 5)])
    state = await repo.get_execution_history("s", before_seq=3)
    assert state.seq == 2
    assert not sessions.updates
    repo.get_events.assert_awaited_once_with("s")


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [0, -1, True, 2**54, "4"])
async def test_invalid_cursor_is_rejected_before_database_access(cursor):
    with pytest.raises(ValueError):
        await module.MongoSessionRepository().get_execution_history("s", before_seq=cursor)


def test_cache_is_omitted_from_ordinary_session_database_and_api_projections():
    from beanie.odm.utils.projection import get_projection
    from app.domain.models.session import Session
    from app.domain.models.message import Message
    # Do not make normal session lookups/lists transfer the cache blob either.
    assert "execution_history_projection" not in get_projection(SessionDocument)
    assert "execution_history_projection" not in Session.model_json_schema()["properties"]
    message = Message(message="question")
    message._session_events_snapshot = ExecutionHistory()
    assert "session_events_snapshot" not in message.model_dump_json()
