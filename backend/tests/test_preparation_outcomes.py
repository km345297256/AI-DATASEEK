"""No provider, database or sandbox: real admission and model driver, memory I/O."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import ErrorEvent, MessageEvent
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.input_delivery import InputDeliveryService
from app.domain.services import model_runtime as runtime
from app.infrastructure.external.sandbox.dataset_readability import DatasetReadabilityError
from test_input_delivery import MemoryInputs, FakeTask, expired, pending
from test_front_controller_transport_reliability import routing, decision, RoutingClient


class TraceStore:
    def __init__(self):
        self.records = {}

    async def put(self, record):
        self.records[record.trace_id] = record.model_copy(deep=True)


@pytest.mark.asyncio
async def test_preparation_failure_is_authoritative_and_notification_is_idempotent_after_crash():
    repository = MemoryInputs()
    record = await pending(repository)
    service = InputDeliveryService(repository, repository)
    for attempt in range(1, 4):
        record = await service.claim(await repository.get("session", record.key))
        assert record.admission.attempts == attempt
        await service.retry_preparation(record, failure_code="dataset_changed")
    record = await repository.get("session", record.key)
    assert record.admission.state == "interrupted"
    assert record.admission.execution_started is False
    assert len(repository.events) == 1

    persist = repository.add_event
    crash = True

    async def crash_between_outcome_and_terminal(session_id, event):
        nonlocal crash
        if isinstance(event, ErrorEvent) and crash:
            crash = False
            raise ConnectionError("synthetic terminal write outage")
        await persist(session_id, event)

    repository.add_event = crash_between_outcome_and_terminal
    with pytest.raises(ConnectionError):
        await service.maintain(lambda _: pytest.fail("must not rerun"))
    await service.maintain(lambda _: pytest.fail("must not rerun"))
    await service.maintain(lambda _: pytest.fail("must not rerun"))
    assert [event.type for event in repository.events] == ["message", "message", "error"]
    metadata = repository.events[1].metadata
    assert metadata["analysis_outcome"]["status"] == "failed"
    assert metadata["analysis_outcome"]["reason_code"] == "dataset_changed"
    assert metadata["analysis_started"] is False and metadata["preparation_attempts"] == 3
    assert metadata["execution_stage"] == "input_preparation"
    assert "分析尚未开始" in repository.events[1].message
    assert "源文件已改变" not in repository.events[1].message
    assert (await repository.get("session", record.key)).admission.notified


@pytest.mark.asyncio
async def test_unknown_failure_cannot_persist_exception_text_as_code():
    repository = MemoryInputs()
    record = await pending(repository)
    service = InputDeliveryService(repository, repository)
    claimed = await service.claim(record)
    await service.retry_preparation(claimed, failure_code="/Users/private/secret")
    record = await repository.get("session", record.key)
    assert record.admission.preparation_failure_code == "input_preparation_failed"
    assert "/Users" not in record.model_dump_json()


@pytest.mark.asyncio
async def test_user_cancel_after_one_preparation_failure_is_not_reported_as_exhausted_preparation():
    repository = MemoryInputs()
    record = await pending(repository)
    service = InputDeliveryService(repository, repository)
    record = await service.claim(record)
    await service.retry_preparation(record, failure_code="dataset_changed")
    await service.cancel_session("session")
    await service.maintain(lambda _: pytest.fail("must not dispatch"))
    outcome = repository.events[1].metadata["analysis_outcome"]
    assert outcome["reason_code"] == "request_cancelled"
    assert repository.events[1].metadata["analysis_started"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("analysis_admitted", [False, True])
async def test_reaper_does_not_equate_running_worker_with_started_analysis(analysis_admitted):
    repository = MemoryInputs()
    record = await pending(repository)
    service = InputDeliveryService(repository, repository)
    record = await service.claim(record)
    task = FakeTask()
    await service.bind(record, task)
    await service.start("session", record.event, task)
    assert (await repository.get("session", record.key)).admission.execution_started is None
    if analysis_admitted:
        await service.mark_analysis_started("session", record.key)
    record = await repository.get("session", record.key)
    await expired(repository, record)
    recovery = InputDeliveryService(repository, repository)
    await recovery.maintain(lambda _: pytest.fail("must not dispatch"))
    await recovery.maintain(lambda _: pytest.fail("must not dispatch"))
    assert repository.events[1].metadata["analysis_started"] is (True if analysis_admitted else None)


@pytest.mark.asyncio
async def test_reaper_preserves_final_verdict_committed_before_worker_exit():
    repository = MemoryInputs()
    record = await pending(repository)
    original = InputDeliveryService(repository, repository)
    record = await original.claim(record)
    task = FakeTask()
    await original.bind(record, task)
    await original.start("session", record.event, task)
    final = MessageEvent(message="confirmed result", metadata={"analysis_outcome": {
        "status": "partial", "reason_code": "artifacts_missing", "missing": [], "can_resume": False}})
    await original.prepare_event("session", record.key, final)
    await repository.add_event("session", final)
    record = await repository.get("session", record.key)
    await expired(repository, record)
    recovery = InputDeliveryService(repository, repository)
    await recovery.maintain(lambda _: pytest.fail("must not reexecute"))
    await recovery.maintain(lambda _: pytest.fail("must not reexecute"))
    outcomes = [item for item in repository.events if isinstance(item, MessageEvent)
                and (item.metadata or {}).get("analysis_outcome")]
    assert outcomes == [final]
    assert isinstance(repository.events[-1], ErrorEvent)
    assert (await repository.get("session", record.key)).admission.notified


@pytest.mark.asyncio
async def test_committed_outcome_query_is_bound_to_exact_input_and_session(monkeypatch):
    import hashlib
    from app.domain.models.input_admission import terminal_event_id
    from app.infrastructure.repositories.mongo_input_repository import MongoInputRepository
    from app.infrastructure.models.documents import SessionEventDocument
    repository = MemoryInputs()
    record = await pending(repository)
    final = MessageEvent(message="confirmed", metadata={"analysis_outcome": {"status": "succeeded"}})
    find = AsyncMock(return_value={"event": final.model_dump()})
    monkeypatch.setattr(SessionEventDocument, "get_pymongo_collection", lambda: SimpleNamespace(find_one=find))
    actual = await MongoInputRepository(repository).committed_outcome(record)
    assert actual == final
    expected_id = hashlib.sha256(terminal_event_id(record.key, "analysis_outcome").encode()).hexdigest()
    find.assert_awaited_once_with({"session_id": "session", "producer_event_key": expected_id}, {"event": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_usage", [False, True])
async def test_each_pre_sandbox_route_is_audited_even_when_all_three_preparations_fail(routing, monkeypatch, provider_usage, caplog):
    resolver, client, settings = routing
    settings.dataset_request_resolver_timeout_seconds = 1
    client._responses = [decision(), decision(), decision()]
    original = RoutingClient._agenerate

    async def generate(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        if provider_usage:
            result.generations[0].message.usage_metadata = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
        return result

    monkeypatch.setattr(RoutingClient, "_agenerate", generate)
    store = TraceStore()
    usage = AsyncMock()
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", usage)
    monkeypatch.setattr("app.domain.services.agent_domain_service.get_model_trace_repository", lambda: store)
    repository = MemoryInputs()
    record = await pending(repository)
    service = AgentDomainService(agent_repository=object(), session_repository=repository, sandbox_cls=object,
        task_cls=SimpleNamespace(get=lambda _: None), file_storage=object(), mcp_repository=object(),
        sandbox_runtime=object(), input_repository=repository)
    service._dataset_request_resolver = resolver
    service._create_task = AsyncMock(side_effect=DatasetReadabilityError("dataset_changed", {
        "stage": "directory_scan", "object_kind": "directory", "object_id": "a" * 16,
        "changed_fields": ["uid", "gid"], "source_path": "/Users/private/not-logged"}))
    for attempt in range(1, 4):
        record = await service._input_delivery.claim(await repository.get("session", record.key))
        with pytest.raises(DatasetReadabilityError):
            await service._dispatch_claimed_input(record)
    assert len(client._requests) == len(store.records) == 3
    assert {item.task_id for item in store.records.values()} == {
        f"preparation-{record.key}-{attempt}" for attempt in range(1, 4)}
    for item in store.records.values():
        assert item.user_id == "user" and item.session_id == "session"
        assert item.role == "front_controller" and item.status == "succeeded"
        assert item.actual_total_tokens == (30 if provider_usage else None)
        assert item.usage_source == ("provider" if provider_usage else "reservation")
        assert "/Users" not in item.public_view().model_dump_json()
    assert usage.await_count == (3 if provider_usage else 0)
    # A recorder fake sees the call, but the governed result's settlement flag
    # prevents the real resolver from double-counting these physical calls.
    assert "not-logged" not in caplog.text
    assert '"changed_fields": ["uid", "gid"]' in caplog.text
    assert (await repository.get("session", record.key)).admission.preparation_failure_code == "dataset_changed"


@pytest.mark.asyncio
async def test_audit_store_unavailable_stops_before_any_provider_request(routing, monkeypatch):
    resolver, client, _ = routing
    client._responses = [decision()]
    store = SimpleNamespace(put=AsyncMock(side_effect=ConnectionError("private store address")))
    monkeypatch.setattr("app.domain.services.agent_domain_service.get_model_trace_repository", lambda: store)
    repository = MemoryInputs()
    record = await pending(repository)
    service = AgentDomainService(agent_repository=object(), session_repository=repository, sandbox_cls=object,
        task_cls=SimpleNamespace(get=lambda _: None), file_storage=object(), mcp_repository=object(),
        sandbox_runtime=object(), input_repository=repository)
    service._dataset_request_resolver = resolver
    service._create_task = AsyncMock()
    record = await service._input_delivery.claim(record)
    with pytest.raises(runtime.ModelBudgetStopped):
        await service._dispatch_claimed_input(record)
    assert not client._requests
    service._create_task.assert_not_awaited()
    stored = await repository.get("session", record.key)
    assert stored.admission.preparation_failure_code == "model_audit_unavailable"
    assert "private store address" not in stored.model_dump_json()


@pytest.mark.asyncio
async def test_legacy_audit_failure_closes_session_and_releases_unqueued_claim(routing, monkeypatch):
    from test_chat_message_idempotency import FakeSessionRepository, FakeTask, make_service, bootstrap
    from app.domain.models.session import SessionStatus
    resolver, client, _ = routing
    client._responses = [decision()]
    store = SimpleNamespace(put=AsyncMock(side_effect=ConnectionError("private store address")))
    monkeypatch.setattr("app.domain.services.agent_domain_service.get_model_trace_repository", lambda: store)
    repository = FakeSessionRepository()
    repository.session.task_id = None
    FakeTask.current = None
    service = make_service(repository)
    service._dataset_request_resolver = resolver
    service._create_task = AsyncMock()
    with pytest.raises(runtime.ModelBudgetStopped):
        await bootstrap(service, repository, "legacy-client")
    assert not client._requests and not repository.claims
    service._create_task.assert_not_awaited()
    assert repository.session.status == SessionStatus.COMPLETED
    assert repository.events[0].metadata["analysis_outcome"]["reason_code"] == "model_audit_unavailable"
    assert isinstance(repository.events[-1], ErrorEvent)
