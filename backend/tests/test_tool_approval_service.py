from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

import pytest

from app.domain.models.tool_approval import (
    TOOL_APPROVAL_UNCONSUMED_STATUSES,
    ToolApprovalRecord,
    ToolApprovalStatus,
)
from app.domain.services.tool_approval_service import (
    ToolApprovalConflictError,
    ToolApprovalService,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class InMemoryToolApprovalRepository:
    def __init__(self) -> None:
        self.records: dict[str, ToolApprovalRecord] = {}
        self.lock = asyncio.Lock()

    async def insert(self, record: ToolApprovalRecord) -> ToolApprovalRecord:
        async with self.lock:
            if record.approval_id in self.records:
                raise RuntimeError("duplicate")
            self.records[record.approval_id] = record.model_copy(deep=True)
            return record.model_copy(deep=True)

    async def find_by_id(self, approval_id: str):
        record = self.records.get(approval_id)
        return record.model_copy(deep=True) if record is not None else None

    async def find_for_owner(
        self,
        user_id: str,
        session_id: str,
        approval_id: str,
    ):
        record = self.records.get(approval_id)
        if (
            record is None
            or record.user_id != user_id
            or record.session_id != session_id
        ):
            return None
        return record.model_copy(deep=True)

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int,
    ):
        records = [
            record
            for record in self.records.values()
            if record.user_id == user_id and record.session_id == session_id
        ]
        records.sort(key=lambda item: (item.created_at, item.approval_id), reverse=True)
        return [record.model_copy(deep=True) for record in records[:limit]]

    async def list_unconsumed_by_runtime(self, runtime_id: str, *, limit: int):
        records = [
            record
            for record in self.records.values()
            if record.runtime_id == runtime_id
            and record.status in TOOL_APPROVAL_UNCONSUMED_STATUSES
        ]
        records.sort(key=lambda item: (item.expires_at, item.approval_id))
        return [record.model_copy(deep=True) for record in records[:limit]]

    async def list_expired_unconsumed(self, before: datetime, *, limit: int):
        records = [
            record
            for record in self.records.values()
            if record.status in TOOL_APPROVAL_UNCONSUMED_STATUSES
            and record.expires_at <= before
        ]
        records.sort(key=lambda item: (item.expires_at, item.approval_id))
        return [record.model_copy(deep=True) for record in records[:limit]]

    async def compare_and_set(
        self,
        approval_id: str,
        *,
        expected_revision: int,
        expected_statuses: frozenset[ToolApprovalStatus],
        updates: Mapping[str, Any],
        expected_user_id: str | None = None,
        expected_session_id: str | None = None,
        expected_runtime_id: str | None = None,
        expires_at_lte: datetime | None = None,
        expires_at_gt: datetime | None = None,
    ):
        async with self.lock:
            record = self.records.get(approval_id)
            if (
                record is None
                or record.revision != expected_revision
                or record.status not in expected_statuses
                or (
                    expected_user_id is not None
                    and record.user_id != expected_user_id
                )
                or (
                    expected_session_id is not None
                    and record.session_id != expected_session_id
                )
                or (
                    expected_runtime_id is not None
                    and record.runtime_id != expected_runtime_id
                )
                or (
                    expires_at_lte is not None
                    and record.expires_at > expires_at_lte
                )
                or (
                    expires_at_gt is not None
                    and record.expires_at <= expires_at_gt
                )
            ):
                return None
            updated = record.model_copy(update={
                **dict(updates),
                "revision": record.revision + 1,
            })
            self.records[approval_id] = updated
            return updated.model_copy(deep=True)

    async def consume_approved(
        self,
        approval_id: str,
        *,
        expected_user_id: str,
        expected_session_id: str,
        expected_runtime_id: str,
        expected_call_digest: str,
        unexpired_at: datetime,
    ):
        async with self.lock:
            record = self.records.get(approval_id)
            if (
                record is None
                or record.user_id != expected_user_id
                or record.session_id != expected_session_id
                or record.runtime_id != expected_runtime_id
                or record.call_digest != expected_call_digest
                or record.status != ToolApprovalStatus.APPROVED
                or record.expires_at <= unexpired_at
            ):
                return None
            updated = record.model_copy(update={
                "status": ToolApprovalStatus.CONSUMED,
                "revision": record.revision + 1,
            })
            self.records[approval_id] = updated
            return updated.model_copy(deep=True)

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        async with self.lock:
            self.records = {
                approval_id: record
                for approval_id, record in self.records.items()
                if (record.user_id, record.session_id) != (user_id, session_id)
            }


async def _create(service: ToolApprovalService, **overrides: Any):
    values: dict[str, Any] = {
        "user_id": "user-1",
        "session_id": "session-1",
        "task_id": "task-1",
        "tool_name": "publish_result",
        "call_digest": "a" * 64,
        "effects": ["network", "external_side_effect"],
        "permissions": ["provider.write"],
        "arguments_preview": {"destination": "[redacted]", "count": 2},
        "credential_refs": ["cred_" + "b" * 32],
        "execution_snapshot_id": "snapshot-1",
        "catalog_revision": "catalog-1",
    }
    values.update(overrides)
    return await service.create(**values)


@pytest.mark.asyncio
async def test_decision_is_revisioned_private_and_owner_scoped():
    repository = InMemoryToolApprovalRepository()
    service = ToolApprovalService(repository, clock=MutableClock())
    created = await _create(service)

    assert created.status == ToolApprovalStatus.PENDING
    assert created.revision == 1
    assert created.expires_at - created.created_at == timedelta(seconds=300)
    public = created.public_view().model_dump()
    assert "user_id" not in public
    assert "session_id" not in public
    assert "task_id" not in public
    assert "runtime_id" not in public
    assert "call_digest" not in public
    assert await service.get_for_owner(
        "other-user", "session-1", created.approval_id
    ) is None

    approved = await service.decide(
        "user-1",
        "session-1",
        created.approval_id,
        "approved",
        created.revision,
    )
    assert approved.status == ToolApprovalStatus.APPROVED
    assert approved.revision == 2
    assert approved.decided_at == created.created_at


@pytest.mark.asyncio
async def test_decision_cas_allows_only_one_racing_owner_decision():
    repository = InMemoryToolApprovalRepository()
    service = ToolApprovalService(repository)
    created = await _create(service)

    results = await asyncio.gather(
        service.decide(
            "user-1", "session-1", created.approval_id, "approved", 1
        ),
        service.decide(
            "user-1", "session-1", created.approval_id, "rejected", 1
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ToolApprovalRecord) for item in results) == 1
    assert sum(isinstance(item, ToolApprovalConflictError) for item in results) == 1
    stored = repository.records[created.approval_id]
    assert stored.revision == 2
    assert stored.status in {ToolApprovalStatus.APPROVED, ToolApprovalStatus.REJECTED}


@pytest.mark.asyncio
async def test_cross_user_and_cross_session_decisions_are_hidden():
    repository = InMemoryToolApprovalRepository()
    service = ToolApprovalService(repository)
    created = await _create(service)

    assert await service.get_for_owner(
        "user-1", "different-session", created.approval_id
    ) is None
    with pytest.raises(LookupError, match="Tool approval not found"):
        await service.decide(
            "other-user", "session-1", created.approval_id, "approved", 1
        )


@pytest.mark.asyncio
async def test_get_and_maintain_expire_unconsumed_approvals():
    clock = MutableClock()
    repository = InMemoryToolApprovalRepository()
    service = ToolApprovalService(repository, clock=clock)
    first = await _create(service)
    second = await _create(service, call_digest="c" * 64)
    clock.advance(301)

    expired = await service.get_for_owner(
        "user-1", "session-1", first.approval_id
    )
    assert expired is not None
    assert expired.status == ToolApprovalStatus.EXPIRED
    assert expired.revision == 2
    assert await service.maintain() == 1
    assert repository.records[second.approval_id].status == ToolApprovalStatus.EXPIRED
    with pytest.raises(ToolApprovalConflictError):
        await service.decide(
            "user-1", "session-1", first.approval_id, "approved", 1
        )


@pytest.mark.asyncio
async def test_approved_call_can_be_consumed_exactly_once():
    repository = InMemoryToolApprovalRepository()
    service = ToolApprovalService(repository)
    created = await _create(service)
    await service.decide(
        "user-1", "session-1", created.approval_id, "approved", 1
    )

    results = await asyncio.gather(
        service.consume(created.approval_id, "a" * 64),
        service.consume(created.approval_id, "a" * 64),
        return_exceptions=True,
    )
    assert sum(
        isinstance(item, ToolApprovalRecord)
        and item.status == ToolApprovalStatus.CONSUMED
        for item in results
    ) == 1
    assert sum(isinstance(item, ToolApprovalConflictError) for item in results) == 1
    assert repository.records[created.approval_id].revision == 3


@pytest.mark.asyncio
async def test_digest_mismatch_and_restarted_runtime_cannot_consume():
    repository = InMemoryToolApprovalRepository()
    original = ToolApprovalService(repository)
    created = await _create(original)
    await original.decide(
        "user-1", "session-1", created.approval_id, "approved", 1
    )

    with pytest.raises(ToolApprovalConflictError):
        await original.consume(created.approval_id, "d" * 64)
    restarted = ToolApprovalService(repository)
    assert restarted.runtime_id != original.runtime_id
    with pytest.raises(ToolApprovalConflictError):
        await restarted.consume(created.approval_id, "a" * 64)
    consumed = await original.consume(created.approval_id, "a" * 64)
    assert consumed.status == ToolApprovalStatus.CONSUMED


@pytest.mark.asyncio
async def test_cancel_and_shutdown_are_limited_to_creating_runtime():
    repository = InMemoryToolApprovalRepository()
    first_runtime = ToolApprovalService(repository)
    second_runtime = ToolApprovalService(repository)
    first = await _create(first_runtime)
    second = await _create(
        second_runtime,
        user_id="user-2",
        session_id="session-2",
        call_digest="e" * 64,
    )

    with pytest.raises(ToolApprovalConflictError):
        await second_runtime.cancel(first.approval_id)
    cancelled = await first_runtime.cancel(first.approval_id)
    assert cancelled.status == ToolApprovalStatus.CANCELLED
    assert await second_runtime.shutdown() == 1
    assert repository.records[second.approval_id].status == ToolApprovalStatus.CANCELLED


@pytest.mark.asyncio
async def test_delete_owner_never_deletes_another_owner_or_session():
    repository = InMemoryToolApprovalRepository()
    service = ToolApprovalService(repository)
    target = await _create(service)
    retained = await _create(
        service,
        session_id="session-2",
        call_digest="f" * 64,
    )

    await service.delete_owner("user-1", "session-1")
    assert target.approval_id not in repository.records
    assert retained.approval_id in repository.records
