"""Deterministic indexed history windows; no live database/model required."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import MessageEvent, ToolEvent, ToolStatus
from app.domain.models.session_history import page_legacy_history
from app.infrastructure.models.documents import SessionEventDocument
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.interfaces.api.session_routes import get_session_history
from app.application.errors.exceptions import NotFoundError


def _matches(document, query):
    for key, expected in query.items():
        value = document
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(expected, dict):
            for op, bound in expected.items():
                if op == "$lt" and not value < bound:
                    return False
                if op == "$lte" and not value <= bound:
                    return False
                if op == "$gte" and not value >= bound:
                    return False
        elif value != expected:
            return False
    return True


class Collection:
    def __init__(self, documents, *, legacy=False):
        self.documents = documents
        self.legacy = legacy
        self.rows_returned = 0
        self.queries = []
        self.after_watermark = None

    async def find_one(self, query, projection, **kwargs):
        self.queries.append((query, projection))
        if "$or" in query:
            return {"_id": "legacy"} if self.legacy else None
        selected = [item for item in self.documents if _matches(item, query)]
        selected.sort(key=lambda item: item["seq"], reverse=True)
        result = selected[0].copy() if selected else None
        self.rows_returned += int(result is not None)
        if self.after_watermark:
            self.after_watermark()
            self.after_watermark = None
        return result

    def find(self, query, projection):
        self.queries.append((query, projection))
        return Cursor(self, [item for item in self.documents if _matches(item, query)])


class Cursor:
    def __init__(self, owner, documents):
        self.owner, self.documents = owner, documents
    def sort(self, field, direction):
        self.documents.sort(key=lambda item: item[field], reverse=direction == -1)
        return self
    def limit(self, value):
        self.documents = self.documents[:value]
        return self
    async def to_list(self, length=None):
        result = self.documents if length is None else self.documents[:length]
        self.owner.rows_returned += len(result)
        return result


def _document(seq, *, role="assistant", session_id="s"):
    return {"session_id": session_id, "seq": seq, "event": {
        "id": f"event-{seq}", "seq": seq, "type": "message", "role": role, "message": f"message {seq}",
    }}


def _repository(monkeypatch, collection):
    monkeypatch.setattr(SessionEventDocument, "get_pymongo_collection", classmethod(lambda cls: collection))
    return MongoSessionRepository()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1000, 10000, 50000])
async def test_first_page_document_transfer_does_not_grow_with_history(monkeypatch, size):
    collection = Collection([_document(i, role="user" if i % 10 == 1 else "assistant")
                             for i in range(1, size + 1)])
    repo = _repository(monkeypatch, collection)
    repo.get_events = AsyncMock(side_effect=AssertionError("no full-history load"))
    page = await repo.get_history_page("s")
    assert len(page.events) == 50
    assert page.has_more is True
    assert page.next_before_seq == size - 49
    assert collection.rows_returned == 57  # watermark + six boundary IDs + 50 event bodies
    assert all(query.get("session_id") == "s" for query, _ in collection.queries)


@pytest.mark.asyncio
async def test_whole_turn_pages_replay_exactly_without_overlap_or_tool_splitting(monkeypatch):
    docs = []
    for turn in range(12):
        seq = turn * 4 + 1
        docs.append(_document(seq, role="user"))
        for offset, status in [(1, ToolStatus.CALLING), (2, ToolStatus.CALLED)]:
            event = ToolEvent(id=f"tool-{seq+offset}", seq=seq + offset, tool_call_id=f"call-{turn}",
                              tool_name="shell", function_name="read", function_args={}, status=status)
            docs.append({"session_id": "s", "seq": seq + offset, "event": event.model_dump()})
        docs.append(_document(seq + 3))
    collection = Collection(docs + [_document(900, role="user", session_id="other")])
    repo = _repository(monkeypatch, collection)
    restored = []
    before = None
    while True:
        page = await repo.get_history_page("s", turns=5, before_seq=before)
        assert page.events[0].role == "user"
        tools = [event for event in page.events if isinstance(event, ToolEvent)]
        assert [tool.tool_call_id for tool in tools[::2]] == [tool.tool_call_id for tool in tools[1::2]]
        restored = page.events + restored
        if not page.has_more:
            break
        assert page.next_before_seq is not None
        assert before is None or page.next_before_seq < before
        before = page.next_before_seq
    assert [event.seq for event in restored] == list(range(1, 49))


@pytest.mark.asyncio
async def test_history_page_freezes_watermark_during_live_append(monkeypatch):
    collection = Collection([_document(i, role="user" if i % 2 else "assistant") for i in range(1, 21)])
    collection.after_watermark = lambda: collection.documents.append(_document(21, role="user"))
    page = await _repository(monkeypatch, collection).get_history_page("s", turns=2)
    assert [event.seq for event in page.events] == [17, 18, 19, 20]


@pytest.mark.asyncio
async def test_legacy_sequence_projection_is_preserved(monkeypatch):
    repo = _repository(monkeypatch, Collection([], legacy=True))
    events = [MessageEvent(seq=i, role="user" if i % 2 else "assistant", message=str(i)) for i in range(1, 9)]
    repo.get_events = AsyncMock(return_value=events)
    page = await repo.get_history_page("s", turns=2)
    assert [event.seq for event in page.events] == [5, 6, 7, 8]
    older = page_legacy_history(events, turns=2, before_seq=page.next_before_seq)
    assert [event.seq for event in older.events] == [1, 2, 3, 4]
    assert not older.has_more


@pytest.mark.asyncio
async def test_large_single_turn_is_not_silently_truncated(monkeypatch):
    repo = _repository(monkeypatch, Collection([_document(i, role="user" if i == 1 else "assistant")
                                               for i in range(1, 1002)]))
    page = await repo.get_history_page("s", turns=1)
    assert len(page.events) == 1001
    assert not page.has_more


@pytest.mark.asyncio
async def test_history_auth_denial_never_reads_events():
    service = SimpleNamespace(get_session=AsyncMock(return_value=None), get_session_history=AsyncMock())
    with pytest.raises(NotFoundError):
        await get_session_history("private", turns=5, before_seq=None,
                                  current_user=SimpleNamespace(id="other"), agent_service=service)
    service.get_session_history.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("turns,before", [(0, None), (21, None), (True, None), (1, 0), (1, True), (1, 2**54)])
async def test_history_rejects_invalid_limits_without_database_access(turns, before):
    with pytest.raises(ValueError):
        await MongoSessionRepository().get_history_page("s", turns=turns, before_seq=before)
