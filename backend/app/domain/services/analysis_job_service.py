from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable, TypeVar

from app.domain.models.analysis_job import (
    ANALYSIS_JOB_ACTIVE_STATUSES,
    ANALYSIS_JOB_TERMINAL_STATUSES,
    AnalysisJobErrorCode,
    AnalysisJobRecord,
    AnalysisJobStatus,
    AnalysisJobView,
)
from app.domain.models.spill import SpillArtifactRef
from app.domain.repositories.analysis_job_repository import AnalysisJobRepository


logger = logging.getLogger(__name__)

AnalysisJobUpdateCallback = Callable[[AnalysisJobView], Awaitable[None]]

_UPDATE_TIMEOUT_SECONDS = 1.0
_STATE_DEADLINE_SECONDS = 5.0
_MAINTENANCE_BATCH_SIZE = 500
_CAS_RETRIES = 8
_DEFAULT_ERROR_CODES = {
    AnalysisJobStatus.FAILED: AnalysisJobErrorCode.TOOL_FAILED,
    AnalysisJobStatus.CANCELLED: AnalysisJobErrorCode.CANCELLED,
    AnalysisJobStatus.TIMED_OUT: AnalysisJobErrorCode.TOOL_TIMEOUT,
    AnalysisJobStatus.INTERRUPTED: AnalysisJobErrorCode.WORKER_INTERRUPTED,
}

_T = TypeVar("_T")


def _job_log_ref(job_id: str) -> str:
    digest = hashlib.sha256(
        str(job_id).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"job:sha256:{digest}"


def _tool_call_ref(tool_call_id: str) -> str:
    digest = hashlib.sha256(
        str(tool_call_id).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"call:sha256:{digest}"


class AnalysisJobService:
    """Durable AnalysisJob state machine; tool execution stays in AgentLoop."""

    def __init__(
        self,
        repository: AnalysisJobRepository,
        *,
        lease_seconds: float = 30,
    ) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or float(lease_seconds) <= 0
        ):
            raise ValueError("analysis job lease must be a positive finite number")
        self._repository = repository
        self._lease = timedelta(seconds=float(lease_seconds))
        self._runtime_id = uuid.uuid4().hex
        self._handles: dict[str, asyncio.Task] = {}
        self._callbacks: dict[str, AnalysisJobUpdateCallback] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._state_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._closed = False

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @staticmethod
    def _state_deadline() -> float:
        return asyncio.get_running_loop().time() + _STATE_DEADLINE_SECONDS

    @staticmethod
    async def _repository_call(
        operation: Callable[[], Awaitable[_T]],
        *,
        deadline: float,
    ) -> _T:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("analysis job state deadline exceeded")
        async with asyncio.timeout(remaining):
            return await operation()

    async def create(
        self,
        *,
        user_id: str,
        session_id: str,
        task_id: str,
        tool_name: str,
        tool_call_id: str,
        execution_snapshot_id: str | None = None,
        catalog_revision: str | None = None,
        cancellable: bool = True,
        timeout_seconds: float | None = None,
        on_update: AnalysisJobUpdateCallback | None = None,
    ) -> AnalysisJobRecord:
        if self._closed:
            raise RuntimeError("analysis job service is shut down")
        deadline = self._state_deadline()
        now = datetime.now(UTC)
        record = AnalysisJobRecord(
            user_id=user_id,
            session_id=session_id,
            task_id=task_id,
            runtime_id=self._runtime_id,
            tool_name=tool_name,
            tool_call_ref=_tool_call_ref(tool_call_id),
            execution_snapshot_id=execution_snapshot_id,
            catalog_revision=catalog_revision,
            cancellable=cancellable,
            timeout_seconds=timeout_seconds,
            lease_expires_at=now + self._lease,
            created_at=now,
        )
        persisted = await self._repository_call(
            lambda: self._repository.insert(record),
            deadline=deadline,
        )
        async with self._state_lock:
            if on_update is not None:
                self._callbacks[persisted.job_id] = on_update
            self._owners[persisted.job_id] = (user_id, session_id)
        await self._publish(persisted)
        return persisted

    async def bind(self, job_id: str, task: asyncio.Task) -> None:
        if not isinstance(task, asyncio.Task):
            raise TypeError("analysis job handle must be an asyncio.Task")
        if self._closed:
            task.cancel()
            raise RuntimeError("analysis job service is shut down")
        async with self._state_lock:
            previous = self._handles.get(job_id)
            if previous is not None and previous is not task and not previous.done():
                task.cancel()
                raise RuntimeError("analysis job already has a running handle")
            self._handles[job_id] = task

        deadline = self._state_deadline()
        record = await self._repository_call(
            lambda: self._repository.find_by_id(job_id),
            deadline=deadline,
        )
        if record is None or record.runtime_id != self._runtime_id:
            task.cancel()
            await self._drop_handle(job_id, expected=task)
            raise KeyError("analysis job was not found in this runtime")
        if (
            record.status == AnalysisJobStatus.CANCELLING
            or record.status in ANALYSIS_JOB_TERMINAL_STATUSES
        ):
            task.cancel()

    async def mark_running(self, job_id: str) -> AnalysisJobRecord:
        deadline = self._state_deadline()
        for _ in range(_CAS_RETRIES):
            record = await self._require_job(job_id, deadline=deadline)
            if record.runtime_id != self._runtime_id:
                raise RuntimeError("analysis job belongs to another runtime")
            if record.status != AnalysisJobStatus.QUEUED:
                return record
            now = datetime.now(UTC)
            updated = await self._repository_call(
                lambda: self._repository.compare_and_set(
                    job_id,
                    expected_revision=record.revision,
                    expected_statuses=frozenset({AnalysisJobStatus.QUEUED}),
                    expected_runtime_id=self._runtime_id,
                    updates={
                        "status": AnalysisJobStatus.RUNNING,
                        "started_at": record.started_at or now,
                        "lease_expires_at": now + self._lease,
                    },
                ),
                deadline=deadline,
            )
            if updated is not None:
                await self._publish(updated)
                return updated
        raise RuntimeError("analysis job could not enter running state")

    async def finish(
        self,
        job_id: str,
        status: AnalysisJobStatus,
        *,
        error_code: AnalysisJobErrorCode | str | None = None,
        result_spill: SpillArtifactRef | None = None,
    ) -> AnalysisJobRecord:
        deadline = self._state_deadline()
        status = AnalysisJobStatus(status)
        if status not in ANALYSIS_JOB_TERMINAL_STATUSES:
            raise ValueError("analysis job finish requires a terminal status")
        normalized_error = self._terminal_error_code(status, error_code)
        if result_spill is not None and not isinstance(result_spill, SpillArtifactRef):
            result_spill = SpillArtifactRef.model_validate(result_spill)

        for _ in range(_CAS_RETRIES):
            record = await self._require_job(job_id, deadline=deadline)
            if record.status in ANALYSIS_JOB_TERMINAL_STATUSES:
                await self._drop_handle(job_id)
                await self._publish(record)
                await self._drop_callback(job_id)
                return record
            now = datetime.now(UTC)
            updated = await self._repository_call(
                lambda: self._repository.compare_and_set(
                    job_id,
                    expected_revision=record.revision,
                    expected_statuses=ANALYSIS_JOB_ACTIVE_STATUSES,
                    updates={
                        "status": status,
                        "finished_at": now,
                        "lease_expires_at": now,
                        "result_spill": result_spill,
                        "error_code": normalized_error,
                    },
                ),
                deadline=deadline,
            )
            if updated is None:
                continue
            await self._drop_handle(job_id)
            await self._publish(updated)
            await self._drop_callback(job_id)
            return updated
        raise RuntimeError("analysis job terminal state could not be persisted")

    async def get_for_owner(
        self,
        user_id: str,
        session_id: str,
        job_id: str,
    ) -> AnalysisJobRecord | None:
        deadline = self._state_deadline()
        return await self._repository_call(
            lambda: self._repository.find_for_owner(user_id, session_id, job_id),
            deadline=deadline,
        )

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[AnalysisJobRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("analysis job list limit must be a positive integer")
        deadline = self._state_deadline()
        return await self._repository_call(
            lambda: self._repository.list_for_owner(
                user_id,
                session_id,
                limit=min(limit, 100),
            ),
            deadline=deadline,
        )

    async def request_cancel(
        self,
        user_id: str,
        session_id: str,
        job_id: str,
    ) -> AnalysisJobRecord | None:
        deadline = self._state_deadline()
        for _ in range(_CAS_RETRIES):
            record = await self._repository_call(
                lambda: self._repository.find_for_owner(
                    user_id,
                    session_id,
                    job_id,
                ),
                deadline=deadline,
            )
            if record is None:
                return None
            if record.status in ANALYSIS_JOB_TERMINAL_STATUSES:
                return record
            if not record.cancellable:
                raise ValueError("analysis job is not cancellable")
            handle = await self._get_handle(job_id)
            # The execution result already exists; complete/spill projection may
            # still be persisting its terminal state. Let finish decide it.
            if handle is not None and handle.done():
                return record
            if record.status == AnalysisJobStatus.CANCELLING:
                if handle is not None:
                    handle.cancel()
                return record
            now = datetime.now(UTC)
            updated = await self._repository_call(
                lambda: self._repository.compare_and_set(
                    job_id,
                    expected_revision=record.revision,
                    expected_statuses=frozenset({
                        AnalysisJobStatus.QUEUED,
                        AnalysisJobStatus.RUNNING,
                    }),
                    updates={
                        "status": AnalysisJobStatus.CANCELLING,
                        "cancel_requested_at": now,
                    },
                ),
                deadline=deadline,
            )
            if updated is None:
                continue
            handle = await self._get_handle(job_id)
            if handle is not None and not handle.done():
                handle.cancel()
            await self._publish(updated)
            return updated
        raise RuntimeError("analysis job cancellation could not be persisted")

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        async with self._state_lock:
            active = [
                job_id
                for job_id, owner in self._owners.items()
                if owner == (user_id, session_id)
                and (handle := self._handles.get(job_id)) is not None
                and not handle.done()
            ]
        if active:
            raise RuntimeError("analysis job workers must stop before owner deletion")
        deadline = self._state_deadline()
        await self._repository_call(
            lambda: self._repository.delete_owner(user_id, session_id),
            deadline=deadline,
        )
        async with self._state_lock:
            for job_id, owner in list(self._owners.items()):
                if owner == (user_id, session_id):
                    self._owners.pop(job_id, None)
                    self._handles.pop(job_id, None)
                    self._callbacks.pop(job_id, None)

    async def maintain(self) -> None:
        if self._closed:
            return
        async with self._maintenance_lock:
            if self._closed:
                return
            deadline = self._state_deadline()
            now = datetime.now(UTC)
            async with self._state_lock:
                local_handles = tuple(self._handles.items())
            # A different monitor can terminalize an expired record. Its
            # original runtime must then stop and forget the stale worker even
            # though terminal records no longer appear in the active listing.
            for job_id, handle in local_handles:
                record = await self._repository_call(
                    lambda job_id=job_id: self._repository.find_by_id(job_id),
                    deadline=deadline,
                )
                if (
                    record is not None
                    and record.runtime_id == self._runtime_id
                    and record.status in ANALYSIS_JOB_ACTIVE_STATUSES
                ):
                    continue
                if not handle.done():
                    handle.cancel()
                await self._drop_handle(job_id, expected=handle)
                await self._drop_callback(job_id)

            records = await self._repository_call(
                lambda: self._repository.list_active_by_runtime(
                    self._runtime_id,
                    limit=_MAINTENANCE_BATCH_SIZE,
                ),
                deadline=deadline,
            )
            for record in records:
                handle = await self._get_handle(record.job_id)
                if record.status == AnalysisJobStatus.CANCELLING:
                    if handle is not None and not handle.done():
                        handle.cancel()
                # Unknown create outcomes, never-bound workers, and completed
                # workers awaiting a failed finish must age out. Only a live
                # bound execution with a still-valid lease earns a renewal. An
                # expired owner must compete in the interrupted CAS below; it
                # cannot resurrect itself ahead of another runtime's reaper.
                if (
                    handle is None
                    or handle.done()
                    or record.lease_expires_at <= now
                ):
                    continue
                await self._repository_call(
                    lambda record=record: self._repository.compare_and_set(
                        record.job_id,
                        expected_revision=record.revision,
                        expected_statuses=frozenset({record.status}),
                        expected_runtime_id=self._runtime_id,
                        updates={"lease_expires_at": now + self._lease},
                    ),
                    deadline=deadline,
                )

            expired = await self._repository_call(
                lambda: self._repository.list_expired_active(
                    now,
                    limit=_MAINTENANCE_BATCH_SIZE,
                ),
                deadline=deadline,
            )
            for record in expired:
                updated = await self._interrupt_expired(
                    record,
                    before=now,
                    deadline=deadline,
                )
                if updated is None:
                    continue
                handle = await self._get_handle(record.job_id)
                if handle is not None and not handle.done():
                    handle.cancel()
                await self._publish(updated)
                await self._drop_callback(record.job_id)

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._maintenance_lock:
            deadline = self._state_deadline()
            try:
                records = await self._repository_call(
                    lambda: self._repository.list_active_by_runtime(
                        self._runtime_id,
                        limit=_MAINTENANCE_BATCH_SIZE,
                    ),
                    deadline=deadline,
                )
                for record in records:
                    updated = await self._interrupt(record, deadline=deadline)
                    if updated is not None:
                        await self._publish(updated)
                        await self._drop_callback(record.job_id)
            except Exception as error:
                logger.warning(
                    "Analysis job shutdown persistence failed error_type=%s",
                    type(error).__name__,
                )

        async with self._state_lock:
            handles = tuple(self._handles.values())
        current = asyncio.current_task()
        pending = [task for task in handles if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        async with self._state_lock:
            self._handles.clear()
            self._callbacks.clear()
            self._owners.clear()

    async def _interrupt_expired(
        self,
        record: AnalysisJobRecord,
        *,
        before: datetime,
        deadline: float,
    ) -> AnalysisJobRecord | None:
        now = datetime.now(UTC)
        return await self._repository_call(
            lambda: self._repository.compare_and_set(
                record.job_id,
                expected_revision=record.revision,
                expected_statuses=frozenset({record.status}),
                expected_runtime_id=record.runtime_id,
                lease_expires_at_lte=before,
                updates={
                    "status": AnalysisJobStatus.INTERRUPTED,
                    "finished_at": now,
                    "lease_expires_at": now,
                    "error_code": AnalysisJobErrorCode.WORKER_INTERRUPTED,
                },
            ),
            deadline=deadline,
        )

    async def _interrupt(
        self,
        record: AnalysisJobRecord,
        *,
        deadline: float,
    ) -> AnalysisJobRecord | None:
        now = datetime.now(UTC)
        return await self._repository_call(
            lambda: self._repository.compare_and_set(
                record.job_id,
                expected_revision=record.revision,
                expected_statuses=frozenset({record.status}),
                expected_runtime_id=self._runtime_id,
                updates={
                    "status": AnalysisJobStatus.INTERRUPTED,
                    "finished_at": now,
                    "lease_expires_at": now,
                    "error_code": AnalysisJobErrorCode.WORKER_INTERRUPTED,
                },
            ),
            deadline=deadline,
        )

    @staticmethod
    def _terminal_error_code(
        status: AnalysisJobStatus,
        error_code: AnalysisJobErrorCode | str | None,
    ) -> AnalysisJobErrorCode | None:
        expected = _DEFAULT_ERROR_CODES.get(status)
        if expected is None:
            if error_code is not None:
                raise ValueError("successful analysis job cannot contain an error code")
            return None
        normalized = expected if error_code is None else AnalysisJobErrorCode(error_code)
        if normalized != expected:
            raise ValueError("analysis job error code does not match terminal status")
        return normalized

    async def _require_job(
        self,
        job_id: str,
        *,
        deadline: float,
    ) -> AnalysisJobRecord:
        record = await self._repository_call(
            lambda: self._repository.find_by_id(job_id),
            deadline=deadline,
        )
        if record is None:
            raise KeyError("analysis job was not found")
        return record

    async def _get_handle(self, job_id: str) -> asyncio.Task | None:
        async with self._state_lock:
            return self._handles.get(job_id)

    async def _drop_handle(
        self,
        job_id: str,
        *,
        expected: asyncio.Task | None = None,
    ) -> None:
        async with self._state_lock:
            current = self._handles.get(job_id)
            if expected is None or current is expected:
                self._handles.pop(job_id, None)
                self._owners.pop(job_id, None)

    async def _drop_callback(self, job_id: str) -> None:
        async with self._state_lock:
            self._callbacks.pop(job_id, None)

    async def _publish(self, record: AnalysisJobRecord) -> None:
        async with self._state_lock:
            callback = self._callbacks.get(record.job_id)
        if callback is None:
            return
        try:
            async with asyncio.timeout(_UPDATE_TIMEOUT_SECONDS):
                await callback(record.public_view())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Analysis job update failed job=%s callback=%s error_type=%s",
                _job_log_ref(record.job_id),
                type(callback).__name__,
                type(error).__name__,
            )


__all__ = ["AnalysisJobService", "AnalysisJobUpdateCallback"]
