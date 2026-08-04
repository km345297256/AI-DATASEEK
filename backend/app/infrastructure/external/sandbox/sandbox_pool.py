import asyncio
import logging
from typing import Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

logger = logging.getLogger(__name__)

_pool_instance: Optional['SandboxPool'] = None


class SandboxPool:
    def __init__(self, pool_size: int):
        self._configured_pool_size = pool_size
        self._pool_size = min(pool_size, 3)
        self._pool: asyncio.Queue = asyncio.Queue()
        self._replenish_lock = asyncio.Lock()
        self._replenish_retry_seconds = 10
        self._replenish_interval_seconds = 1
        self._background_tasks: Set[asyncio.Task] = set()
        self._replenish_task: Optional[asyncio.Task] = None
        if self._configured_pool_size != self._pool_size:
            logger.warning(
                "Sandbox pool size capped from %s to %s to avoid startup resource exhaustion",
                self._configured_pool_size,
                self._pool_size,
            )

    def start_background_init(self) -> None:
        self._track_background_task(self._initialize())

    def _track_background_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def on_done(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                exception = done_task.exception()
            except Exception:
                logger.exception("Sandbox pool background task failed")
                return
            if exception:
                logger.error(
                    "Sandbox pool background task failed",
                    exc_info=(type(exception), exception, exception.__traceback__),
                )

        task.add_done_callback(on_done)
        return task

    async def _initialize(self):
        logger.info(
            "Pre-warming %s sandbox containers (throttled single-create mode)...",
            self._pool_size,
        )
        await self._replenish()
        logger.info(f"Sandbox pool ready with {self._pool.qsize()} warm containers")

    async def acquire(self) -> 'DockerSandbox':
        while True:
            try:
                sandbox = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                logger.warning("Sandbox pool empty, falling back to direct creation")
                return await self._create_and_warm()

            if await sandbox.is_available():
                logger.info(f"Acquired warm sandbox {sandbox.id} from pool (remaining: {self._pool.qsize()})")
                self.schedule_replenish()
                return sandbox

            logger.warning("Discarding unavailable warm sandbox %s", sandbox.id)
            await sandbox.destroy()

    def schedule_replenish(self) -> None:
        if self._pool_size <= 0:
            return
        if self._replenish_task and not self._replenish_task.done():
            return
        self._replenish_task = self._track_background_task(self._replenish())

    async def _create_and_warm(self) -> 'DockerSandbox':
        from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
        from app.infrastructure.external.sandbox.runtime import get_default_sandbox_runtime

        sandbox = await get_default_sandbox_runtime().allocate()
        await sandbox.ensure_sandbox()
        await DockerSandbox.create_record(sandbox.id, sandbox.ip, status="warm")
        return sandbox

    async def _replenish(self):
        async with self._replenish_lock:
            if self._pool.qsize() >= self._pool_size:
                return
            deficit = self._pool_size - self._pool.qsize()
            logger.info(f"Replenishing sandbox pool ({deficit} needed, creating 1)")
            try:
                sandbox = await self._create_and_warm()
                await self._pool.put(sandbox)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "Pool replenish failed; retrying in %s seconds: %s",
                    self._replenish_retry_seconds,
                    e,
                )
                await asyncio.sleep(self._replenish_retry_seconds)
            else:
                await asyncio.sleep(self._replenish_interval_seconds)
            finally:
                if self._pool.qsize() < self._pool_size:
                    self._replenish_task = None
                    self.schedule_replenish()

    async def shutdown(self):
        logger.info(f"Shutting down sandbox pool, destroying {self._pool.qsize()} warm containers")
        for task in list(self._background_tasks):
            task.cancel()
        while not self._pool.empty():
            try:
                sandbox = self._pool.get_nowait()
                await sandbox.destroy()
            except Exception as e:
                logger.error(f"Failed to destroy pooled sandbox: {e}")


def get_sandbox_pool() -> Optional[SandboxPool]:
    return _pool_instance


def set_sandbox_pool(pool: SandboxPool) -> None:
    global _pool_instance
    _pool_instance = pool
