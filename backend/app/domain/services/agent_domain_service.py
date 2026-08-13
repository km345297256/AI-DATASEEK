from typing import Optional, AsyncGenerator, List
import asyncio
import logging
import shutil
import tempfile
import uuid
import weakref
from datetime import datetime
from app.domain.models.session import Session, SessionStatus
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.models.event import BaseEvent, ErrorEvent, DoneEvent, MessageEvent, WaitEvent, AgentEvent
from app.domain.utils.public_error import public_error_message
from pydantic import TypeAdapter
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.external.task import Task, TaskInputClosedError
from typing import Type
from app.domain.external.file import FileStorage
from app.domain.external.sandbox_runtime import SandboxNotFoundError, SandboxRuntime
from app.domain.models.file import FileInfo
from app.domain.repositories.mcp_repository import MCPRepository
from app.infrastructure.external.sandbox.sandbox_pool import get_sandbox_pool
from app.infrastructure.external.sandbox.runtime import (
    SandboxCapacityError,
    get_default_sandbox_runtime,
)
from app.core.config import get_settings
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.application.services.dataset_request_resolver import DatasetRequestResolver, FrontControllerResolution
from app.domain.services.lightweight_task_runner import LightweightTaskRunner

# Setup logging
logger = logging.getLogger(__name__)


class _SandboxRetirementError(RuntimeError):
    pass

class AgentDomainService:
    """
    Agent domain service, responsible for coordinating the work of planning agent and execution agent
    """
    
    def __init__(
        self,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        sandbox_cls: Type[Sandbox],
        task_cls: Type[Task],
        file_storage: FileStorage,
        mcp_repository: MCPRepository,
        search_engine: Optional[SearchEngine] = None,
        sandbox_runtime: Optional[SandboxRuntime] = None,
    ):
        self._repository = agent_repository
        self._session_repository = session_repository
        self._sandbox_cls = sandbox_cls
        self._search_engine = search_engine
        self._task_cls = task_cls
        self._file_storage = file_storage
        self._mcp_repository = mcp_repository
        self._sandbox_runtime = sandbox_runtime or get_default_sandbox_runtime(sandbox_cls)
        self._dataset_service = DataCenterDatasetService()
        self._dataset_request_resolver = DatasetRequestResolver()
        self._chat_bootstrap_tasks: set[asyncio.Task] = set()
        self._session_bootstrap_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        logger.info("AgentDomainService initialization completed")
            
    async def shutdown(self) -> None:
        """Clean up all Agent's resources"""
        logger.info("Starting to close all Agents")
        await self._task_cls.destroy()
        logger.info("All agents closed successfully")

    async def _create_task(
        self,
        session: Session,
        dataset_ids: Optional[List[str]] = None,
        front_controller_resolution: Optional[FrontControllerResolution] = None,
    ) -> Task:
        """Create a new agent task"""
        sandbox_runtime = self._sandbox_runtime
        sandbox = None
        sandbox_replaced = False
        sandbox_id = session.sandbox_id
        requested_dataset_ids = list(dict.fromkeys(dataset_ids or []))
        if sandbox_id and set(session.sandbox_dataset_ids) != set(requested_dataset_ids):
            try:
                stale_sandbox = await sandbox_runtime.restore(sandbox_id)
                await self._retire_replaced_sandbox(stale_sandbox, sandbox_id)
            except SandboxNotFoundError:
                logger.info(
                    "Session %s sandbox %s was already gone before dataset remount",
                    session.id,
                    sandbox_id,
                )
            except Exception as exc:
                logger.error("Failed to retire sandbox %s before dataset remount: %s", sandbox_id, exc)
                raise RuntimeError("The previous analysis environment could not be safely released") from exc
            logger.info(
                "Replacing session %s sandbox because dataset mounts changed from %s to %s",
                session.id,
                session.sandbox_dataset_ids,
                requested_dataset_ids,
            )
            session.sandbox_id = None
            session.sandbox_dataset_ids = []
            session.task_id = None
            await self._session_repository.save(session)
            sandbox_id = None
            sandbox_replaced = True
        if sandbox_id:
            try:
                sandbox = await sandbox_runtime.restore(sandbox_id)
                if hasattr(sandbox, "is_paused") and await sandbox.is_paused():
                    logger.info("Session %s sandbox %s is paused; resuming", session.id, sandbox_id)
                    if not await sandbox.resume():
                        logger.warning("Session %s sandbox %s failed to resume; creating a replacement", session.id, sandbox_id)
                        await self._retire_replaced_sandbox(sandbox, sandbox_id)
                        sandbox = None
                    elif not await self._wait_for_resumed_sandbox(sandbox):
                        logger.warning(
                            "Session %s sandbox %s did not become ready after resume; retiring it before replacement",
                            session.id,
                            sandbox_id,
                        )
                        await self._retire_replaced_sandbox(sandbox, sandbox_id)
                        sandbox = None
                elif hasattr(sandbox, "is_available") and not await sandbox.is_available():
                    logger.warning(
                        "Session %s sandbox %s is unavailable; retiring it before replacement",
                        session.id,
                        sandbox_id,
                    )
                    await self._retire_replaced_sandbox(sandbox, sandbox_id)
                    sandbox = None
            except _SandboxRetirementError:
                raise
            except SandboxCapacityError:
                # A paused sandbox that is merely waiting for a capacity slot
                # is still healthy and owned by this session. Never delete it
                # just because the bounded admission queue timed out.
                raise
            except SandboxNotFoundError as exc:
                logger.info(
                    "Session %s sandbox %s no longer exists: %s",
                    session.id,
                    sandbox_id,
                    exc,
                )
                sandbox = None
            except Exception as e:
                logger.warning("Session %s sandbox %s could not be restored: %s", session.id, sandbox_id, e)
                if sandbox is not None:
                    await self._retire_replaced_sandbox(sandbox, sandbox_id)
                    sandbox = None
                else:
                    # Unknown restore errors may be transient node/network
                    # failures. Allocating a replacement here could leak the
                    # still-running original and overcommit capacity.
                    raise RuntimeError(
                        "The previous analysis environment could not be safely restored"
                    ) from e
            sandbox_replaced = sandbox is None
            if sandbox_replaced:
                # Persist the cleared pointer before capacity selection. A retry
                # must never resume the same broken container again.
                session.sandbox_id = None
                session.sandbox_dataset_ids = []
                session.task_id = None
                await self._session_repository.save(session)

        if not sandbox:
            pool = get_sandbox_pool() if not dataset_ids else None
            if pool and pool.enabled:
                sandbox = await pool.acquire()
                # Warm container already has a record; update it with session association
                await sandbox_runtime.assign(sandbox, session)
            else:
                sandbox = (
                    await sandbox_runtime.allocate(session, dataset_ids=dataset_ids)
                    if dataset_ids
                    else await sandbox_runtime.allocate(session)
                )
            session.sandbox_id = sandbox.id
            session.sandbox_dataset_ids = requested_dataset_ids
            await self._session_repository.save(session)
            if sandbox_replaced:
                try:
                    await self._ensure_sandbox_api_ready(sandbox)
                    await self._hydrate_replacement_sandbox(
                        session,
                        sandbox,
                        previous_sandbox_id=sandbox_id,
                    )
                except Exception:
                    await self._retire_replaced_sandbox(sandbox, sandbox.id)
                    session.sandbox_id = None
                    session.sandbox_dataset_ids = []
                    session.task_id = None
                    await self._session_repository.save(session)
                    raise

        browser = await sandbox.get_browser()
        if not browser:
            logger.error(f"Failed to get browser for Sandbox {sandbox_id}")
            raise RuntimeError(f"Failed to get browser for Sandbox {sandbox_id}")

        await self._session_repository.save(session)

        task_runner = AgentTaskRunner(
            session_id=session.id,
            agent_id=session.agent_id,
            user_id=session.user_id,
            sandbox=sandbox,
            browser=browser,
            file_storage=self._file_storage,
            search_engine=self._search_engine,
            session_repository=self._session_repository,
            agent_repository=self._repository,
            mcp_repository=self._mcp_repository,
            llm_overrides=session.llm_overrides,
            front_controller_resolution=front_controller_resolution,
        )

        task = self._task_cls.create(task_runner)
        session.task_id = task.id
        await self._session_repository.save(session)

        # Update record with task_id now that we have it
        await sandbox_runtime.assign(sandbox, session, task.id)

        return task

    async def _create_lightweight_task(self, session: Session, resolution: FrontControllerResolution) -> Task:
        runner = LightweightTaskRunner(
            session_id=session.id,
            user_id=session.user_id,
            resolution=resolution,
            session_repository=self._session_repository,
            file_storage=self._file_storage,
        )
        task = self._task_cls.create(runner)
        session.task_id = task.id
        await self._session_repository.save(session)
        logger.info(
            "Session %s selected %s execution without sandbox allocation",
            session.id,
            resolution.mode,
        )
        return task

    async def _wait_for_resumed_sandbox(self, sandbox: Sandbox) -> bool:
        """Give a resumed container time to wake before declaring it stale."""

        ensure_ready = getattr(sandbox, "ensure_api_ready", None)
        if not callable(ensure_ready):
            ensure_ready = getattr(sandbox, "ensure_sandbox", None)
        if callable(ensure_ready):
            try:
                await asyncio.wait_for(
                    ensure_ready(),
                    timeout=max(1.0, get_settings().sandbox_resume_ready_timeout_seconds),
                )
                return True
            except Exception as exc:
                logger.warning("Resumed sandbox %s did not become ready: %s", sandbox.id, exc)
                return False
        is_available = getattr(sandbox, "is_available", None)
        return bool(await is_available()) if callable(is_available) else True

    async def _ensure_sandbox_api_ready(self, sandbox: Sandbox) -> None:
        ensure_ready = getattr(sandbox, "ensure_api_ready", None)
        if not callable(ensure_ready):
            ensure_ready = getattr(sandbox, "ensure_sandbox", None)
        if callable(ensure_ready):
            await ensure_ready()

    @staticmethod
    async def _retire_replaced_sandbox(sandbox: Sandbox, sandbox_id: str) -> None:
        """Release a broken sandbox without leaking a running capacity slot."""

        destroy = getattr(sandbox, "destroy", None)
        if callable(destroy) and await destroy():
            return
        pause = getattr(sandbox, "pause", None)
        if callable(pause) and await pause():
            logger.warning(
                "Sandbox %s could not be destroyed but was paused before replacement",
                sandbox_id,
            )
            return
        raise _SandboxRetirementError(
            "The previous analysis environment could not be safely released"
        )

    async def _hydrate_replacement_sandbox(
        self,
        session: Session,
        sandbox: Sandbox,
        *,
        previous_sandbox_id: Optional[str],
    ) -> None:
        """Restore persisted files when a session gets a replacement sandbox."""
        restored = 0
        failed = 0
        seen_paths: set[str] = set()
        pending_files: list[FileInfo] = []
        for file_info in session.files:
            if not file_info.file_id or not file_info.file_path or file_info.file_path in seen_paths:
                continue
            seen_paths.add(file_info.file_path)
            pending_files.append(file_info)

        semaphore = asyncio.Semaphore(
            max(1, get_settings().sandbox_hydration_concurrency)
        )

        async def restore_file(file_info: FileInfo) -> bool:
            async with semaphore:
                file_data = None
                staged_file = None
                try:
                    file_data, stored_info = await self._file_storage.download_file(file_info.file_id, session.user_id)
                    # MinIO/urllib3 response reads are synchronous. Stage them
                    # off the event loop and spill larger files to disk instead
                    # of keeping several full artifacts resident in memory.
                    staged_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
                    await asyncio.to_thread(
                        shutil.copyfileobj,
                        file_data,
                        staged_file,
                        1024 * 1024,
                    )
                    await asyncio.to_thread(staged_file.seek, 0)
                    result = await sandbox.file_upload(
                        staged_file,
                        file_info.file_path,
                        filename=stored_info.filename or file_info.filename,
                    )
                    if result.success:
                        return True
                    logger.warning(
                        "Failed to hydrate file %s into replacement sandbox %s: %s",
                        file_info.file_path,
                        sandbox.id,
                        result.message,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to hydrate file %s (%s) for session %s into replacement sandbox %s: %s",
                        file_info.file_path,
                        file_info.file_id,
                        session.id,
                        sandbox.id,
                        exc,
                    )
                finally:
                    if staged_file is not None:
                        await asyncio.to_thread(staged_file.close)
                    if file_data is not None:
                        close = getattr(file_data, "close", None)
                        if callable(close):
                            await asyncio.to_thread(close)
                        release_conn = getattr(file_data, "release_conn", None)
                        if callable(release_conn):
                            await asyncio.to_thread(release_conn)
                return False

        results = await asyncio.gather(
            *(restore_file(file_info) for file_info in pending_files)
        )
        for succeeded in results:
            if succeeded:
                restored += 1
            else:
                failed += 1
        logger.info(
            "Session %s hydrated replacement sandbox %s from previous sandbox %s: restored=%d failed=%d",
            session.id,
            sandbox.id,
            previous_sandbox_id,
            restored,
            failed,
        )
        
    async def _get_task(self, session: Session) -> Optional[Task]:
        """Get a task for the given session"""

        task_id = session.task_id
        if not task_id:
            return None
        
        return self._task_cls.get(task_id)

    async def stop_session(self, session_id: str) -> None:
        """Stop a session"""
        session = await self._session_repository.find_by_id(session_id)
        if not session:
            logger.error(f"Attempted to stop non-existent Session {session_id}")
            raise RuntimeError("Session not found")
        task = await self._get_task(session)
        if task:
            task.cancel()
        await self._session_repository.update_status(session_id, SessionStatus.COMPLETED)

    async def delete_session_resources(self, session: Session) -> None:
        """Stop active work and retire the sandbox before a session is deleted."""
        lock = self._session_bootstrap_locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            latest_session = await self._session_repository.find_by_id(session.id)
            if latest_session is not None:
                session = latest_session

            task = await self._get_task(session)
            if task is not None:
                task.cancel()
                wait_closed = getattr(task, "wait_closed", None)
                if callable(wait_closed):
                    await wait_closed()

            if not session.sandbox_id:
                return

            try:
                sandbox = await self._sandbox_runtime.restore(session.sandbox_id)
                destroyed = await sandbox.destroy()
            except SandboxNotFoundError:
                logger.info(
                    "Session %s sandbox %s was already gone during deletion",
                    session.id,
                    session.sandbox_id,
                )
                return
            if destroyed is False:
                raise RuntimeError("The analysis environment could not be released")

    def _track_chat_bootstrap(self, task: asyncio.Task, session_id: str) -> asyncio.Task:
        self._chat_bootstrap_tasks.add(task)

        def on_done(done_task: asyncio.Task) -> None:
            self._chat_bootstrap_tasks.discard(done_task)
            if done_task.cancelled():
                return
            done_task.exception()

        task.add_done_callback(on_done)
        return task

    async def _handle_chat_bootstrap_error(self, session_id: str, exc: BaseException) -> None:
        logger.exception("Chat bootstrap failed for session %s: %s", session_id, exc)
        try:
            await self._session_repository.add_event(
                session_id,
                ErrorEvent(error=public_error_message(exc)),
            )
            await self._session_repository.update_status(session_id, SessionStatus.COMPLETED)
        except Exception:
            logger.exception("Failed to persist chat bootstrap error for session %s", session_id)

    async def _resume_claimed_chat_task(self, session_id: str, task: Optional[Task]) -> None:
        """Restart a claimed task when its queued message has not started yet."""
        if task is None or not task.done:
            return
        is_empty = getattr(task.input_stream, "is_empty", None)
        if not callable(is_empty) or await is_empty():
            return
        logger.info("Restarting task %s with a previously claimed queued message", task.id)
        await self._session_repository.update_status(session_id, SessionStatus.RUNNING)
        await task.run()

    @staticmethod
    def _client_message_event_id(session_id: str, client_message_id: str) -> str:
        """Return a stable event ID so bootstrap retries upsert one user event."""
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"ai-dataseek:{session_id}:{client_message_id}",
            )
        )

    async def _bootstrap_chat_task(
        self,
        session: Session,
        user_id: str,
        message: str,
        timestamp: Optional[datetime],
        attachments: Optional[List[dict]],
        skills: Optional[List[str]],
        mcp_servers: Optional[List[str]],
        dataset_ids: Optional[List[str]],
        mcp_access_all: bool,
        client_message_id: Optional[str],
    ) -> Optional[Task]:
        """Serialize one session's bootstrap and refresh state inside the lock."""
        lock = self._session_bootstrap_locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            find_session = getattr(
                self._session_repository,
                "find_by_id_and_user_id",
                None,
            )
            if callable(find_session):
                latest_session = await find_session(session.id, user_id)
                if latest_session is None:
                    raise RuntimeError("Session not found")
                session = latest_session
            return await self._bootstrap_chat_task_locked(
                session=session,
                user_id=user_id,
                message=message,
                timestamp=timestamp,
                attachments=attachments,
                skills=skills,
                mcp_servers=mcp_servers,
                dataset_ids=dataset_ids,
                mcp_access_all=mcp_access_all,
                client_message_id=client_message_id,
            )

    async def _bootstrap_chat_task_locked(
        self,
        session: Session,
        user_id: str,
        message: str,
        timestamp: Optional[datetime],
        attachments: Optional[List[dict]],
        skills: Optional[List[str]],
        mcp_servers: Optional[List[str]],
        dataset_ids: Optional[List[str]],
        mcp_access_all: bool,
        client_message_id: Optional[str],
    ) -> Optional[Task]:
        client_message_claimed = False
        queued_event_id: Optional[str] = None
        task: Optional[Task] = None
        try:
            task = await self._get_task(session)
            if client_message_id:
                client_message_claimed = await self._session_repository.claim_client_message_id(
                    session.id,
                    client_message_id,
                )
                if not client_message_claimed:
                    logger.info(
                        "Ignoring duplicate client message %s for session %s",
                        client_message_id,
                        session.id,
                    )
                    if task is not None or session.status != SessionStatus.RUNNING:
                        await self._resume_claimed_chat_task(session.id, task)
                        return task

                    # Claims are persisted, while in-process Task instances are
                    # lost on a backend restart. Reclaim the orphaned claim while
                    # holding the per-session bootstrap lock.
                    logger.warning(
                        "Reclaiming client message %s for session %s after task registry loss",
                        client_message_id,
                        session.id,
                    )
                    await self._session_repository.release_client_message_id(
                        session.id,
                        client_message_id,
                    )
                    client_message_claimed = await self._session_repository.claim_client_message_id(
                        session.id,
                        client_message_id,
                    )
                    if not client_message_claimed:
                        return None

            await self._session_repository.update_status(session.id, SessionStatus.RUNNING)

            effective_dataset_ids = list(dict.fromkeys(dataset_ids or session.dataset_ids or []))
            if not effective_dataset_ids:
                get_events = getattr(self._session_repository, "get_events", None)
                previous_events = await get_events(session.id) if get_events else []
                for previous_event in reversed(previous_events):
                    if not isinstance(previous_event, MessageEvent):
                        continue
                    previous_ids = (previous_event.metadata or {}).get("dataset_ids", [])
                    if previous_ids:
                        effective_dataset_ids = list(dict.fromkeys(previous_ids))
                        break
            if effective_dataset_ids != session.dataset_ids:
                session.dataset_ids = effective_dataset_ids
                await self._session_repository.save(session)

            if task is not None and not getattr(task, "accepting_input", True):
                wait_closed = getattr(task, "wait_closed", None)
                if callable(wait_closed):
                    await wait_closed()
                task = None

            if task is not None and not task.done:
                wait_closed = getattr(task, "wait_closed", None)
                if callable(wait_closed):
                    await wait_closed()
                    task = None

            controller_resolution: FrontControllerResolution | None = None
            if task is None or task.done:
                try:
                    datasets = [
                        await self._dataset_service.get_dataset(dataset_id, user_id=user_id)
                        for dataset_id in effective_dataset_ids
                    ]
                    get_events = getattr(self._session_repository, "get_events", None)
                    conversation_events = await get_events(session.id) if callable(get_events) else []
                    attachment_names = [
                        str(item.get("filename") or item.get("name") or "")
                        for item in (attachments or [])
                        if isinstance(item, dict)
                    ]
                    controller_resolution = await self._dataset_request_resolver.resolve(
                        question=message,
                        datasets=datasets,
                        events=conversation_events,
                        llm_overrides=session.llm_overrides,
                        user_id=user_id,
                        session_id=session.id,
                        selected_skills=skills or [],
                        selected_mcp_servers=mcp_servers or [],
                        attachment_names=attachment_names,
                    )
                except Exception as exc:
                    logger.error(
                        "Session %s Front Controller failed before task creation: %s",
                        session.id,
                        exc,
                    )
                    raise RuntimeError("The request could not be safely classified") from exc

            if task is None or task.done:
                if session.task_id and task is None:
                    logger.warning(
                        "Session %s references missing task %s; creating a new task",
                        session.id,
                        session.task_id,
                    )
                task = (
                    await self._create_lightweight_task(session, controller_resolution)
                    if controller_resolution is not None and (
                        controller_resolution.mode == "reject"
                        or (
                            controller_resolution.mode in {"direct", "catalog"}
                        )
                    )
                    else await self._create_task(
                        session,
                        effective_dataset_ids or None,
                        front_controller_resolution=controller_resolution,
                    )
                )
                if not task:
                    raise RuntimeError("Failed to create task")

            await self._session_repository.update_latest_message(session.id, message, timestamp or datetime.now())

            metadata = {
                "skills": skills or [],
                "mcp_servers": mcp_servers or [],
                "dataset_ids": effective_dataset_ids,
                "mcp_access_all": mcp_access_all,
            }
            if client_message_id:
                metadata["client_message_id"] = client_message_id

            message_event = MessageEvent(
                message=message,
                role="user",
                attachments=await self._resolve_message_attachments(attachments, user_id),
                metadata=metadata,
            )
            if client_message_id:
                message_event.id = self._client_message_event_id(
                    session.id,
                    client_message_id,
                )

            payload = message_event.model_dump_json()
            enqueue_input = getattr(task, "enqueue_input", None)

            if client_message_id:
                # Persist the stable event before making it executable. A retry
                # upserts the same history entry instead of duplicating it.
                await self._session_repository.add_event(session.id, message_event)
            try:
                queued_event_id = (
                    await enqueue_input(payload)
                    if callable(enqueue_input)
                    else await task.input_stream.put(payload)
                )
            except TaskInputClosedError:
                # The previous runner won the atomic close-vs-enqueue race.
                # Wait until its browser cleanup and sandbox pause finish, then
                # create one fresh runner on the same session environment.
                wait_closed = getattr(task, "wait_closed", None)
                if callable(wait_closed):
                    await wait_closed()
                task = (
                    await self._create_lightweight_task(session, controller_resolution)
                    if controller_resolution is not None and (
                        controller_resolution.mode == "reject"
                        or (
                            controller_resolution.mode in {"direct", "catalog"}
                        )
                    )
                    else await self._create_task(
                        session,
                        effective_dataset_ids or None,
                        front_controller_resolution=controller_resolution,
                    )
                )
                enqueue_input = getattr(task, "enqueue_input", None)
                queued_event_id = (
                    await enqueue_input(payload)
                    if callable(enqueue_input)
                    else await task.input_stream.put(payload)
                )
            if not client_message_id:
                message_event.id = queued_event_id
                await self._session_repository.add_event(session.id, message_event)

            await task.run()
            logger.debug("Put message into Session %s's event queue: %s...", session.id, message[:50])
            return task
        except Exception as exc:
            release_claim = queued_event_id is None
            if queued_event_id is not None and task is not None:
                try:
                    release_claim = bool(
                        await task.input_stream.delete_message(queued_event_id)
                    )
                except Exception:
                    # Keep the claim when queue cleanup is uncertain. A retry can
                    # resume the same queued task without enqueueing a duplicate.
                    logger.exception(
                        "Failed to remove queued client message %s from task %s",
                        client_message_id,
                        task.id,
                    )
            if client_message_claimed and release_claim and client_message_id:
                try:
                    await self._session_repository.release_client_message_id(
                        session.id,
                        client_message_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to release client message %s for session %s",
                        client_message_id,
                        session.id,
                    )
            await self._handle_chat_bootstrap_error(session.id, exc)
            raise

    async def chat(
        self,
        session_id: str,
        user_id: str,
        message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        latest_event_id: Optional[str] = None,
        attachments: Optional[List[dict]] = None,
        skills: Optional[List[str]] = None,
        mcp_servers: Optional[List[str]] = None,
        dataset_ids: Optional[List[str]] = None,
        mcp_access_all: bool = False,
        llm_overrides: Optional[dict] = None,
        client_message_id: Optional[str] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        Chat with an agent
        """

        try:
            session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
            if not session:
                logger.error(f"Attempted to chat with non-existent Session {session_id} for user {user_id}")
                raise RuntimeError("Session not found")

            if llm_overrides is not None:
                session.llm_overrides = llm_overrides
                await self._session_repository.save(session)

            task = await self._get_task(session)

            if message:
                bootstrap_task = self._track_chat_bootstrap(
                    asyncio.create_task(
                        self._bootstrap_chat_task(
                            session=session,
                            user_id=user_id,
                            message=message,
                            timestamp=timestamp,
                            attachments=attachments,
                            skills=skills,
                            mcp_servers=mcp_servers,
                            dataset_ids=dataset_ids,
                            mcp_access_all=mcp_access_all,
                            client_message_id=client_message_id,
                        )
                    ),
                    session_id,
                )
                task = await asyncio.shield(bootstrap_task)
            
            logger.info(f"Session {session_id} started")
            logger.debug(f"Session {session_id} task: {task}")
            await self._session_repository.update_unread_message_count(session_id, 0)
           
            while task and not task.done:
                event_id, event_str = await task.output_stream.get(start_id=latest_event_id, block_ms=0)
                latest_event_id = event_id
                if event_str is None:
                    logger.debug(f"No event found in Session {session_id}'s event queue")
                    continue
                event = TypeAdapter(AgentEvent).validate_json(event_str)
                event.id = event_id
                logger.debug(f"Got event from Session {session_id}'s event queue: {type(event).__name__}")
                yield event
                if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                    break
            
            logger.info(f"Session {session_id} completed")

        except asyncio.CancelledError:
            logger.info("Session %s stream disconnected; agent task continues", session_id)
            raise
        except Exception as e:
            logger.exception(f"Error in Session {session_id}")
            event = ErrorEvent(error=public_error_message(e))
            try:
                await self._session_repository.add_event(session_id, event)
            except Exception as persist_error:
                logger.warning("Failed to persist Session %s stream error: %s", session_id, persist_error)
            yield event # TODO: raise api exception

    async def _resolve_message_attachments(
        self,
        attachments: Optional[List[dict]],
        user_id: str,
    ) -> Optional[List[FileInfo]]:
        resolved: List[FileInfo] = []
        for attachment in attachments or []:
            if not attachment or not attachment.get("file_id"):
                continue
            file_info = await self._file_storage.get_file_info(attachment["file_id"], user_id)
            if file_info:
                resolved.append(file_info)
            else:
                resolved.append(
                    FileInfo(
                        file_id=attachment["file_id"],
                        filename=attachment.get("filename", ""),
                    )
                )
        return resolved or None
