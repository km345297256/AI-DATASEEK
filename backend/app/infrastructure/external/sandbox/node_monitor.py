import asyncio
import logging
from contextlib import suppress
from datetime import datetime, UTC, timedelta

import docker

from app.domain.models.execution_node import ExecutionNodeStatus, ExecutionNodeType
from app.infrastructure.external.sandbox.node_health import check_execution_node, ensure_local_default_node
from app.infrastructure.models.documents import ExecutionNodeDocument, SandboxRecordDocument

logger = logging.getLogger(__name__)


class ExecutionNodeMonitor:
    def __init__(self, interval_seconds: int = 30):
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.check_once()
            except Exception as exc:
                logger.warning("Execution node monitor tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def check_once(self) -> None:
        await ensure_local_default_node()
        nodes = await ExecutionNodeDocument.find(ExecutionNodeDocument.enabled == True).to_list()  # noqa: E712
        nodes = [node for node in nodes if node.status != ExecutionNodeStatus.DELETED]
        if not nodes:
            return
        results = await asyncio.gather(
            *(check_execution_node(node) for node in nodes),
            return_exceptions=True,
        )
        for node, result in zip(nodes, results):
            if isinstance(result, Exception):
                logger.warning("Execution node %s health check failed: %s", node.node_id, result)
                continue
            await self._destroy_expired_paused_sandboxes(node)

    async def _destroy_expired_paused_sandboxes(self, node: ExecutionNodeDocument) -> None:
        if node.type != ExecutionNodeType.LOCAL_DOCKER:
            return
        minutes = node.runtime_config.get("paused_sandbox_destroy_after_minutes")
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return
        if minutes <= 0:
            return

        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        records = await SandboxRecordDocument.find({"status": "paused"}).to_list()
        expired = [
            record
            for record in records
            if (record.last_used_at or record.paused_at or record.assigned_at or record.created_at) <= cutoff
        ]
        if not expired:
            return

        client = docker.from_env()
        try:
            for record in expired:
                try:
                    container = await asyncio.to_thread(client.containers.get, record.container_name)
                    await asyncio.to_thread(container.remove, force=True)
                except Exception as exc:
                    logger.warning("Failed to remove expired paused sandbox %s: %s", record.container_name, exc)
                    continue
                now = datetime.now(UTC)
                record.status = "destroyed"
                record.destroyed_at = now
                record.last_used_at = now
                await record.save()
                logger.info("Destroyed expired paused sandbox %s after %s inactive minutes", record.container_name, minutes)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
