import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage

from app.core.config import Settings
from app.domain.models.model_trace import ModelTraceRecord
from app.domain.services import model_runtime as runtime
from app.infrastructure.repositories import mongo_model_trace_repository as repository_module
from app.infrastructure.repositories.mongo_model_trace_repository import MongoModelTraceRepository


def _collection(monkeypatch, update):
    collection = SimpleNamespace(update_one=update)
    monkeypatch.setattr(repository_module, "ModelTraceDocument", SimpleNamespace(
        get_pymongo_collection=lambda: collection,
    ))
    # A short legacy metrics timeout must not govern required audit storage.
    monkeypatch.setattr(repository_module, "get_settings", lambda: SimpleNamespace(
        model_trace_store_timeout_seconds=0.001,
    ))


@pytest.mark.asyncio
async def test_required_trace_persistence_waits_past_metrics_timeout(monkeypatch):
    async def update(*args, **kwargs):
        await asyncio.sleep(0.02)

    writer = AsyncMock(side_effect=update)
    _collection(monkeypatch, writer)
    record = ModelTraceRecord(user_id="u", session_id="s", task_id="t")

    await MongoModelTraceRepository().put(record)

    writer.assert_awaited_once_with(
        {"trace_id": record.trace_id, "user_id": "u", "session_id": "s"},
        {"$set": {"record": record.model_dump(mode="python")}}, upsert=True,
    )


@pytest.mark.asyncio
async def test_required_trace_persistence_preserves_explicit_cancellation(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def update(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _collection(monkeypatch, update)
    task = asyncio.create_task(MongoModelTraceRepository().put(
        ModelTraceRecord(user_id="u", session_id="s", task_id="t"),
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_model_admission_and_settlement_await_slow_durable_repository(monkeypatch):
    settings = Settings(_env_file=None, api_key="synthetic-test-key")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", AsyncMock())
    persisted = []

    async def update(query, mutation, **kwargs):
        await asyncio.sleep(0.02)
        persisted.append(mutation["$set"]["record"])

    _collection(monkeypatch, update)

    async def invoke(messages, max_tokens):
        assert [record["status"] for record in persisted] == ["started"]
        return AIMessage(content="done", usage_metadata={
            "input_tokens": 10, "output_tokens": 1, "total_tokens": 11,
        })

    with runtime.model_execution_scope(
        user_id="u", session_id="s", task_id="t", store=MongoModelTraceRepository(),
    ):
        result = await runtime.invoke_model_request(
            messages=[HumanMessage(content="synthetic request")],
            max_output_tokens=100, provider="deepseek", model_name="fixture", invoke=invoke,
        )
    assert result.content == "done"
    assert [record["status"] for record in persisted] == ["started", "succeeded"]
