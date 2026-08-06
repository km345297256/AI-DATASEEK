import asyncio
import logging
from datetime import UTC, datetime
from typing import Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

logger = logging.getLogger(__name__)

_pool_instance: Optional['SandboxPool'] = None


class SandboxPool:
    def __init__(self, pool_size: int):
        self._pool_size = max(0, int(pool_size))
        self._pool: asyncio.Queue = asyncio.Queue()
        self._replenish_lock = asyncio.Lock()
        self._replenish_retry_seconds = 10
        self._replenish_interval_seconds = 1
        self._background_tasks: Set[asyncio.Task] = set()
        self._replenish_task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self._pool_size > 0

    @property
    def target_size(self) -> int:
        return self._pool_size

    @property
    def warm_count(self) -> int:
        return self._pool.qsize()

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
        await self._adopt_or_retire_existing_warm_sandboxes()
        if self.enabled:
            await self._replenish()
        logger.info(f"Sandbox pool ready with {self._pool.qsize()} warm containers")

    async def _adopt_or_retire_existing_warm_sandboxes(self) -> None:
        """Converge warm containers left by a previous backend process."""
        from app.core.config import get_settings
        from app.infrastructure.external.sandbox.container_identity import is_sandbox_container_name
        from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
        from app.infrastructure.models.documents import SandboxRecordDocument

        records = await SandboxRecordDocument.find(
            {"status": {"$in": ["warm", "warming"]}},
        ).to_list()
        name_prefix = get_settings().sandbox_name_prefix
        for record in records:
            if not is_sandbox_container_name(record.container_name, name_prefix):
                continue
            sandbox = None
            try:
                sandbox = await DockerSandbox.get(record.container_name)
                available = await sandbox.is_available()
            except Exception:
                available = False

            if available and self._pool.qsize() < self._pool_size:
                await self._pool.put(sandbox)
                logger.info("Adopted existing warm sandbox %s", record.container_name)
                continue

            if sandbox is not None:
                try:
                    if not await sandbox.destroy():
                        raise RuntimeError("sandbox destroy returned false")
                    continue
                except Exception as exc:
                    logger.warning("Failed to retire orphan warm sandbox %s: %s", record.container_name, exc)
                    continue

            record.status = "destroyed"
            record.destroyed_at = datetime.now(UTC)
            record.last_used_at = record.destroyed_at
            await record.save()

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
            if not await sandbox.destroy():
                logger.error("Failed to destroy unavailable warm sandbox %s", sandbox.id)

    def schedule_replenish(self) -> None:
        if self._pool_size <= 0:
            return
        if self._replenish_task and not self._replenish_task.done():
            return
        self._replenish_task = self._track_background_task(self._replenish())

    async def _create_and_warm(self) -> 'DockerSandbox':
        from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
        from app.infrastructure.external.sandbox.runtime import LocalDockerRuntime
        from app.infrastructure.models.documents import SandboxRecordDocument

        sandbox = await LocalDockerRuntime().allocate_local_warm()
        await DockerSandbox.create_record(sandbox.id, sandbox.ip, status="warming")
        try:
            ensure_api_ready = getattr(sandbox, "ensure_api_ready", None)
            if callable(ensure_api_ready):
                await ensure_api_ready()
            else:
                await sandbox.ensure_sandbox()
            record = await SandboxRecordDocument.find_one(
                SandboxRecordDocument.container_name == sandbox.id,
            )
            if not record:
                raise RuntimeError(f"Warm sandbox {sandbox.id} has no lifecycle record")
            record.status = "warm"
            record.last_used_at = datetime.now(UTC)
            await record.save()
            return sandbox
        except Exception:
            if not await sandbox.destroy():
                logger.error("Failed to destroy sandbox %s after warm-up error", sandbox.id)
            raise

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

    async def resize(self, pool_size: int) -> None:
        """Apply a new warm target without restarting the backend."""
        target = max(0, int(pool_size))
        surplus = []
        async with self._replenish_lock:
            previous = self._pool_size
            self._pool_size = target
            while self._pool.qsize() > target:
                try:
                    surplus.append(self._pool.get_nowait())
                except asyncio.QueueEmpty:
                    break
        for sandbox in surplus:
            try:
                if not await sandbox.destroy():
                    raise RuntimeError("sandbox destroy returned false")
            except Exception as exc:
                logger.error("Failed to destroy surplus warm sandbox %s: %s", sandbox.id, exc)
        logger.info("Resized sandbox warm pool from %s to %s", previous, target)
        if self.enabled and self._pool.qsize() < self._pool_size:
            self.schedule_replenish()

    async def shutdown(self):
        logger.info(f"Shutting down sandbox pool, destroying {self._pool.qsize()} warm containers")
        for task in list(self._background_tasks):
            task.cancel()
        while not self._pool.empty():
            try:
                sandbox = self._pool.get_nowait()
                if not await sandbox.destroy():
                    raise RuntimeError("sandbox destroy returned false")
            except Exception as e:
                logger.error(f"Failed to destroy pooled sandbox: {e}")


def get_sandbox_pool() -> Optional[SandboxPool]:
    return _pool_instance


def set_sandbox_pool(pool: Optional[SandboxPool]) -> None:
    global _pool_instance
    _pool_instance = pool


async def configure_sandbox_pool(pool_size: int) -> SandboxPool:
    """Create or resize the node-local singleton warm pool."""
    pool = get_sandbox_pool()
    if pool is None:
        pool = SandboxPool(pool_size)
        set_sandbox_pool(pool)
        pool.start_background_init()
        return pool
    await pool.resize(pool_size)
    return pool
