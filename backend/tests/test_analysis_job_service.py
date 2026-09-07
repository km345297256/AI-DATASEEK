from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

import pytest

from app.domain.models.analysis_job import (
    ANALYSIS_JOB_ACTIVE_STATUSES,
    AnalysisJobErrorCode,
    AnalysisJobRecord,
    AnalysisJobStatus,
)
from app.domain.services.analysis_job_service import AnalysisJobService


class InMemoryAnalysisJobRepository:
    def __init__(self) -> None:
        self.records: dict[str, AnalysisJobRecord] = {}

    async def insert(self, record: AnalysisJobRecord) -> AnalysisJobRecord:
        if record.job_id in self.records:
            raise RuntimeError("duplicate analysis job")
        self.records[record.job_id] = record.model_copy(deep=True)
        return self.records[record.job_id].model_copy(deep=True)

    async def find_by_id(self, job_id: str):
        record = self.records.get(job_id)
        return record.model_copy(deep=True) if record is not None else None

    async def find_for_owner(self, user_id: str, session_id: str, job_id: str):
        record = self.records.get(job_id)
        if (
            record is None
            or record.user_id != user_id
            or record.session_id != session_id
        ):
            return None
        return record.model_copy(deep=True)

    async def list_for_owner(self, user_id: str, session_id: str, *, limit: int):
        records = [
            record
            for record in self.records.values()
            if record.user_id == user_id and record.session_id == session_id
        ]
        records.sort(key=lambda item: (item.created_at, item.job_id), reverse=True)
        return [record.model_copy(deep=True) for record in records[:limit]]

    async def list_active_by_runtime(self, runtime_id: str, *, limit: int):
        records = [
            record
            for record in self.records.values()
            if record.runtime_id == runtime_id
            and record.status in ANALYSIS_JOB_ACTIVE_STATUSES
        ]
        records.sort(key=lambda item: (item.lease_expires_at, item.job_id))
        return [record.model_copy(deep=True) for record in records[:limit]]

    async def list_expired_active(self, before: datetime, *, limit: int):
        records = [
            record
            for record in self.records.values()
            if record.status in ANALYSIS_JOB_ACTIVE_STATUSES
            and record.lease_expires_at <= before
        ]
        records.sort(key=lambda item: (item.lease_expires_at, item.job_id))
        return [record.model_copy(deep=True) for record in records[:limit]]

    async def compare_and_set(
        self,
        job_id: str,
        *,
        expected_revision: int,
        expected_statuses: frozenset[AnalysisJobStatus],
        updates: Mapping[str, Any],
        expected_runtime_id: str | None = None,
        lease_expires_at_lte: datetime | None = None,
    ):
        record = self.records.get(job_id)
        if (
            record is None
            or record.revision != expected_revision
            or record.status not in expected_statuses
            or (
                expected_runtime_id is not None
                and record.runtime_id != expected_runtime_id
            )
            or (
                lease_expires_at_lte is not None
                and record.lease_expires_at > lease_expires_at_lte
            )
        ):
            return None
        updated = record.model_copy(update={
            **dict(updates),
            "revision": record.revision + 1,
        })
        self.records[job_id] = updated
        return updated.model_copy(deep=True)

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        self.records = {
            job_id: record
            for job_id, record in self.records.items()
            if (record.user_id, record.session_id) != (user_id, session_id)
        }


async def _create(service: AnalysisJobService, **overrides):
    values = {
        "user_id": "user-1",
        "session_id": "session-1",
        "task_id": "task-1",
        "tool_name": "dataset_quicklook",
        "tool_call_id": "private-call-id",
        "execution_snapshot_id": "snapshot-1",
        "catalog_revision": "catalog-1",
        "cancellable": True,
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return await service.create(**values)


@pytest.mark.asyncio
async def test_job_lifecycle_is_revisioned_public_and_owner_scoped():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    updates = []

    async def on_update(view):
        updates.append(view)

    created = await _create(service, on_update=on_update)
    assert created.status == AnalysisJobStatus.QUEUED
    assert created.revision == 1
    assert created.tool_call_ref.startswith("call:sha256:")
    assert "private-call-id" not in created.model_dump_json()
    public = created.public_view().model_dump()
    assert "user_id" not in public
    assert "session_id" not in public
    assert "task_id" not in public
    assert "runtime_id" not in public
    assert "tool_call_ref" not in public

    running = await service.mark_running(created.job_id)
    finished = await service.finish(created.job_id, AnalysisJobStatus.SUCCEEDED)
    assert running.revision == 2
    assert running.started_at is not None
    assert finished.revision == 3
    assert finished.finished_at is not None
    assert finished.error_code is None
    assert [item.status for item in updates] == [
        AnalysisJobStatus.QUEUED,
        AnalysisJobStatus.RUNNING,
        AnalysisJobStatus.SUCCEEDED,
    ]
    assert await service.get_for_owner(
        "other-user", "session-1", created.job_id
    ) is None
    assert [item.job_id for item in await service.list_for_owner(
        "user-1", "session-1"
    )] == [created.job_id]


@pytest.mark.asyncio
async def test_cancel_before_bind_is_persisted_and_cancels_worker_on_bind():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    created = await _create(service)

    cancelling = await service.request_cancel(
        "user-1", "session-1", created.job_id
    )
    assert cancelling is not None
    assert cancelling.status == AnalysisJobStatus.CANCELLING
    assert cancelling.cancel_requested_at is not None

    worker = asyncio.create_task(asyncio.Event().wait())
    await service.bind(created.job_id, worker)
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert (await service.mark_running(created.job_id)).status == (
        AnalysisJobStatus.CANCELLING
    )
    terminal = await service.finish(created.job_id, AnalysisJobStatus.CANCELLED)
    assert terminal.status == AnalysisJobStatus.CANCELLED
    assert terminal.error_code == AnalysisJobErrorCode.CANCELLED


@pytest.mark.asyncio
async def test_cancel_does_not_override_a_worker_result_waiting_for_finish():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    created = await _create(service)
    worker = asyncio.create_task(asyncio.sleep(0))
    await service.bind(created.job_id, worker)
    await worker

    unchanged = await service.request_cancel(
        "user-1", "session-1", created.job_id
    )
    assert unchanged is not None
    assert unchanged.status == AnalysisJobStatus.QUEUED
    terminal = await service.finish(created.job_id, AnalysisJobStatus.SUCCEEDED)
    assert terminal.status == AnalysisJobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_successful_result_wins_when_cancellation_races_with_finish():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    created = await _create(service)

    async def finish_despite_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "already completed"

    worker = asyncio.create_task(finish_despite_cancel())
    await service.bind(created.job_id, worker)
    await service.mark_running(created.job_id)
    await asyncio.sleep(0)
    cancelling = await service.request_cancel(
        "user-1", "session-1", created.job_id
    )
    assert cancelling is not None
    assert cancelling.status == AnalysisJobStatus.CANCELLING
    assert await worker == "already completed"

    terminal = await service.finish(created.job_id, AnalysisJobStatus.SUCCEEDED)
    assert terminal.status == AnalysisJobStatus.SUCCEEDED
    assert terminal.error_code is None


@pytest.mark.asyncio
async def test_cancel_rejects_non_cancellable_job_and_hides_other_owners():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    created = await _create(service, cancellable=False)

    assert await service.request_cancel(
        "other-user", "session-1", created.job_id
    ) is None
    with pytest.raises(ValueError, match="not cancellable"):
        await service.request_cancel("user-1", "session-1", created.job_id)
    assert repository.records[created.job_id].status == AnalysisJobStatus.QUEUED


@pytest.mark.asyncio
async def test_maintenance_renews_only_live_bound_workers_and_interrupts_stale_jobs():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository, lease_seconds=30)
    live = await _create(service, tool_call_id="live")
    live_worker = asyncio.create_task(asyncio.Event().wait())
    await service.bind(live.job_id, live_worker)
    await service.mark_running(live.job_id)

    stale = await _create(service, tool_call_id="never-bound")
    valid_other_service = AnalysisJobService(repository, lease_seconds=30)
    valid_other = await _create(
        valid_other_service,
        tool_call_id="other-runtime",
    )
    now = datetime.now(UTC)
    live_record = repository.records[live.job_id]
    repository.records[live.job_id] = live_record.model_copy(update={
        "lease_expires_at": now + timedelta(seconds=1),
    })
    stale_record = repository.records[stale.job_id]
    repository.records[stale.job_id] = stale_record.model_copy(update={
        "lease_expires_at": now - timedelta(seconds=1),
    })

    await service.maintain()

    assert repository.records[live.job_id].status == AnalysisJobStatus.RUNNING
    assert repository.records[live.job_id].lease_expires_at > now
    assert repository.records[stale.job_id].status == AnalysisJobStatus.INTERRUPTED
    assert repository.records[stale.job_id].error_code == (
        AnalysisJobErrorCode.WORKER_INTERRUPTED
    )
    assert repository.records[valid_other.job_id].status == AnalysisJobStatus.QUEUED

    live_worker.cancel()
    await asyncio.gather(live_worker, return_exceptions=True)
    await service.finish(live.job_id, AnalysisJobStatus.CANCELLED)
    await valid_other_service.shutdown()


@pytest.mark.asyncio
async def test_expired_local_worker_cannot_resurrect_its_lease():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    created = await _create(service)
    worker = asyncio.create_task(asyncio.Event().wait())
    await service.bind(created.job_id, worker)
    await service.mark_running(created.job_id)
    current = repository.records[created.job_id]
    repository.records[created.job_id] = current.model_copy(update={
        "lease_expires_at": datetime.now(UTC) - timedelta(seconds=1),
    })

    await service.maintain()

    with pytest.raises(asyncio.CancelledError):
        await worker
    terminal = repository.records[created.job_id]
    assert terminal.status == AnalysisJobStatus.INTERRUPTED
    assert terminal.error_code == AnalysisJobErrorCode.WORKER_INTERRUPTED


@pytest.mark.asyncio
async def test_cross_runtime_cancel_is_observed_by_lease_maintenance():
    repository = InMemoryAnalysisJobRepository()
    worker_service = AnalysisJobService(repository)
    api_service = AnalysisJobService(repository)
    created = await _create(worker_service)
    worker = asyncio.create_task(asyncio.Event().wait())
    await worker_service.bind(created.job_id, worker)
    await worker_service.mark_running(created.job_id)

    cancelling = await api_service.request_cancel(
        "user-1", "session-1", created.job_id
    )
    assert cancelling is not None
    assert cancelling.status == AnalysisJobStatus.CANCELLING
    assert not worker.done()
    await worker_service.maintain()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await worker_service.finish(created.job_id, AnalysisJobStatus.CANCELLED)


@pytest.mark.asyncio
async def test_maintenance_stops_local_handle_terminalized_by_another_monitor():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    created = await _create(service)
    worker = asyncio.create_task(asyncio.Event().wait())
    await service.bind(created.job_id, worker)
    await service.mark_running(created.job_id)
    current = repository.records[created.job_id]
    repository.records[created.job_id] = current.model_copy(update={
        "revision": current.revision + 1,
        "status": AnalysisJobStatus.INTERRUPTED,
        "finished_at": datetime.now(UTC),
        "error_code": AnalysisJobErrorCode.WORKER_INTERRUPTED,
    })

    await service.maintain()

    with pytest.raises(asyncio.CancelledError):
        await worker
    assert created.job_id not in service._handles
    assert created.job_id not in service._callbacks


def test_naive_mongo_datetimes_are_normalized_to_utc():
    naive = datetime.utcnow()
    record = AnalysisJobRecord(
        job_id="a" * 32,
        tool_name="shell_run",
        created_at=naive,
        started_at=naive,
        finished_at=naive,
        cancel_requested_at=naive,
        user_id="user-1",
        session_id="session-1",
        task_id="task-1",
        runtime_id="b" * 32,
        tool_call_ref="call:sha256:123456789abc",
        lease_expires_at=naive,
    )

    assert record.created_at.tzinfo == UTC
    assert record.started_at is not None and record.started_at.tzinfo == UTC
    assert record.finished_at is not None and record.finished_at.tzinfo == UTC
    assert record.cancel_requested_at is not None
    assert record.cancel_requested_at.tzinfo == UTC
    assert record.lease_expires_at.tzinfo == UTC
    assert record.public_view().model_dump_json().count("Z") == 4


@pytest.mark.asyncio
async def test_error_codes_are_fixed_and_callback_failure_cannot_fail_transition(caplog):
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)

    async def failing_callback(_view):
        raise RuntimeError("private callback detail")

    created = await _create(service, on_update=failing_callback)
    await service.mark_running(created.job_id)
    with pytest.raises(ValueError):
        await service.finish(
            created.job_id,
            AnalysisJobStatus.FAILED,
            error_code="private callback detail",
        )
    terminal = await service.finish(created.job_id, AnalysisJobStatus.FAILED)
    assert terminal.error_code == AnalysisJobErrorCode.TOOL_FAILED
    assert "private callback detail" not in caplog.text


@pytest.mark.asyncio
async def test_delete_owner_requires_workers_to_be_stopped():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    created = await _create(service)
    worker = asyncio.create_task(asyncio.Event().wait())
    await service.bind(created.job_id, worker)

    with pytest.raises(RuntimeError, match="workers must stop"):
        await service.delete_owner("user-1", "session-1")
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    await service.finish(created.job_id, AnalysisJobStatus.CANCELLED)
    await service.delete_owner("user-1", "session-1")
    assert repository.records == {}
