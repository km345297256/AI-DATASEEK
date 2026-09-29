"""Read-only cache observations never alter billing totals, requests or quotas."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.models.model_trace import ModelTraceRecord, ModelTraceView
from app.domain.models.usage import TokenUsageRecord
from app.domain.services import model_runtime as runtime
from app.domain.services.token_usage_service import TokenUsageService
from app.infrastructure.models.documents import TokenUsageDocument
from test_model_runtime import TraceStore


def message(*, details=None, wire=None, normalized=True):
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    if details is not None:
        usage["input_token_details"] = details
    return AIMessage(content="synthetic response", usage_metadata=usage if normalized else None,
                     response_metadata={"token_usage": wire} if wire is not None else {})


@pytest.mark.parametrize("details,wire,expected", [
    (None, None, (None, None)),
    ({"cache_read": 0, "cache_creation": 0}, None, (0, 0)),
    ({"cache_read": 50, "cache_creation": 12}, None, (50, 12)),
    (None, {"prompt_tokens_details": {"cached_tokens": 50}}, (50, None)),
    (None, {"input_tokens_details": {"cached_tokens": 50}}, (50, None)),
    (None, {"prompt_cache_hit_tokens": 50, "prompt_cache_miss_tokens": 50}, (50, None)),
    (None, {"prompt_cache_miss_tokens": 100}, (None, None)),
    (None, {"cache_read_input_tokens": 50, "cache_creation_input_tokens": 12}, (50, 12)),
    ({"cache_read": 50}, {"prompt_cache_hit_tokens": 50,
                           "prompt_tokens_details": {"cached_tokens": 50}}, (50, None)),
    ({"cache_read": 50}, {"prompt_cache_hit_tokens": 49}, (None, None)),
    ({"cache_creation": 12}, {"cache_creation_input_tokens": 13}, (None, None)),
    ({"cache_read": 50, "cache_creation": 12}, {"prompt_cache_hit_tokens": 49}, (None, 12)),
    (None, {"prompt_tokens_details": None}, (None, None)),
    (None, {"prompt_cache_hit_tokens": None}, (None, None)),
])
def test_explicit_semantics_aliases_and_unknown_are_preserved(details, wire, expected):
    observation = TokenUsageService().extract_cache_usage(message(details=details, wire=wire))
    assert (observation["cache_read_tokens"], observation["cache_write_tokens"]) == expected


@pytest.mark.parametrize("invalid", [True, False, -1, 1.0, float("nan"), float("inf"), "5", {}, []])
def test_malformed_alias_cannot_become_zero_or_override_a_valid_alias(invalid):
    # Bypass SDK coercion deliberately: extraction must validate raw metadata.
    raw = SimpleNamespace(usage_metadata={"input_token_details": {"cache_read": 5}},
                          response_metadata={"token_usage": {"prompt_cache_hit_tokens": invalid}})
    assert TokenUsageService().extract_cache_usage(raw)["cache_read_tokens"] is None


def test_malformed_detail_container_and_unknown_metadata_are_not_retained():
    raw = SimpleNamespace(usage_metadata={"input_token_details": []}, response_metadata={
        "token_usage": {"prompt_cache_hit_tokens": 5, "private-provider-detail": "secret"}})
    result = TokenUsageService().extract_cache_usage(raw)
    assert result == {"cache_read_tokens": None, "cache_write_tokens": None}
    assert "secret" not in str(result)


def test_usage_fallback_and_provider_extension_do_not_change_totals():
    wire = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
    service = TokenUsageService()
    for normalized in (True, False):
        value = message(wire=wire, normalized=normalized)
        assert service.extract_usage(value) == {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        assert service.extract_cache_usage(value) == {"cache_read_tokens": 80, "cache_write_tokens": None}


def test_response_usage_container_and_unreported_counters_are_supported():
    raw = SimpleNamespace(usage_metadata=None, response_metadata={
        "usage": {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 7}})
    assert TokenUsageService().extract_cache_usage(raw) == {"cache_read_tokens": 0, "cache_write_tokens": 7}
    assert TokenUsageService().extract_cache_usage(object()) == {"cache_read_tokens": None, "cache_write_tokens": None}


@pytest.mark.asyncio
async def test_usage_record_stores_only_numbers_and_keeps_aggregate_semantics(monkeypatch):
    inserted = []

    class FakeDocument:
        def __init__(self, record):
            self.record = record

        async def insert(self):
            inserted.append(self.record)

        def to_domain(self):
            return self.record

    monkeypatch.setattr(TokenUsageDocument, "from_domain", FakeDocument)
    value = message(details={"cache_read": 80}, wire={"prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20, "secret": "untrusted-provider-metadata"})
    result = await TokenUsageService().record_from_message(value, model_provider="deepseek")
    assert len(inserted) == 1
    assert result.total_tokens == 110 and result.prompt_tokens == 100 and result.completion_tokens == 10
    assert result.cache_read_tokens == 80 and result.cache_write_tokens is None
    assert "untrusted-provider-metadata" not in result.model_dump_json()


@pytest.mark.parametrize("model,legacy", [
    (TokenUsageRecord, {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}),
    (ModelTraceRecord, {"user_id": "u", "session_id": "s", "task_id": "t", "schema_version": 1}),
])
def test_legacy_records_roundtrip_and_optional_counters_are_strict(model, legacy):
    record = model.model_validate(legacy)
    assert record.cache_read_tokens is None and record.cache_write_tokens is None
    assert model.model_validate_json(record.model_dump_json()) == record
    for invalid in (True, -1, "10", 1.0):
        with pytest.raises(ValidationError):
            model.model_validate({**legacy, "cache_read_tokens": invalid})


def test_document_mapping_keeps_cache_fields_without_database_access():
    record = TokenUsageRecord(prompt_tokens=100, total_tokens=100, cache_read_tokens=0, cache_write_tokens=8)
    data = record.model_dump()
    data["usage_id"] = data.pop("id")
    document = TokenUsageDocument.model_construct(**data)
    assert document.to_domain() == record
    assert {"cache_read_tokens", "cache_write_tokens"} <= TokenUsageDocument.model_fields.keys()


def test_tool_schema_identity_is_stable_order_sensitive_and_content_free():
    first = [{"type": "function", "function": {"name": "read", "description": "PRIVATE-SCHEMA"}}]
    reordered_keys = [{"function": {"description": "PRIVATE-SCHEMA", "name": "read"}, "type": "function"}]
    original = deepcopy(first)
    digest, count = runtime._tool_schema_identity(first)
    assert len(digest) == 64 and count == 1
    assert runtime._tool_schema_identity(reordered_keys) == (digest, count)
    assert first == original
    second = {"type": "function", "function": {"name": "write"}}
    assert runtime._tool_schema_identity(first + [second]) != runtime._tool_schema_identity([second] + first)
    assert runtime._tool_schema_identity([])[1] == 0
    assert runtime._tool_schema_identity(None) == (None, None)
    assert runtime._tool_schema_identity([{"bad": object()}]) == (None, None)
    assert runtime._tool_schema_identity([{"bad": float("nan")}]) == (None, None)


@pytest.fixture
def settings(monkeypatch):
    value = Settings(_env_file=None, api_key="synthetic-cache-test-key")
    monkeypatch.setattr(runtime, "get_settings", lambda: value)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: value)
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", AsyncMock())
    return value


async def invoke(handler, tools=()):
    return await runtime.invoke_model_request(messages=[HumanMessage(content="synthetic")],
        tool_schemas=tools, max_output_tokens=100, provider="deepseek", model_name="fixture", invoke=handler)


@pytest.mark.asyncio
async def test_runtime_cache_and_tools_observation_is_private_and_never_double_charged(settings):
    store = TraceStore()
    tools = [{"type": "function", "function": {"name": "read", "description": "PRIVATE-SCHEMA", "parameters": {"type": "object"}}}]
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="t", store=store) as scope:
        await invoke(AsyncMock(return_value=message(details={"cache_read": 80, "cache_creation": 10},
            wire={"prompt_cache_hit_tokens": 80})), tools)
        assert scope.ledger.charged_tokens == 110
        assert scope.ledger.call_limit is None and scope.ledger.token_limit is None
    record = next(iter(store.records.values()))
    assert record.cache_read_tokens == 80 and record.cache_write_tokens == 10
    assert record.actual_input_tokens == 100 and record.actual_output_tokens == 10 and record.actual_total_tokens == 110
    assert record.tool_schema_count == 1 and len(record.tool_schema_sha256) == 64
    assert store.history[0].tool_schema_sha256 == record.tool_schema_sha256
    assert store.history[0].cache_read_tokens is None
    assert "PRIVATE-SCHEMA" not in record.model_dump_json()
    for field in ("cache_read_tokens", "cache_write_tokens", "tool_schema_sha256", "tool_schema_count"):
        assert field not in record.public_view().model_dump()
        assert field not in ModelTraceView.model_json_schema()["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("private-error"), asyncio.CancelledError()])
async def test_failed_attempt_has_tool_identity_but_no_invented_cache_usage(settings, error):
    store = TraceStore()
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="t", store=store):
        with pytest.raises(type(error)):
            await invoke(AsyncMock(side_effect=error))
    record = next(iter(store.records.values()))
    assert record.status in {"failed", "cancelled"} and record.usage_source == "reservation"
    assert record.cache_read_tokens is None and record.cache_write_tokens is None
    assert record.tool_schema_count == 0 and len(record.tool_schema_sha256) == 64
    assert "private-error" not in record.model_dump_json()


@pytest.mark.asyncio
async def test_invalid_json_attempt_keeps_reported_cache_without_retry_or_double_accounting(settings):
    store = TraceStore()
    handler = AsyncMock(return_value=message(wire={"prompt_cache_hit_tokens": 0}))

    def invalid(_response):
        raise json.JSONDecodeError("synthetic", "x", 0)

    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="t", store=store) as scope:
        with runtime.model_response_validation(invalid):
            with pytest.raises(json.JSONDecodeError):
                await invoke(handler)
        assert scope.ledger.charged_tokens == 110
    record = next(iter(store.records.values()))
    assert record.status == "failed" and record.cache_read_tokens == 0
    assert record.cache_write_tokens is None and handler.await_count == 1


@pytest.mark.asyncio
async def test_missing_totals_preserve_reservation_and_report_only_explicit_cache(settings):
    store = TraceStore()
    response = AIMessage(content="synthetic", response_metadata={"token_usage": {"prompt_cache_hit_tokens": 0}})
    with runtime.model_execution_scope(user_id="u", session_id="s", task_id="t", store=store) as scope:
        await invoke(AsyncMock(return_value=response))
        assert scope.ledger.charged_tokens > 100
    record = next(iter(store.records.values()))
    assert record.actual_total_tokens is None and record.usage_source == "reservation"
    assert record.cache_read_tokens == 0 and record.cache_write_tokens is None
