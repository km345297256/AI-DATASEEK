import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from datetime import UTC, datetime, timedelta

import pytest
from langchain.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.core.config import Settings
from app.domain.services.analysis_budget import (
    AnalysisBudgetService, BudgetEvidence, BudgetPolicy, BudgetUnavailableError,
)
from app.domain.services import model_runtime as runtime
from app.infrastructure.repositories.mongo_analysis_budget_repository import MongoAnalysisBudgetRepository


class MemoryBudgetRepository:
    def __init__(self):
        self.docs = {}
        self.writes = []
        self.fail = False
        self.uncertain_write = False
        self.operations = {}

    async def get(self, lineage_id):
        if self.fail:
            raise OSError("private storage address")
        return deepcopy(self.docs.get(lineage_id))

    async def create_if_absent(self, document):
        if document["_id"] not in self.docs:
            self.docs[document["_id"]] = deepcopy(document)
        return deepcopy(self.docs[document["_id"]])

    async def compare_and_swap(self, lineage_id, version, document, *, require_before_deadline=False):
        await asyncio.sleep(0)  # Exercise competing versions, not serial mocks.
        if self.docs[lineage_id]["version"] != version:
            return False
        self.docs[lineage_id] = deepcopy(document)
        self.writes.append(deepcopy(document))
        if self.uncertain_write:
            raise OSError("write response lost")
        return True

    async def reserve_operation(self, document):
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("private storage address")
        created = document["_id"] not in self.operations
        if created:
            self.operations[document["_id"]] = deepcopy(document)
        if self.uncertain_write:
            raise OSError("write response lost")
        return deepcopy(self.operations[document["_id"]]), created

    async def aggregate_usage(self, lineage_id, user_id, session_id):
        if self.fail:
            raise OSError("private storage address")
        entries = [item for item in self.operations.values() if item["lineage_id"] == lineage_id
                   and item["owner_id"] == user_id and item["session_id"] == session_id and item["allowed"]]
        models = [item for item in entries if item["kind"] == "model"]
        return {"tool_batches_used": sum(item["kind"] == "tool" for item in entries),
                "model_calls": len(models), "charged_tokens": sum(
                    item["actual_tokens"] if item["actual_tokens"] is not None else item["reserved_tokens"]
                    for item in models)}

    async def settle_operation(self, operation_id, lineage_id, user_id, session_id, actual_tokens):
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("private storage address")
        item = self.operations.get(operation_id)
        if not item or (item["lineage_id"], item["owner_id"], item["session_id"]) != (lineage_id, user_id, session_id):
            return None
        if item["actual_tokens"] is None and actual_tokens is not None:
            item["actual_tokens"] = actual_tokens
        return deepcopy(item)


def evidence(units=1, **overrides):
    return BudgetEvidence.model_validate({
        "scope_digest": "a" * 64, "confirmed_progress_units": units,
        "progress_digest": f"{units:064x}", "next_action_bounded": True, **overrides,
    })


async def opened(*, policy=None, repository=None, **kwargs):
    repository = repository or MemoryBudgetRepository()
    policy = policy or BudgetPolicy(initial_batches=2, increment_batches=2, hard_batches=6,
                                    model_token_limit=10000, grant_min_model_tokens=10)
    service = AnalysisBudgetService(repository, policy=policy)
    handle = await service.open(user_id="owner", session_id="session", origin_input_id="11",
                                scope_digest="a" * 64, **kwargs)
    return service, handle, repository


@pytest.mark.asyncio
async def test_defaults_and_original_request_reopen_never_reset_spent_budget():
    policy = BudgetPolicy.from_settings(Settings(_env_file=None))
    assert policy.unlimited and policy.max_grants == 0
    assert (policy.initial_batches, policy.hard_batches, policy.model_token_limit,
            policy.model_call_limit, policy.deadline_seconds) == (None,) * 5
    service, handle, repository = await opened()
    await handle.reserve_tool_batch(evidence())
    same = await service.open(user_id="owner", session_id="session", origin_input_id="11", scope_digest="a" * 64)
    resumed = await service.open(user_id="owner", session_id="session", origin_input_id="99",
                                 scope_digest="a" * 64, lineage_id=handle.lineage_id)
    assert same.lineage_id == resumed.lineage_id == handle.lineage_id
    assert (await resumed.snapshot()).tool_batches_used == 1
    assert len(repository.docs) == 1


@pytest.mark.asyncio
async def test_two_host_reviewed_grants_are_atomic_and_hard_limited():
    service, handle, repository = await opened()
    admissions = []
    for units in (1, 1, 1, 1, 2, 2, 3):
        admissions.append(await handle.reserve_tool_batch(evidence(units)))
    assert [a.reason for a in admissions] == ["allowed", "allowed", "granted", "allowed", "granted", "allowed", "tool_budget_exhausted"]
    assert [a.tool_batches_used for a in admissions] == [1, 2, 3, 4, 5, 6, 6]
    assert [a.granted_batches for a in admissions] == [0, 0, 2, 0, 2, 0, 0]
    doc = repository.docs[handle.lineage_id]
    assert len(doc["grants"]) == doc["grant_count"] == 2
    assert [(g["from_limit"], g["to_limit"]) for g in doc["grants"]] == [(2, 4), (4, 6)]
    # Grant and the newly admitted batch are the same CAS document.
    assert repository.writes[2]["tool_batches_used"] == 3
    assert repository.writes[2]["grant_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,reason", [
    ({"has_unknown_execution": True}, "budget_execution_unconfirmed"),
    ({"no_progress_loop": True}, "budget_no_progress_loop"),
    ({"confirmed_progress_units": 0}, "budget_no_confirmed_progress"),
    ({"next_action_bounded": False}, "budget_next_action_unbounded"),
    ({"estimated_next_batches": 3}, "budget_next_action_unbounded"),
    ({"scope_digest": "b" * 64}, "budget_scope_changed"),
])
async def test_rule_rejection_cannot_spend_or_grant(overrides, reason):
    _, handle, repository = await opened()
    for _ in range(2):
        await handle.reserve_tool_batch(evidence())
    result = await handle.reserve_tool_batch(evidence(**overrides))
    assert not result.allowed and result.reason == reason
    assert result.tool_batches_used == result.soft_limit == 2
    assert result.grant_count == 0


@pytest.mark.asyncio
async def test_each_additional_grant_requires_new_units_and_digest():
    _, handle, _ = await opened()
    for _ in range(4):
        await handle.reserve_tool_batch(evidence())
    assert (await handle.reserve_tool_batch(evidence())).reason == "budget_no_confirmed_progress"
    assert (await handle.reserve_tool_batch(evidence(2, progress_digest=f"{1:064x}"))).reason == "budget_no_confirmed_progress"
    assert (await handle.reserve_tool_batch(evidence(2))).allowed


@pytest.mark.asyncio
async def test_independent_model_headroom_blocks_grant_not_only_tool_counter():
    policy = BudgetPolicy(initial_batches=1, hard_batches=3, model_call_limit=1,
                          model_token_limit=100, grant_min_model_tokens=10)
    _, handle, _ = await opened(policy=policy)
    await handle.reserve_tool_batch(evidence())
    reservation = await handle.reserve_model_request(20)
    await handle.settle_model_request(reservation.reservation_id, 10)
    assert (await handle.reserve_tool_batch(evidence())).reason == "budget_model_headroom_insufficient"


@pytest.mark.asyncio
async def test_limited_headroom_and_no_grants_configuration():
    for policy in (
        BudgetPolicy(initial_batches=1, hard_batches=3, max_grants=0),
        BudgetPolicy(initial_batches=1, hard_batches=1),
    ):
        _, handle, _ = await opened(policy=policy)
        await handle.reserve_tool_batch(evidence())
        assert (await handle.reserve_tool_batch(evidence())).reason == "tool_budget_exhausted"


@pytest.mark.asyncio
async def test_concurrent_model_reservations_and_settlements_do_not_overspend():
    policy = BudgetPolicy(model_call_limit=5, model_token_limit=500)
    _, handle, _ = await opened(policy=policy)
    results = await asyncio.gather(*(handle.reserve_model_request(100) for _ in range(12)))
    accepted = [result for result in results if result.allowed]
    assert len(accepted) == 5
    assert len({result.reservation_id for result in accepted}) == 5
    assert (await handle.snapshot()).charged_tokens == 500
    await asyncio.gather(*(handle.settle_model_request(item.reservation_id, 30) for item in accepted))
    snapshot = await handle.snapshot()
    assert snapshot.charged_tokens == 150 and snapshot.model_calls == 5
    assert (await handle.reserve_model_request(1)).reason == "task_call_budget_exceeded"


@pytest.mark.asyncio
async def test_concurrent_batches_cannot_duplicate_grants_or_exceed_hard_cap():
    _, handle, repository = await opened()
    results = await asyncio.gather(*(handle.reserve_tool_batch(evidence()) for _ in range(12)))
    assert sum(result.allowed for result in results) == 4  # Same proof cannot buy a second grant.
    assert repository.docs[handle.lineage_id]["grant_count"] == 1
    results = await asyncio.gather(*(handle.reserve_tool_batch(evidence(2)) for _ in range(6)))
    assert sum(result.allowed for result in results) == 2
    assert (await handle.snapshot()).tool_batches_used == 6


@pytest.mark.asyncio
async def test_duplicate_batch_id_neither_charges_nor_authorizes_tool_replay():
    _, handle, _ = await opened()
    key = uuid4().hex
    results = await asyncio.gather(*(handle.reserve_tool_batch(evidence(), reservation_id=key) for _ in range(5)))
    assert sum(result.allowed for result in results) == 1
    assert {result.reason for result in results} == {"allowed", "tool_batch_already_reserved"}
    assert (await handle.snapshot()).tool_batches_used == 1
    with pytest.raises(BudgetUnavailableError, match="reservation_changed"):
        await handle.reserve_tool_batch(evidence(2), reservation_id=key)


@pytest.mark.asyncio
async def test_model_reservation_is_not_permission_to_resend_and_settlement_is_idempotent():
    _, handle, _ = await opened()
    key = uuid4().hex
    assert (await handle.reserve_model_request(100, reservation_id=key)).allowed
    duplicate = await handle.reserve_model_request(100, reservation_id=key)
    assert not duplicate.allowed and duplicate.reason == "model_request_already_reserved"
    assert (await handle.settle_model_request(key, None)).charged_tokens == 100
    assert (await handle.settle_model_request(key, 50)).charged_tokens == 50
    assert (await handle.settle_model_request(key, 50)).charged_tokens == 50
    with pytest.raises(BudgetUnavailableError, match="settlement_changed"):
        await handle.settle_model_request(key, 49)


@pytest.mark.asyncio
async def test_provider_actual_overrun_is_recorded_and_future_admission_stops():
    _, handle, _ = await opened(policy=BudgetPolicy(model_token_limit=100))
    item = await handle.reserve_model_request(50)
    assert (await handle.settle_model_request(item.reservation_id, 101)).charged_tokens == 101
    assert (await handle.reserve_model_request(1)).reason == "task_token_budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"user_id": "other"}, {"session_id": "other"}, {"scope_digest": "b" * 64}])
async def test_lineage_owner_scope_and_session_are_immutable(changed):
    service, handle, _ = await opened()
    with pytest.raises(BudgetUnavailableError, match="scope_or_policy_changed"):
        await service.open(**{"user_id": "owner", "session_id": "session", "scope_digest": "a" * 64,
                              "origin_input_id": "99", "lineage_id": handle.lineage_id, **changed})


@pytest.mark.asyncio
async def test_policy_change_or_missing_ledger_never_creates_fresh_continuation_budget():
    service, handle, repository = await opened()
    changed = AnalysisBudgetService(repository, policy=BudgetPolicy())
    for creator, lineage in ((changed, handle.lineage_id), (service, "f" * 32)):
        with pytest.raises(BudgetUnavailableError):
            await creator.open(user_id="owner", session_id="session", origin_input_id="99",
                               scope_digest="a" * 64, lineage_id=lineage)
    assert len(repository.docs) == 1


@pytest.mark.asyncio
async def test_lease_loss_prevents_admission_but_does_not_erase_actual_usage():
    live = AsyncMock()
    _, handle, repository = await opened(require_live=live)
    item = await handle.reserve_model_request(100)
    live.side_effect = asyncio.CancelledError("lease lost")
    with pytest.raises(asyncio.CancelledError):
        await handle.reserve_tool_batch(evidence())
    with pytest.raises(asyncio.CancelledError):
        await handle.reserve_model_request(10)
    assert (await handle.settle_model_request(item.reservation_id, 30)).charged_tokens == 30
    assert repository.docs[handle.lineage_id]["tool_batches_used"] == 0


@pytest.mark.asyncio
async def test_uncertain_store_response_never_returns_execution_permission():
    _, handle, repository = await opened()
    repository.uncertain_write = True
    key = uuid4().hex
    with pytest.raises(BudgetUnavailableError, match="store_unavailable"):
        await handle.reserve_tool_batch(evidence(), reservation_id=key)
    repository.uncertain_write = False
    assert (await handle.snapshot()).tool_batches_used == 1
    assert not (await handle.reserve_tool_batch(evidence(), reservation_id=key)).allowed


@pytest.mark.asyncio
async def test_malformed_counter_or_store_outage_fails_closed_with_fixed_message():
    _, handle, repository = await opened()
    repository.docs[handle.lineage_id]["charged_tokens"] = 99
    with pytest.raises(BudgetUnavailableError, match="invalid"):
        await handle.snapshot()
    repository.fail = True
    with pytest.raises(BudgetUnavailableError) as error:
        await handle.snapshot()
    assert "private" not in str(error.value)


@pytest.mark.parametrize("values", [{"initial_batches": 20, "hard_batches": 10}, {"max_grants": 9}, {"initial_batches": True}])
def test_invalid_policy_rejected(values):
    with pytest.raises(ValidationError):
        BudgetPolicy(**values)


@pytest.mark.parametrize("values", [{"has_unknown_execution": "false"}, {"confirmed_progress_units": True}, {"extra": "approve"}])
def test_evidence_is_strict_not_model_coercion(values):
    with pytest.raises(ValidationError):
        evidence(**values)


@pytest.mark.asyncio
async def test_mongo_repository_uses_unique_id_and_owner_scope_version_cas():
    collection = SimpleNamespace(find_one=AsyncMock(), insert_one=AsyncMock(), replace_one=AsyncMock())
    repository = MongoAnalysisBudgetRepository(collection)
    document = {"_id": "id", "version": 1, "owner_id": "u", "session_id": "s", "scope_digest": "digest"}
    assert await repository.create_if_absent(document) == document
    collection.insert_one.side_effect = DuplicateKeyError("duplicate")
    collection.find_one.return_value = document
    assert await repository.create_if_absent(document) == document
    collection.replace_one.return_value = SimpleNamespace(matched_count=0)
    assert not await repository.compare_and_swap("id", 0, document)
    collection.replace_one.assert_awaited_once_with(
        {"_id": "id", "version": 0, "owner_id": "u", "session_id": "s", "scope_digest": "digest"}, document)


@pytest.fixture
def model_runtime_settings(monkeypatch):
    settings = Settings(_env_file=None, api_key="test-private-hmac-key")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", AsyncMock())


async def physical_model_call(invoke):
    return await runtime.invoke_model_request(messages=[HumanMessage(content="private data")], max_output_tokens=100,
                                             provider="fixture", model_name="fixture", invoke=invoke)


@pytest.mark.asyncio
async def test_model_runtime_keeps_lineage_counts_across_new_task_scopes(model_runtime_settings):
    _, handle, _ = await opened(policy=BudgetPolicy(model_call_limit=2))
    invoke = AsyncMock(return_value=AIMessage(content="done", usage_metadata={
        "input_tokens": 20, "output_tokens": 10, "total_tokens": 30}))
    for index in range(2):
        with runtime.model_execution_scope(user_id="owner", session_id="session", task_id=f"task{index}"):
            with runtime.analysis_budget_scope(handle):
                await physical_model_call(invoke)
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="continuation", durable_budget=handle):
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await physical_model_call(invoke)
        assert stopped.value.code == "task_call_budget_exceeded"
    assert invoke.await_count == 2
    assert (await handle.snapshot()).charged_tokens == 60


@pytest.mark.asyncio
async def test_budget_scope_does_not_reset_task_hard_cap_or_leak_to_next_input(model_runtime_settings):
    _, first, _ = await opened()
    _, second, _ = await opened()
    invoke = AsyncMock(return_value=AIMessage(content="done"))
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", call_limit=1) as scope:
        with runtime.analysis_budget_scope(first):
            assert await asyncio.create_task(asyncio.sleep(0, result=runtime.current_analysis_budget())) is first
            await physical_model_call(invoke)
        assert runtime.current_analysis_budget() is None
        with runtime.analysis_budget_scope(second):
            with pytest.raises(runtime.ModelBudgetStopped):
                await physical_model_call(invoke)
        assert scope.ledger.calls == 1
    assert (await second.snapshot()).model_calls == 0
    assert invoke.await_count == 1


@pytest.mark.asyncio
async def test_runtime_missing_usage_and_failed_request_preserve_reservation(model_runtime_settings):
    _, handle, _ = await opened()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", durable_budget=handle):
        await physical_model_call(AsyncMock(return_value=AIMessage(content="no usage")))
        with pytest.raises(TimeoutError):
            await physical_model_call(AsyncMock(side_effect=TimeoutError("provider response unknown")))
    snapshot = await handle.snapshot()
    assert snapshot.model_calls == 2 and snapshot.charged_tokens > 200


@pytest.mark.asyncio
async def test_runtime_store_outage_stops_before_provider(model_runtime_settings):
    _, handle, repository = await opened()
    repository.fail = True
    invoke = AsyncMock()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", durable_budget=handle):
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await physical_model_call(invoke)
        assert stopped.value.code == "trace_store_unavailable"
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_settlement_still_records_actual_provider_usage_without_replay(model_runtime_settings):
    _, handle, repository = await opened()
    trace_store = SimpleNamespace(records=[], put=None)
    async def put(record):
        trace_store.records.append(record.model_copy(deep=True))
    trace_store.put = put
    async def invoke(messages, limit):
        repository.fail = True
        return AIMessage(content="done", usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30})
    provider = AsyncMock(side_effect=invoke)
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", store=trace_store, durable_budget=handle):
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await physical_model_call(provider)
        assert stopped.value.code == "trace_store_unavailable"
    assert trace_store.records[-1].actual_total_tokens == 30
    assert trace_store.records[-1].status == "succeeded"
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_budget_audit_timeout_is_bounded_and_never_authorizes_execution():
    repository = MemoryBudgetRepository()
    service = AnalysisBudgetService(repository, store_timeout_seconds=0.001)
    handle = await service.open(user_id="owner", session_id="session", origin_input_id="11", scope_digest="a" * 64)
    repository.get = AsyncMock(side_effect=lambda *_: None)
    async def never_returns(*args):
        await asyncio.sleep(10)
    repository.get = never_returns
    with pytest.raises(BudgetUnavailableError, match="store_unavailable"):
        await handle.reserve_tool_batch(evidence())


@pytest.mark.asyncio
async def test_original_deadline_is_inherited_and_mongo_naive_milliseconds_are_accepted():
    service, handle, repository = await opened()
    doc = repository.docs[handle.lineage_id]
    for key in ("created_at", "deadline_at"):
        value = doc[key]
        doc[key] = value.replace(microsecond=value.microsecond // 1000 * 1000, tzinfo=None)
    inherited = await service.open(user_id="owner", session_id="session", origin_input_id="99",
                                   scope_digest="a" * 64, lineage_id=handle.lineage_id)
    deadline = (await inherited.snapshot()).deadline_at
    service._now = lambda: deadline
    assert (await inherited.reserve_model_request(1)).reason == "analysis_budget_deadline_exceeded"
    assert (await inherited.reserve_tool_batch(evidence())).reason == "analysis_budget_deadline_exceeded"
    assert repository.docs[handle.lineage_id]["model_calls"] == 0
    assert repository.docs[handle.lineage_id]["deadline_at"].replace(tzinfo=UTC) == deadline


@pytest.mark.asyncio
async def test_grant_needs_remaining_finalization_window_but_initial_budget_does_not_extend_deadline():
    service, handle, repository = await opened()
    await handle.reserve_tool_batch(evidence())
    await handle.reserve_tool_batch(evidence())
    deadline = (await handle.snapshot()).deadline_at
    service._now = lambda: deadline - timedelta(seconds=29)
    result = await handle.reserve_tool_batch(evidence())
    assert not result.allowed and result.reason == "budget_finalization_headroom_insufficient"
    assert repository.docs[handle.lineage_id]["grant_count"] == 0


@pytest.mark.asyncio
async def test_expiry_during_cas_does_not_return_permission_and_existing_settlement_survives_expiry():
    service, handle, repository = await opened()
    model = await handle.reserve_model_request(100)
    deadline = (await handle.snapshot()).deadline_at
    original = repository.compare_and_swap
    async def expire_after_write(*args, **kwargs):
        result = await original(*args, **kwargs)
        service._now = lambda: deadline
        return result
    repository.compare_and_swap = expire_after_write
    result = await handle.reserve_tool_batch(evidence())
    assert not result.allowed and result.reason == "analysis_budget_deadline_exceeded"
    assert (await handle.settle_model_request(model.reservation_id, 30)).charged_tokens == 30


@pytest.mark.asyncio
async def test_mongo_admission_uses_server_clock_gate_but_settlement_cas_can_run_after_expiry():
    collection = SimpleNamespace(replace_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
    repository = MongoAnalysisBudgetRepository(collection)
    doc = {"_id": "id", "owner_id": "u", "session_id": "s", "scope_digest": "digest"}
    assert await repository.compare_and_swap("id", 0, doc, require_before_deadline=True)
    assert collection.replace_one.call_args.args[0]["$expr"] == {"$gt": ["$deadline_at", "$$NOW"]}
    assert await repository.compare_and_swap("id", 1, doc)
    assert "$expr" not in collection.replace_one.call_args.args[0]


@pytest.mark.asyncio
async def test_model_request_deadline_stops_in_flight_without_provider_retry(model_runtime_settings):
    _, handle, repository = await opened(policy=BudgetPolicy(deadline_seconds=1))
    doc = repository.docs[handle.lineage_id]
    doc["created_at"] = datetime.now(UTC) - timedelta(seconds=0.8)
    doc["deadline_at"] = doc["created_at"] + timedelta(seconds=1)
    async def slow(*args):
        await asyncio.sleep(10)
    invoke = AsyncMock(side_effect=slow)
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", durable_budget=handle):
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await physical_model_call(invoke)
        assert stopped.value.code == "analysis_budget_deadline_exceeded"
    invoke.assert_awaited_once()
    assert repository.docs[handle.lineage_id]["model_calls"] == 1


def test_removed_environment_values_cannot_reenable_production_limits(monkeypatch, tmp_path):
    retired = {
        "MODEL_TASK_TOKEN_BUDGET": "4096", "MODEL_TASK_CALL_BUDGET": "1",
        "ANALYSIS_BUDGET_INITIAL_BATCHES": "1", "ANALYSIS_BUDGET_HARD_BATCHES": "1",
        "ANALYSIS_BUDGET_MAX_GRANTS": "0", "ANALYSIS_BUDGET_INCREMENT_BATCHES": "1",
        "ANALYSIS_BUDGET_DEADLINE_SECONDS": "60",
    }
    for key, value in retired.items():
        monkeypatch.setenv(key, value)
    env = tmp_path / "retired.env"
    env.write_text("\n".join(f"{key}={value}" for key, value in retired.items()))
    settings = Settings(_env_file=env)
    assert not hasattr(settings, "model_task_token_budget")
    assert not hasattr(settings, "analysis_budget_deadline_seconds")
    assert BudgetPolicy.from_settings(settings).unlimited
    assert BudgetPolicy.from_settings(SimpleNamespace(**{key.lower(): int(value) for key, value in retired.items()})).unlimited


async def unlimited_opened(**kwargs):
    return await opened(policy=BudgetPolicy.from_settings(Settings(_env_file=None)), **kwargs)


@pytest.mark.asyncio
async def test_unlimited_production_crosses_all_retired_caps_without_growing_lineage_document():
    service, handle, repository = await unlimited_opened()
    before = deepcopy(repository.docs[handle.lineage_id])
    service._now = lambda: before["created_at"] + timedelta(days=2)
    for _ in range(70):
        assert (await handle.reserve_tool_batch(evidence(0, next_action_bounded=False))).allowed
    for _ in range(140):
        item = await handle.reserve_model_request(10000)
        assert item.allowed
        await handle.settle_model_request(item.reservation_id, 9000)
    resumed = await service.open(user_id="owner", session_id="session", origin_input_id="99",
                                scope_digest="a" * 64, lineage_id=handle.lineage_id)
    snapshot = await resumed.snapshot()
    assert (snapshot.tool_batches_used, snapshot.model_calls, snapshot.charged_tokens) == (70, 140, 1260000)
    assert (snapshot.soft_limit, snapshot.hard_limit, snapshot.model_call_limit,
            snapshot.model_token_limit, snapshot.deadline_at) == (None,) * 5
    assert snapshot.grant_count == 0
    assert repository.docs[handle.lineage_id] == before
    assert len(repository.operations) == 210
    assert all(len(str(item)) < 1500 for item in repository.operations.values())


@pytest.mark.asyncio
async def test_unlimited_unique_reservations_and_single_settlement_are_concurrent_and_idempotent():
    _, handle, repository = await unlimited_opened()
    tool_id, model_id = uuid4().hex, uuid4().hex
    tools = await asyncio.gather(*(handle.reserve_tool_batch(evidence(), reservation_id=tool_id) for _ in range(20)))
    models = await asyncio.gather(*(handle.reserve_model_request(100, reservation_id=model_id) for _ in range(20)))
    assert sum(item.allowed for item in tools) == sum(item.allowed for item in models) == 1
    await asyncio.gather(*(handle.settle_model_request(model_id, 30) for _ in range(20)))
    snapshot = await handle.snapshot()
    assert (snapshot.tool_batches_used, snapshot.model_calls, snapshot.charged_tokens) == (1, 1, 30)
    with pytest.raises(BudgetUnavailableError, match="settlement_changed"):
        await handle.settle_model_request(model_id, 31)
    with pytest.raises(BudgetUnavailableError, match="reservation_changed"):
        await handle.reserve_model_request(101, reservation_id=model_id)
    with pytest.raises(BudgetUnavailableError, match="reservation_changed"):
        await handle.reserve_tool_batch(evidence(2), reservation_id=tool_id)
    assert len(repository.operations) == 2


@pytest.mark.asyncio
async def test_unlimited_unknown_insert_outcome_cannot_replay_and_failed_provider_remains_metered():
    _, handle, repository = await unlimited_opened()
    key = uuid4().hex
    repository.uncertain_write = True
    with pytest.raises(BudgetUnavailableError, match="store_unavailable"):
        await handle.reserve_model_request(100, reservation_id=key)
    repository.uncertain_write = False
    assert not (await handle.reserve_model_request(100, reservation_id=key)).allowed
    assert (await handle.settle_model_request(key, None)).charged_tokens == 100
    assert (await handle.settle_model_request(key, 10)).charged_tokens == 10


@pytest.mark.asyncio
async def test_unlimited_still_checks_scope_owner_lease_and_cancellation():
    live = AsyncMock()
    service, handle, repository = await unlimited_opened(require_live=live)
    denied = await handle.reserve_tool_batch(evidence(scope_digest="b" * 64))
    assert not denied.allowed and denied.reason == "budget_scope_changed"
    assert (await handle.snapshot()).tool_batches_used == 0
    with pytest.raises(BudgetUnavailableError, match="scope_or_policy_changed"):
        await service.open(user_id="other", session_id="session", origin_input_id="11",
                           scope_digest="a" * 64, lineage_id=handle.lineage_id)
    model = await handle.reserve_model_request(100)
    live.side_effect = asyncio.CancelledError("cancelled")
    with pytest.raises(asyncio.CancelledError):
        await handle.reserve_tool_batch(evidence())
    with pytest.raises(asyncio.CancelledError):
        await handle.reserve_model_request(100)
    assert (await handle.settle_model_request(model.reservation_id, 50)).charged_tokens == 50


@pytest.mark.asyncio
async def test_unlimited_metering_loss_never_authorizes_provider(model_runtime_settings):
    _, handle, repository = await unlimited_opened()
    repository.aggregate_usage = AsyncMock(side_effect=OSError("private database"))
    invoke = AsyncMock()
    with runtime.model_execution_scope(user_id="owner", session_id="session", task_id="task", durable_budget=handle):
        with pytest.raises(runtime.ModelBudgetStopped) as stopped:
            await physical_model_call(invoke)
        assert stopped.value.code == "trace_store_unavailable"
    invoke.assert_not_awaited()
