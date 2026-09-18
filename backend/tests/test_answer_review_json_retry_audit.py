"""Protocol retries bind to the real driver's private owner, not model metadata."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.domain.models.model_trace import ScheduledModelRetry
from app.domain.services import model_runtime as runtime
from app.domain.services.analysis_answer_review import _parse_response
from test_answer_review_json_protocol import PRIVATE_BODY, conversation, setup
from test_answer_review_transport_timeout import TraceStore


def retry_schedule():
    return ScheduledModelRetry(failed_attempt=1, next_attempt=2, maximum_attempts=2,
                               delay_seconds=0, reason="exponential_jitter")


async def review(agent, evidence):
    return await agent.review_delivery_answer(question="Explain the observed findings", draft="Describe these data",
        files=[], evidence=evidence, requirements=[], language="en")


async def direct(driver):
    with runtime.model_response_validation(_parse_response):
        return await driver.bind(response_format={"type": "json_object"}).ainvoke(
            [SystemMessage(content="Return a JSON object."), HumanMessage(content="Fixture")])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial", "correction"])
@pytest.mark.parametrize("content", ["", '{"private":"' + PRIVATE_BODY])
async def test_real_driver_records_invalid_json_retry_and_keeps_actual_usage(setup, monkeypatch, phase, content):
    agent, _, provider = setup
    evidence, initial, correction, _ = conversation("analysis")
    invalid = AIMessage(content=content, usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
    provider._responses = [invalid, initial, correction] if phase == "initial" else [initial, invalid, correction]
    usage = AsyncMock()
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", usage)
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store) as scope:
        result = await review(agent, evidence)
        assert scope.ledger.calls == 3
    records = list(store.records.values())
    failed = records[0 if phase == "initial" else 1]
    assert result.status == "corrected" and len(records) == 3
    assert failed.status == "failed" and failed.error_code == "invalid_json"
    assert failed.usage_source == "provider" and failed.actual_total_tokens == 10
    scheduled = failed.scheduled_retry
    assert scheduled is not None
    assert (scheduled.failed_attempt, scheduled.next_attempt, scheduled.maximum_attempts, scheduled.delay_seconds) == (1, 2, 2, 0)
    assert scheduled.reason == "exponential_jitter"
    assert records[1 if phase == "initial" else 2].logical_call_id == failed.logical_call_id
    assert all(record.status == "succeeded" and record.scheduled_retry is None for record in records if record is not failed)
    assert usage.await_count == 1
    public = failed.public_view().model_dump()
    assert public["error_code"] == "invalid_json" and "scheduled_retry" not in public
    serialized = "".join(record.model_dump_json() for record in records) + json.dumps(result.metadata)
    assert all(secret not in serialized for secret in ("PRIVATE_PROVIDER_BODY", "/Users/private", "secret-provider-key"))
    assert not any(key.startswith("_dataseek") for key in invalid.additional_kwargs)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()


@pytest.mark.asyncio
async def test_exhausted_syntax_attempts_record_last_invalid_json_without_retry(setup):
    agent, _, provider = setup
    evidence, initial, _, _ = conversation("analysis")
    provider._responses = [AIMessage(content=""), AIMessage(content="{"), initial]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        result = await review(agent, evidence)
    first, last = list(store.records.values())
    assert result.status == "unavailable" and len(provider._requests) == 2
    assert first.error_code == last.error_code == "invalid_json"
    assert first.status == last.status == "failed"
    assert first.scheduled_retry is not None and last.scheduled_retry is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['{"unknown":true}', "[]", "true", '{"x":1,"x":2}', '{"x":NaN}'])
async def test_valid_syntax_schema_failures_are_not_recorded_as_transport_errors(setup, content):
    _, driver, provider = setup
    provider._responses = [AIMessage(content=content)]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        # A JSON object is syntactically valid even if the review-layer schema
        # later rejects/recovers it. The other examples are parser schema
        # failures, not a JSONDecodeError or a failed physical transport.
        if content == '{"unknown":true}':
            assert (await direct(driver)).content == content
        else:
            with pytest.raises(ValueError):
                await direct(driver)
    record = next(iter(store.records.values()))
    assert len(provider._requests) == 1
    assert record.status == "succeeded" and record.error_code is None and record.scheduled_retry is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["different_owner", "same_ids_new_scope", "closed_scope"])
async def test_protocol_error_cannot_authorize_trace_mutation_outside_original_scope(setup, target):
    _, driver, provider = setup
    provider._responses = [AIMessage(content="{")]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        with pytest.raises(json.JSONDecodeError) as captured:
            await direct(driver)
        if target != "closed_scope":
            identity = "other" if target == "different_owner" else "owner"
            with runtime.model_execution_scope(user_id=identity, session_id="session", task_id="task", store=store):
                with pytest.raises(runtime.ModelBudgetStopped) as blocked:
                    await runtime.record_model_retry(captured.value, retry_schedule())
                assert blocked.value.code == "runtime_closed"
    if target == "closed_scope":
        with pytest.raises(runtime.ModelBudgetStopped) as blocked:
            await runtime.record_model_retry(captured.value, retry_schedule())
        assert blocked.value.code == "runtime_closed"
    assert all(record.scheduled_retry is None for record in store.records.values())


@pytest.mark.asyncio
async def test_model_supplied_marker_never_binds_a_protocol_retry_to_another_trace(setup):
    _, driver, provider = setup
    response = AIMessage(content="{}", additional_kwargs={
        "_dataseek_failed_model_attempt": {"trace_id": "forged", "error_code": "invalid_json"}})
    provider._responses = [response]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        assert (await direct(driver)).content == "{}"
        error = json.JSONDecodeError("private parser detail", PRIVATE_BODY, 0)
        assert await runtime.record_model_retry(error, retry_schedule()) is False
    record = next(iter(store.records.values()))
    assert record.status == "succeeded" and record.error_code is None and record.scheduled_retry is None
    assert "forged" not in record.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["validation", "retry"])
async def test_protocol_audit_store_failure_stops_before_any_second_request(setup, fail_at):
    agent, _, provider = setup
    evidence, initial, _, _ = conversation("analysis")
    provider._responses = [AIMessage(content=""), initial]

    class FailingStore(TraceStore):
        async def put(self, record):
            if (record.error_code == "invalid_json" and
                    (fail_at == "validation" or record.scheduled_retry is not None)):
                raise RuntimeError("PRIVATE_STORAGE_DETAIL")
            await super().put(record)

    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=FailingStore()):
        with pytest.raises(runtime.ModelBudgetStopped) as captured:
            await review(agent, evidence)
    assert captured.value.code == "trace_store_unavailable" and len(provider._requests) == 1


@pytest.mark.asyncio
async def test_cancel_during_protocol_retry_audit_prevents_another_request(setup):
    agent, _, provider = setup
    evidence, initial, _, _ = conversation("analysis")
    provider._responses = [AIMessage(content=""), initial]
    entered = asyncio.Event()

    class WaitingStore(TraceStore):
        async def put(self, record):
            if record.scheduled_retry is not None:
                entered.set()
                await asyncio.Event().wait()
            await super().put(record)

    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=WaitingStore()):
        task = asyncio.create_task(review(agent, evidence))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(provider._requests) == 1


@pytest.mark.asyncio
async def test_response_validation_context_does_not_leak_to_other_calls(setup):
    _, driver, provider = setup
    provider._responses = [AIMessage(content="{"), AIMessage(content="not JSON")]
    store = TraceStore()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=store):
        with pytest.raises(json.JSONDecodeError):
            await direct(driver)
        response = await driver.bind(response_format={"type": "json_object"}).ainvoke(
            [SystemMessage(content="Return JSON."), HumanMessage(content="Fixture")])
    assert response.content == "not JSON"
    first, second = list(store.records.values())
    assert first.error_code == "invalid_json" and second.status == "succeeded" and second.error_code is None
