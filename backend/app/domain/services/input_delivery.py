"""Recover accepted inputs without replaying an execution with unknown effects."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, ToolEvent, ToolStatus, WaitEvent
from app.domain.models.input_admission import AcceptedInput, input_key, terminal_event_id
from app.domain.models.session import SessionStatus

logger = logging.getLogger(__name__)
TERMINALS = (DoneEvent, ErrorEvent, WaitEvent)


class InputLeaseLost(asyncio.CancelledError):
    """No more tools or output may be produced by this expired attempt."""


@dataclass
class _LocalInput:
    record: AcceptedInput
    worker: asyncio.Task
    task: Any = None

    def live(self) -> bool:
        return not self.worker.done() or (self.task is not None and not self.task.done)

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if not self.worker.done() and self.worker is not asyncio.current_task():
            self.worker.cancel()


class InputDeliveryService:
    def __init__(self, repository, session_repository, *, lease_seconds: float = 30):
        self.repository = repository
        self._sessions = session_repository
        self.runtime_id = uuid4().hex
        self._lease = timedelta(seconds=lease_seconds)
        self._local: dict[tuple[str, str], _LocalInput] = {}
        self._maintenance_lock = asyncio.Lock()

    async def claim(self, record: AcceptedInput) -> AcceptedInput | None:
        if not await self.repository.authorized(record):
            await self.repository.transition(record, {"state": "cancelled", "notified": False})
            return None
        claimed = await self.repository.claim(record, self.runtime_id, datetime.now(UTC) + self._lease)
        if claimed is not None:
            self._local[(record.session_id, record.key)] = _LocalInput(claimed, asyncio.current_task())
        return claimed

    async def bind(self, record: AcceptedInput, task) -> None:
        current = await self._require_live(record.session_id, record.key, states={"claimed"})
        updated = await self.repository.transition(current, {"task_id": task.id}, require_live=True)
        if updated is None:
            raise InputLeaseLost()
        local = self._local[(record.session_id, record.key)]
        local.record, local.task = updated, task

    async def start(self, session_id: str, event: MessageEvent, task) -> bool:
        key = input_key(event)
        record = await self.repository.get(session_id, key)
        if record is None:
            # Only genuinely legacy events may bypass the new admission path.
            return False
        record = await self._require_live(session_id, key, states={"claimed"})
        if record.admission.task_id != task.id or not await self.repository.authorized(record):
            raise InputLeaseLost()
        updated = await self.repository.transition(record, {"state": "running"}, require_live=True)
        if updated is None:
            raise InputLeaseLost()
        self._local[(session_id, key)] = _LocalInput(updated, asyncio.current_task(), task)
        return True

    async def _require_live(self, session_id: str, key: str, *, states=None) -> AcceptedInput:
        record = await self.repository.get(session_id, key)
        if (record is None or record.admission.state not in (states or {"claimed", "running"})
                or record.admission.runtime_id != self.runtime_id
                or record.admission.lease_expires_at is None
                or record.admission.lease_expires_at <= datetime.now(UTC)):
            raise InputLeaseLost()
        return record

    async def prepare_event(self, session_id: str, key: str, event) -> None:
        await self._require_live(session_id, key)
        if isinstance(event, TERMINALS):
            # Recovery can identify a committed terminal even when the process
            # exited before the following state update. Nothing new enters SSE.
            event.id = terminal_event_id(key, event.type)

    async def complete(self, session_id: str, key: str, event) -> None:
        if not isinstance(event, TERMINALS):
            return
        for _ in range(3):
            record = await self._require_live(session_id, key)
            updated = await self.repository.transition(record, {"state": "completed", "terminal_kind": event.type,
                "notified": True, "lease_expires_at": datetime.now(UTC)}, require_live=True)
            if updated is not None:
                self._local.pop((session_id, key), None)
                return
        raise InputLeaseLost()

    async def retry_preparation(self, record: AcceptedInput) -> None:
        current = await self.repository.get(record.session_id, record.key)
        if current and current.admission.state == "claimed" and current.admission.runtime_id == self.runtime_id:
            if current.admission.attempts >= 3:
                await self.repository.transition(current, {"state": "interrupted", "notified": False})
            else:
                await self.repository.transition(current, {"state": "pending", "runtime_id": None, "task_id": None,
                    "lease_expires_at": None, "retry_after": datetime.now(UTC) + timedelta(seconds=5)})
        self._local.pop((record.session_id, record.key), None)

    async def maintain(self, dispatch: Callable[[AcceptedInput], Awaitable[None]]) -> None:
        async with self._maintenance_lock:
            now = datetime.now(UTC)
            for identity, local in tuple(self._local.items()):
                record = await self.repository.get(*identity)
                if (record is None or record.admission.state not in {"claimed", "running"}
                        or record.admission.runtime_id != self.runtime_id):
                    local.cancel()
                    self._local.pop(identity, None)
                    continue
                if local.live() and record.admission.lease_expires_at > now:
                    await self.repository.transition(record, {"lease_expires_at": now + self._lease}, require_live=True)
            for record in await self.repository.candidates(now):
                if record.admission.state in {"claimed", "running"}:
                    kind = await self.repository.terminal_kind(record)
                    if kind:
                        updated = await self.repository.transition(record, {"state": "completed", "terminal_kind": kind,
                            "notified": True, "lease_expires_at": now})
                        if updated:
                            await self._update_session_status(updated, kind)
                    elif record.admission.state == "claimed":
                        await self.repository.transition(record, {"state": "pending", "runtime_id": None,
                            "task_id": None, "lease_expires_at": None, "retry_after": now})
                    else:
                        # A running input may have executed arbitrary tools. It
                        # is never returned to pending, even after a restart.
                        await self.repository.transition(record, {"state": "interrupted", "notified": False})
                elif record.admission.state in {"interrupted", "cancelled"}:
                    await self._notify_stopped(record)
                elif record.admission.state == "pending":
                    await dispatch(record)

    async def _update_session_status(self, record: AcceptedInput, kind: str) -> None:
        session = await self._sessions.find_by_id(record.session_id)
        if session and (not record.admission.task_id or session.task_id == record.admission.task_id):
            await self._sessions.update_status(record.session_id,
                SessionStatus.WAITING if kind == "wait" else SessionStatus.COMPLETED)

    async def _notify_stopped(self, record: AcceptedInput) -> None:
        session = await self._sessions.find_by_id(record.session_id)
        if not session:
            await self.repository.transition(record, {"notified": True})
            return
        kind = await self.repository.terminal_kind(record)
        if kind is None:
            message = ("本轮执行已中断，系统没有自动重跑。已有结果已保留，请检查后再继续。"
                       if record.admission.state == "interrupted" else "本轮请求已取消，未完成的执行不会自动重跑。")
            event = ErrorEvent(id=terminal_event_id(record.key, "error"), error=message)
            await self._sessions.add_event(record.session_id, event)
            kind = "error"
        await self._update_session_status(record, kind)
        await self.repository.transition(record, {"notified": True, "terminal_kind": kind})

    async def cancel_session(self, session_id: str) -> None:
        await self.repository.cancel_session(session_id)
        for identity, local in tuple(self._local.items()):
            if identity[0] == session_id:
                local.cancel()
                self._local.pop(identity, None)
