from __future__ import annotations

import asyncio
import hmac
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Literal, TypeVar

from app.domain.models.tool_approval import (
    TOOL_APPROVAL_UNCONSUMED_STATUSES,
    ToolApprovalEffect,
    ToolApprovalRecord,
    ToolApprovalStatus,
)
from app.domain.repositories.tool_approval_repository import ToolApprovalRepository


_REPOSITORY_TIMEOUT_SECONDS = 5.0
_MAINTENANCE_BATCH_SIZE = 500
_T = TypeVar("_T")


class ToolApprovalNotFoundError(LookupError):
    """An approval is absent from the requested owner/session boundary."""

    def __init__(self) -> None:
        super().__init__("Tool approval not found")


class ToolApprovalConflictError(RuntimeError):
    """The requested transition lost its CAS or is no longer allowed."""

    def __init__(self) -> None:
        super().__init__("Tool approval state conflict")


class ToolApprovalStateError(RuntimeError):
    """A bounded persistence operation failed without exposing private data."""

    def __init__(self) -> None:
        super().__init__("Tool approval state operation failed")


@dataclass(frozen=True, slots=True)
class _CreationContext:
    user_id: str
    session_id: str
    call_digest: str


class ToolApprovalService:
    """Durable decisions for exact calls; this service never executes a tool."""

    def __init__(
        self,
        repository: ToolApprovalRepository,
        *,
        approval_ttl_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(approval_ttl_seconds, bool)
            or not isinstance(approval_ttl_seconds, (int, float))
            or not math.isfinite(float(approval_ttl_seconds))
            or float(approval_ttl_seconds) <= 0
        ):
            raise ValueError("tool approval lifetime must be a positive finite number")
        self._repository = repository
        self._approval_ttl = timedelta(seconds=float(approval_ttl_seconds))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runtime_id = uuid.uuid4().hex
        self._creation_contexts: dict[str, _CreationContext] = {}
        self._state_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._closed = False

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    async def _repository_call(operation: Callable[[], Awaitable[_T]]) -> _T:
        try:
            async with asyncio.timeout(_REPOSITORY_TIMEOUT_SECONDS):
                return await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ToolApprovalStateError() from None

    async def create(
        self,
        *,
        user_id: str,
        session_id: str,
        task_id: str,
        tool_name: str,
        call_digest: str,
        effects: list[ToolApprovalEffect | str],
        permissions: list[str],
        arguments_preview: dict[str, Any],
        credential_refs: list[str],
        execution_snapshot_id: str | None = None,
        catalog_revision: str | None = None,
    ) -> ToolApprovalRecord:
        if self._closed:
            raise ToolApprovalConflictError()
        now = self._now()
        record = ToolApprovalRecord(
            user_id=user_id,
            session_id=session_id,
            task_id=task_id,
            runtime_id=self._runtime_id,
            call_digest=call_digest,
            tool_name=tool_name,
            effects=effects,
            permissions=permissions,
            arguments_preview=arguments_preview,
            credential_refs=credential_refs,
            execution_snapshot_id=execution_snapshot_id,
            catalog_revision=catalog_revision,
            created_at=now,
            expires_at=now + self._approval_ttl,
        )
        persisted = await self._repository_call(
            lambda: self._repository.insert(record)
        )
        async with self._state_lock:
            if self._closed:
                # The shutdown sweep owns this runtime and will cancel the
                # record. Do not publish a consumable in-memory context.
                raise ToolApprovalConflictError()
            self._creation_contexts[persisted.approval_id] = _CreationContext(
                user_id=persisted.user_id,
                session_id=persisted.session_id,
                call_digest=persisted.call_digest,
            )
        return persisted

    async def get_for_owner(
        self,
        user_id: str,
        session_id: str,
        approval_id: str,
    ) -> ToolApprovalRecord | None:
        record = await self._repository_call(
            lambda: self._repository.find_for_owner(user_id, session_id, approval_id)
        )
        if record is None:
            return None
        return await self._expire_if_due(record)

    async def list_for_owner(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[ToolApprovalRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("tool approval list limit must be a positive integer")
        records = await self._repository_call(
            lambda: self._repository.list_for_owner(
                user_id,
                session_id,
                limit=min(limit, 100),
            )
        )
        return [await self._expire_if_due(record) for record in records]

    async def decide(
        self,
        user_id: str,
        session_id: str,
        approval_id: str,
        decision: Literal["approved", "rejected"],
        expected_revision: int,
    ) -> ToolApprovalRecord:
        try:
            status = ToolApprovalStatus(decision)
        except (TypeError, ValueError):
            raise ValueError("invalid tool approval decision") from None
        if status not in {ToolApprovalStatus.APPROVED, ToolApprovalStatus.REJECTED}:
            raise ValueError("invalid tool approval decision")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("invalid tool approval revision")

        record = await self._repository_call(
            lambda: self._repository.find_for_owner(user_id, session_id, approval_id)
        )
        if record is None:
            raise ToolApprovalNotFoundError()
        record = await self._expire_if_due(record)
        if record.status != ToolApprovalStatus.PENDING:
            raise ToolApprovalConflictError()

        now = self._now()
        updated = await self._repository_call(
            lambda: self._repository.compare_and_set(
                approval_id,
                expected_revision=expected_revision,
                expected_statuses=frozenset({ToolApprovalStatus.PENDING}),
                expected_user_id=user_id,
                expected_session_id=session_id,
                expires_at_gt=now,
                updates={"status": status, "decided_at": now},
            )
        )
        if updated is None:
            existing = await self._repository_call(
                lambda: self._repository.find_for_owner(
                    user_id,
                    session_id,
                    approval_id,
                )
            )
            if existing is None:
                raise ToolApprovalNotFoundError()
            raise ToolApprovalConflictError()
        if status == ToolApprovalStatus.REJECTED:
            await self._drop_creation_context(approval_id)
        return updated

    async def consume(
        self,
        approval_id: str,
        call_digest: str,
    ) -> ToolApprovalRecord:
        async with self._state_lock:
            context = self._creation_contexts.get(approval_id)
        if context is None or not isinstance(call_digest, str):
            raise ToolApprovalConflictError()
        if not hmac.compare_digest(context.call_digest, call_digest):
            raise ToolApprovalConflictError()

        updated = await self._repository_call(
            lambda: self._repository.consume_approved(
                approval_id,
                expected_user_id=context.user_id,
                expected_session_id=context.session_id,
                expected_runtime_id=self._runtime_id,
                expected_call_digest=call_digest,
                unexpired_at=self._now(),
            )
        )
        if updated is None:
            raise ToolApprovalConflictError()
        await self._drop_creation_context(approval_id)
        return updated

    async def cancel(self, approval_id: str) -> ToolApprovalRecord:
        record = await self._repository_call(
            lambda: self._repository.find_by_id(approval_id)
        )
        if record is None:
            raise ToolApprovalNotFoundError()
        if (
            record.runtime_id != self._runtime_id
            or record.status not in TOOL_APPROVAL_UNCONSUMED_STATUSES
        ):
            raise ToolApprovalConflictError()
        updated = await self._repository_call(
            lambda: self._repository.compare_and_set(
                approval_id,
                expected_revision=record.revision,
                expected_statuses=frozenset({record.status}),
                expected_user_id=record.user_id,
                expected_session_id=record.session_id,
                expected_runtime_id=self._runtime_id,
                updates={"status": ToolApprovalStatus.CANCELLED},
            )
        )
        if updated is None:
            raise ToolApprovalConflictError()
        await self._drop_creation_context(approval_id)
        return updated

    async def maintain(self) -> int:
        if self._closed:
            return 0
        async with self._maintenance_lock:
            if self._closed:
                return 0
            now = self._now()
            records = await self._repository_call(
                lambda: self._repository.list_expired_unconsumed(
                    now,
                    limit=_MAINTENANCE_BATCH_SIZE,
                )
            )
            expired = 0
            for record in records:
                updated = await self._expire_record(record, before=now)
                if updated is not None:
                    expired += 1
                    await self._drop_creation_context(record.approval_id)
            return expired

    async def shutdown(self) -> int:
        if self._closed:
            return 0
        self._closed = True
        cancelled = 0
        try:
            async with self._maintenance_lock:
                while True:
                    records = await self._repository_call(
                        lambda: self._repository.list_unconsumed_by_runtime(
                            self._runtime_id,
                            limit=_MAINTENANCE_BATCH_SIZE,
                        )
                    )
                    if not records:
                        break
                    changed = 0
                    for record in records:
                        updated = await self._repository_call(
                            lambda record=record: self._repository.compare_and_set(
                                record.approval_id,
                                expected_revision=record.revision,
                                expected_statuses=frozenset({record.status}),
                                expected_user_id=record.user_id,
                                expected_session_id=record.session_id,
                                expected_runtime_id=self._runtime_id,
                                updates={"status": ToolApprovalStatus.CANCELLED},
                            )
                        )
                        if updated is not None:
                            changed += 1
                    cancelled += changed
                    if len(records) < _MAINTENANCE_BATCH_SIZE:
                        break
                    if changed == 0:
                        break
        finally:
            async with self._state_lock:
                self._creation_contexts.clear()
        return cancelled

    async def delete_owner(self, user_id: str, session_id: str) -> None:
        await self._repository_call(
            lambda: self._repository.delete_owner(user_id, session_id)
        )
        async with self._state_lock:
            for approval_id, context in list(self._creation_contexts.items()):
                if context.user_id == user_id and context.session_id == session_id:
                    self._creation_contexts.pop(approval_id, None)

    async def _expire_if_due(self, record: ToolApprovalRecord) -> ToolApprovalRecord:
        for _ in range(2):
            now = self._now()
            if (
                record.status not in TOOL_APPROVAL_UNCONSUMED_STATUSES
                or record.expires_at > now
            ):
                return record
            updated = await self._expire_record(record, before=now)
            if updated is not None:
                await self._drop_creation_context(record.approval_id)
                return updated
            latest = await self._repository_call(
                lambda: self._repository.find_for_owner(
                    record.user_id,
                    record.session_id,
                    record.approval_id,
                )
            )
            if latest is None:
                return record
            record = latest
        return record

    async def _expire_record(
        self,
        record: ToolApprovalRecord,
        *,
        before: datetime,
    ) -> ToolApprovalRecord | None:
        return await self._repository_call(
            lambda: self._repository.compare_and_set(
                record.approval_id,
                expected_revision=record.revision,
                expected_statuses=frozenset({record.status}),
                expected_user_id=record.user_id,
                expected_session_id=record.session_id,
                expected_runtime_id=record.runtime_id,
                expires_at_lte=before,
                updates={"status": ToolApprovalStatus.EXPIRED},
            )
        )

    async def _drop_creation_context(self, approval_id: str) -> None:
        async with self._state_lock:
            self._creation_contexts.pop(approval_id, None)


__all__ = [
    "ToolApprovalConflictError",
    "ToolApprovalNotFoundError",
    "ToolApprovalService",
    "ToolApprovalStateError",
]
