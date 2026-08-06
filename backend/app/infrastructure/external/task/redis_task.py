import asyncio
import uuid
import logging
from contextlib import suppress
from typing import Optional, Dict

from app.domain.external.task import Task, TaskRunner
from app.domain.external.task import TaskInputClosedError
from app.infrastructure.external.message_queue.redis_stream_queue import RedisStreamQueue, MessageQueue

logger = logging.getLogger(__name__)


class RedisStreamTask(Task):
    """Redis Stream-based task implementation following the Task protocol."""
    
    _task_registry: Dict[str, 'RedisStreamTask'] = {}
    
    def __init__(self, runner: TaskRunner):
        """Initialize Redis Stream task with a task runner.
        
        Args:
            runner: The TaskRunner instance that will execute this task
        """
        self._runner = runner
        self._id = str(uuid.uuid4())
        self._execution_task: Optional[asyncio.Task] = None
        self._input_lifecycle_lock = asyncio.Lock()
        self._closing = False
        self._closed = asyncio.Event()
        
        # Create input/output streams based on task ID
        input_stream_name = f"task:input:{self._id}"
        output_stream_name = f"task:output:{self._id}"
        self._input_stream = RedisStreamQueue(input_stream_name)
        self._output_stream = RedisStreamQueue(output_stream_name)
        
        # Register task instance
        RedisStreamTask._task_registry[self._id] = self
        
    @property
    def id(self) -> str:
        """Task ID."""
        return self._id
    
    @property
    def done(self) -> bool:
        """Check if the task is done.

        Returns:
            bool: True if the task is done, False otherwise
        """
        if self._execution_task is None:
            return True
        return self._execution_task.done()

    @property
    def accepting_input(self) -> bool:
        return not self._closing and not self._closed.is_set()

    async def enqueue_input(self, message) -> str:
        """Atomically enqueue input unless the runner has begun cleanup."""
        async with self._input_lifecycle_lock:
            if self._closing or self._closed.is_set():
                raise TaskInputClosedError(
                    f"Task {self._id} is closing and cannot accept new input"
                )
            event_id = await self._input_stream.put(message)
            # ``cancel`` is synchronous and may set the closing flag while the
            # Redis write is in flight. Remove that just-written message before
            # handing the caller a false success.
            if self._closing or self._closed.is_set():
                try:
                    await self._input_stream.delete_message(event_id)
                except Exception:
                    logger.exception(
                        "Task %s could not remove input queued during shutdown",
                        self._id,
                    )
                raise TaskInputClosedError(
                    f"Task {self._id} closed while accepting input"
                )
            return event_id

    async def pop_input_or_close(self):
        """Atomically pop input or commit the task to graceful cleanup."""
        async with self._input_lifecycle_lock:
            if self._closing:
                return None, None
            if await self._input_stream.is_empty():
                self._closing = True
                return None, None
            return await self._input_stream.pop()

    async def wait_closed(self) -> None:
        await self._closed.wait()
    
    async def run(self) -> None:
        """Run the task using the provided TaskRunner."""
        if self._closing or self._closed.is_set():
            raise TaskInputClosedError(f"Task {self._id} is already closed")
        if self.done:
            self._execution_task = asyncio.create_task(self._execute_task())
            logger.info(f"Task {self._id} execution started")
    
    def cancel(self) -> bool:
        """Cancel the task.

        Returns:
            bool: True if the task is cancelled, False otherwise
        """
        self._closing = True
        if self._execution_task is None:
            self._cleanup_registry()
            self._closed.set()
            return False
        if not self.done:
            self._execution_task.cancel()
            logger.info(f"Task {self._id} cancelled")
            return True
        
        self._cleanup_registry()
        return False
    
    @property
    def input_stream(self) -> MessageQueue:
        """Input stream."""
        return self._input_stream
    
    @property
    def output_stream(self) -> MessageQueue:
        """Output stream."""
        return self._output_stream
    
    async def _on_task_done(self) -> None:
        """Called when the task is done."""
        self._closing = True
        try:
            if self._runner:
                await self._runner.on_done(self)
        finally:
            self._cleanup_registry()
            self._closed.set()
    
    def _cleanup_registry(self) -> None:
        """Remove this task from the registry."""
        if self._id in RedisStreamTask._task_registry:
            del RedisStreamTask._task_registry[self._id]
            logger.info(f"Task {self._id} removed from registry")
    
    async def _execute_task(self):
        """Execute the task using the TaskRunner."""
        try:
            await self._runner.run(self)
        except asyncio.CancelledError:
            logger.info(f"Task {self._id} execution cancelled")
        except Exception as e:
            logger.error(f"Task {self._id} execution failed: {str(e)}")
        finally:
            await self._on_task_done()
    
    @classmethod
    def get(cls, task_id: str) -> Optional['RedisStreamTask']:
        """Get a task by its ID.

        Returns:
            Optional[RedisStreamTask]: Task instance if found, None otherwise
        """
        return cls._task_registry.get(task_id)
    
    @classmethod
    def create(cls, runner: TaskRunner) -> "RedisStreamTask":
        """Create a new task instance with the specified TaskRunner.

        Args:
            runner: The TaskRunner that will execute this task

        Returns:
            RedisStreamTask: New task instance
        """
        return cls(runner)

    @classmethod
    async def destroy(cls) -> None:
        """Destroy all task instances."""
        tasks = list(cls._task_registry.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            if task._execution_task is not None:
                with suppress(asyncio.CancelledError):
                    await task._execution_task
            if task._runner:
                await task._runner.destroy()
        cls._task_registry.clear()
    
    def __repr__(self) -> str:
        """String representation of the task."""
        return f"RedisStreamTask(id={self._id}, done={self.done})"
