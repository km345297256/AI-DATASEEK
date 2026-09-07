from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.models.model_trace import ModelTraceRecord
from app.infrastructure.repositories import mongo_model_trace_repository as repository_module
from app.infrastructure.repositories.mongo_model_trace_repository import MongoModelTraceRepository
from app.interfaces.api.model_trace_routes import router
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.infrastructure.repositories.mongo_model_trace_repository import get_model_trace_repository


def _document(
    trace_id: str,
    created_at: datetime,
    *,
    user_id: str = "user-1",
    session_id: str = "session-1",
    task_id: str = "task-1",
):
    record = ModelTraceRecord(
        trace_id=trace_id,
        user_id=user_id,
        session_id=session_id,
        task_id=task_id,
        status="succeeded",
        created_at=created_at,
    )
    return SimpleNamespace(
        trace_id=trace_id,
        user_id=user_id,
        session_id=session_id,
        record=record,
    )


class _FakeFind:
    def __init__(self, documents, query):
        self._documents = [item for item in documents if self._matches(item, query)]
        self._limit = len(self._documents)

    @staticmethod
    def _matches(document, query):
        if document.user_id != query.get("user_id"):
            return False
        if document.session_id != query.get("session_id"):
            return False
        if "record.task_id" in query and document.record.task_id != query["record.task_id"]:
            return False
        boundary = query.get("$or")
        if not boundary:
            return True
        created_at = boundary[0]["record.created_at"]["$lt"]
        cursor_trace_id = boundary[1]["trace_id"]["$lt"]
        return (
            document.record.created_at < created_at
            or (
                document.record.created_at == created_at
                and document.trace_id < cursor_trace_id
            )
        )

    def sort(self, *fields):
        assert fields == ("-record.created_at", "-trace_id")
        self._documents.sort(
            key=lambda item: (item.record.created_at, item.trace_id),
            reverse=True,
        )
        return self

    def limit(self, limit):
        self._limit = limit
        return self

    async def to_list(self):
        return self._documents[: self._limit]


class _FakeModelTraceDocument:
    documents = []

    @classmethod
    async def find_one(cls, query):
        return next(
            (
                item
                for item in cls.documents
                if item.trace_id == query.get("trace_id")
                and item.user_id == query.get("user_id")
                and item.session_id == query.get("session_id")
            ),
            None,
        )

    @classmethod
    def find(cls, query):
        return _FakeFind(cls.documents, query)


@pytest.mark.asyncio
async def test_repository_cursor_is_owner_scoped_and_stable_across_new_inserts(monkeypatch):
    now = datetime.now(UTC)
    newest = _document("f" * 32, now)
    tied_first = _document("e" * 32, now - timedelta(seconds=1))
    tied_second = _document("d" * 32, now - timedelta(seconds=1))
    oldest = _document("c" * 32, now - timedelta(seconds=2))
    foreign = _document("a" * 32, now, user_id="other-user")
    _FakeModelTraceDocument.documents = [oldest, foreign, tied_second, newest, tied_first]
    monkeypatch.setattr(repository_module, "ModelTraceDocument", _FakeModelTraceDocument)
    monkeypatch.setattr(
        repository_module,
        "get_settings",
        lambda: SimpleNamespace(model_trace_store_timeout_seconds=1),
    )
    repository = MongoModelTraceRepository()

    first_page = await repository.list_for_owner("user-1", "session-1", limit=2)
    assert [item.trace_id for item in first_page] == ["f" * 32, "e" * 32]

    _FakeModelTraceDocument.documents.append(
        _document("9" * 32, now + timedelta(seconds=1))
    )
    second_page = await repository.list_for_owner(
        "user-1",
        "session-1",
        limit=2,
        before=first_page[-1].trace_id,
    )
    assert [item.trace_id for item in second_page] == ["d" * 32, "c" * 32]
    assert not ({item.trace_id for item in first_page} & {item.trace_id for item in second_page})

    assert await repository.list_for_owner(
        "user-1",
        "session-1",
        limit=2,
        before=foreign.trace_id,
    ) == []


def test_trace_api_passes_cursor_only_when_present_and_returns_last_full_page_id():
    traces = [
        _document("f" * 32, datetime.now(UTC)).record.public_view(),
        _document("e" * 32, datetime.now(UTC)).record.public_view(),
    ]
    agents = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(user_id="user-1"))
    )
    repository = SimpleNamespace(list_for_owner=AsyncMock(return_value=traces))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_agent_service] = lambda: agents
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-1")
    app.dependency_overrides[get_model_trace_repository] = lambda: repository

    with TestClient(app) as client:
        response = client.get(f"/sessions/session-1/model-traces?limit=2&before={'a' * 32}")
        assert response.status_code == 200
        assert response.json()["data"]["next_cursor"] == "e" * 32
        repository.list_for_owner.assert_awaited_once_with(
            "user-1",
            "session-1",
            task_id=None,
            limit=2,
            before="a" * 32,
        )

        repository.list_for_owner.reset_mock()
        repository.list_for_owner.return_value = traces[:1]
        response = client.get("/sessions/session-1/model-traces?limit=2")
        assert response.status_code == 200
        assert response.json()["data"]["next_cursor"] is None
        repository.list_for_owner.assert_awaited_once_with(
            "user-1",
            "session-1",
            task_id=None,
            limit=2,
        )

        repository.list_for_owner.reset_mock()
        assert client.get("/sessions/session-1/model-traces?before=not-a-cursor").status_code == 422
        repository.list_for_owner.assert_not_awaited()
