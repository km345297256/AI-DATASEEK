from typing import Optional, AsyncGenerator, List
import asyncio
import logging
import re
import shutil
import tempfile
import uuid
import weakref
from datetime import UTC, datetime
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
from app.domain.external.plugin_runtime import PluginRuntime, PluginRuntimeError
from app.domain.external.spill import SpillArtifactStore
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
from app.application.services.jupyter_service import JupyterService
from app.domain.services.lightweight_task_runner import LightweightTaskRunner
from app.domain.services.tools.pipeline import opaque_log_identifier
from app.domain.models.input_admission import AcceptedInput
from app.domain.services.input_delivery import InputDeliveryService, InputLeaseLost
from app.domain.services.analysis_checkpoint import configuration_digest
from app.domain.models.analysis_input import AnalysisInputContext, upload_catalog_views
from app.domain.services.analysis_input_selection import input_snapshot, snapshot_files, select_input_files

# Setup logging
logger = logging.getLogger(__name__)


class _SandboxRetirementError(RuntimeError):
    pass


class ContinuationRejected(ValueError):
    """A stale or changed continuation is terminal, not retryable preparation."""


CONTINUATION_MESSAGE = "继续未完成的分析"

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
        plugin_runtime: Optional[PluginRuntime] = None,
        spill_artifact_store: Optional[SpillArtifactStore] = None,
        analysis_job_service=None,
        tool_approval_service=None,
        credential_service=None,
        input_repository=None,
    ):
        self._repository = agent_repository
        self._session_repository = session_repository
        self._sandbox_cls = sandbox_cls
        self._search_engine = search_engine
        self._task_cls = task_cls
        self._file_storage = file_storage
        self._mcp_repository = mcp_repository
        self._sandbox_runtime = sandbox_runtime or get_default_sandbox_runtime(sandbox_cls)
        self._plugin_runtime = plugin_runtime
        self._spill_artifact_store = spill_artifact_store
        self._analysis_job_service = analysis_job_service
        self._tool_approval_service = tool_approval_service
        self._credential_service = credential_service
        self._input_delivery = InputDeliveryService(input_repository, session_repository) if input_repository is not None else None
        self._input_monitor: asyncio.Task | None = None
        self._input_dispatching: set[tuple[str, str]] = set()
        self._dataset_service = DataCenterDatasetService()
        self._dataset_request_resolver = DatasetRequestResolver()
        self._chat_bootstrap_tasks: set[asyncio.Task] = set()
        self._chat_bootstrap_sessions: dict[asyncio.Task, str] = {}
        self._jupyter_prewarm_tasks: set[asyncio.Task] = set()
        self._session_bootstrap_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        logger.info("AgentDomainService initialization completed")
            
    async def shutdown(self) -> None:
        """Clean up all Agent's resources"""
        logger.info("Starting to close all Agents")
        input_monitor = getattr(self, "_input_monitor", None)
        if input_monitor is not None:
            input_monitor.cancel()
            await asyncio.gather(input_monitor, return_exceptions=True)
            self._input_monitor = None
        bootstrap_tasks = list(self._chat_bootstrap_tasks)
        for task in bootstrap_tasks:
            task.cancel()
        await asyncio.gather(*bootstrap_tasks, return_exceptions=True)

        # No bootstrap can create a new Task after this registry teardown.
        await self._task_cls.destroy()
        prewarm_tasks = list(self._jupyter_prewarm_tasks)
        for task in prewarm_tasks:
            task.cancel()
        await asyncio.gather(*prewarm_tasks, return_exceptions=True)
        logger.info("All agents closed successfully")

    def _prewarm_jupyter(self, session: Session, *, dataset_ids: list[str]) -> None:
        """Warm Jupyter after the session's dataset-bound Sandbox is ready."""
        if not dataset_ids or not session.sandbox_id:
            return

        async def warm() -> None:
            session_ref = opaque_log_identifier(session.id, namespace="session")
            try:
                await JupyterService().prewarm(
                    session_id=session.id,
                    user_id=session.user_id,
                    sandbox_id=session.sandbox_id,
                )
                logger.info("Prewarmed Jupyter session=%s", session_ref)
            except Exception as exc:
                # Prewarming must never delay or fail the analysis request.
                logger.warning(
                    "Jupyter prewarm failed session=%s error_type=%s",
                    session_ref,
                    type(exc).__name__,
                )

        task = asyncio.create_task(
            warm(),
            name=(
                "jupyter-prewarm-"
                f"{opaque_log_identifier(session.id, namespace='session')}"
            ),
        )
        self._jupyter_prewarm_tasks.add(task)
        task.add_done_callback(self._jupyter_prewarm_tasks.discard)

    async def _create_task(
        self,
        session: Session,
        dataset_ids: Optional[List[str]] = None,
        front_controller_resolution: Optional[FrontControllerResolution] = None,
        session_events_snapshot: list | None = None,
    ) -> Task:
        """Create a new agent task"""
        await self._ensure_plugin_runtime_ready()
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
                    "Sandbox already gone before dataset remount session=%s sandbox=%s",
                    opaque_log_identifier(session.id, namespace="session"),
                    opaque_log_identifier(sandbox_id, namespace="sandbox"),
                )
            except Exception as exc:
                logger.error(
                    "Failed to retire sandbox before dataset remount sandbox=%s error_type=%s",
                    opaque_log_identifier(sandbox_id, namespace="sandbox"),
                    type(exc).__name__,
                )
                raise RuntimeError("The previous analysis environment could not be safely released") from exc
            logger.info(
                "Replacing sandbox after dataset mounts changed session=%s old_count=%d new_count=%d",
                opaque_log_identifier(session.id, namespace="session"),
                len(session.sandbox_dataset_ids),
                len(requested_dataset_ids),
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
                    logger.info(
                        "Sandbox is paused; resuming session=%s sandbox=%s",
                        opaque_log_identifier(session.id, namespace="session"),
                        opaque_log_identifier(sandbox_id, namespace="sandbox"),
                    )
                    if not await sandbox.resume():
                        logger.warning(
                            "Sandbox failed to resume; creating replacement session=%s sandbox=%s",
                            opaque_log_identifier(session.id, namespace="session"),
                            opaque_log_identifier(sandbox_id, namespace="sandbox"),
                        )
                        await self._retire_replaced_sandbox(sandbox, sandbox_id)
                        sandbox = None
                    elif not await self._wait_for_resumed_sandbox(sandbox):
                        logger.warning(
                            "Sandbox not ready after resume; retiring before replacement session=%s sandbox=%s",
                            opaque_log_identifier(session.id, namespace="session"),
                            opaque_log_identifier(sandbox_id, namespace="sandbox"),
                        )
                        await self._retire_replaced_sandbox(sandbox, sandbox_id)
                        sandbox = None
                elif hasattr(sandbox, "is_available") and not await sandbox.is_available():
                    logger.warning(
                        "Sandbox unavailable; retiring before replacement session=%s sandbox=%s",
                        opaque_log_identifier(session.id, namespace="session"),
                        opaque_log_identifier(sandbox_id, namespace="sandbox"),
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
            except SandboxNotFoundError:
                logger.info(
                    "Sandbox no longer exists session=%s sandbox=%s",
                    opaque_log_identifier(session.id, namespace="session"),
                    opaque_log_identifier(sandbox_id, namespace="sandbox"),
                )
                sandbox = None
            except Exception as e:
                logger.warning(
                    "Sandbox could not be restored session=%s sandbox=%s error_type=%s",
                    opaque_log_identifier(session.id, namespace="session"),
                    opaque_log_identifier(sandbox_id, namespace="sandbox"),
                    type(e).__name__,
                )
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

        self._prewarm_jupyter(session, dataset_ids=requested_dataset_ids)

        browser = await sandbox.get_browser()
        if not browser:
            logger.error(
                "Failed to get browser sandbox=%s",
                opaque_log_identifier(sandbox_id, namespace="sandbox"),
            )
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
            plugin_runtime=self._plugin_runtime,
            spill_artifact_store=self._spill_artifact_store,
            analysis_job_service=self._analysis_job_service,
            tool_approval_service=self._tool_approval_service,
            credential_service=self._credential_service,
            input_delivery=self._input_delivery,
            session_events_snapshot=session_events_snapshot,
            effective_dataset_ids=requested_dataset_ids,
        )

        task = self._task_cls.create(task_runner)
        return await self._finish_task_preparation(session, task, sandbox=sandbox)

    async def _finish_task_preparation(
        self, session: Session, task: Task, *, sandbox: Sandbox | None = None,
    ) -> Task:
        """Own a registered, unstarted task until all preparation has settled.

        Cancellation must not let an in-flight database write publish the task
        *after* rollback. Shield preparation, then await it before rolling back.
        The task never starts here; the dispatcher owns enqueue/run after this
        method returns successfully.
        """
        previous_task_id = session.task_id

        async def prepare() -> None:
            published = await self._session_repository.compare_and_set_task_id(
                session.id, expected_task_id=previous_task_id, task_id=task.id,
                dataset_ids=session.dataset_ids,
            )
            if not published:
                raise TaskInputClosedError("Session task changed during preparation")
            session.task_id = task.id
            if sandbox is not None:
                if not getattr(task, "accepting_input", True):
                    raise TaskInputClosedError("Task closed during preparation")
                await self._sandbox_runtime.assign(sandbox, session, task.id)

        preparation = asyncio.create_task(prepare())
        try:
            await asyncio.shield(preparation)
            return task
        except BaseException:
            # Synchronous cancel immediately removes unstarted Redis tasks
            # from their registry and closes input admission.
            task.cancel()

            async def rollback() -> None:
                try:
                    await preparation
                except BaseException:
                    # Preserve the original preparation/cancellation failure.
                    pass
                wait_closed = getattr(task, "wait_closed", None)
                if callable(wait_closed):
                    await wait_closed()
                try:
                    await self._session_repository.compare_and_set_task_id(
                        session.id, expected_task_id=task.id, task_id=previous_task_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not roll back prepared task pointer session=%s task=%s error_type=%s",
                        opaque_log_identifier(session.id, namespace="session"),
                        opaque_log_identifier(task.id, namespace="task"),
                        type(exc).__name__,
                    )
                finally:
                    if session.task_id == task.id:
                        session.task_id = previous_task_id
                # Do not call runner.destroy/on_done: they destroy or pause
                # the session-owned sandbox and close existing browser pages.
                # No runner resources have been started yet. Sandbox assign
                # is a non-authoritative observability link, not execution
                # ownership; without a task-scoped CAS unassign, clearing it
                # could undo another task's association. The next assignment
                # replaces it while the reusable sandbox/artifacts survive.

            cleanup = asyncio.create_task(rollback())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated caller cancellation must not detach cleanup.
                    continue
            cleanup.result()
            raise

    async def _ensure_plugin_runtime_ready(self) -> None:
        """Recover a crashed Cordis child before capturing a task snapshot.

        Startup remains fail-closed: if the catalog cannot be recovered, the
        existing Agent flow is still created with zero Cordis tools.
        """
        runtime = self._plugin_runtime
        if runtime is None or runtime.healthy:
            return
        try:
            await runtime.start()
        except PluginRuntimeError as exc:
            logger.warning(
                "Cordis plugin runtime is unavailable for this task error_type=%s",
                type(exc).__name__,
            )

    async def _create_lightweight_task(self, session: Session, resolution: FrontControllerResolution) -> Task:
        runner = LightweightTaskRunner(
            session_id=session.id,
            user_id=session.user_id,
            resolution=resolution,
            session_repository=self._session_repository,
            file_storage=self._file_storage,
            llm_overrides=session.llm_overrides,
            input_delivery=self._input_delivery,
        )
        task = self._task_cls.create(runner)
        await self._finish_task_preparation(session, task)
        logger.info(
            "Selected execution without sandbox allocation session=%s mode=%s",
            opaque_log_identifier(session.id, namespace="session"),
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
                logger.warning(
                    "Resumed sandbox did not become ready sandbox=%s error_type=%s",
                    opaque_log_identifier(sandbox.id, namespace="sandbox"),
                    type(exc).__name__,
                )
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
                "Sandbox could not be destroyed but was paused before replacement sandbox=%s",
                opaque_log_identifier(sandbox_id, namespace="sandbox"),
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
                        "Failed to hydrate file into replacement sandbox file=%s sandbox=%s message_chars=%d",
                        opaque_log_identifier(file_info.file_path, namespace="file"),
                        opaque_log_identifier(sandbox.id, namespace="sandbox"),
                        len(str(result.message or "")),
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to hydrate file into replacement sandbox file=%s object=%s session=%s sandbox=%s error_type=%s",
                        opaque_log_identifier(file_info.file_path, namespace="file"),
                        opaque_log_identifier(file_info.file_id, namespace="object"),
                        opaque_log_identifier(session.id, namespace="session"),
                        opaque_log_identifier(sandbox.id, namespace="sandbox"),
                        type(exc).__name__,
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
            "Hydrated replacement sandbox session=%s sandbox=%s previous=%s restored=%d failed=%d",
            opaque_log_identifier(session.id, namespace="session"),
            opaque_log_identifier(sandbox.id, namespace="sandbox"),
            opaque_log_identifier(previous_sandbox_id, namespace="sandbox"),
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
            logger.error(
                "Attempted to stop non-existent session=%s",
                opaque_log_identifier(session_id, namespace="session"),
            )
            raise RuntimeError("Session not found")
        task = await self._get_task(session)
        for bootstrap, owner_session in tuple(getattr(self, "_chat_bootstrap_sessions", {}).items()):
            if owner_session == session_id and bootstrap is not asyncio.current_task():
                bootstrap.cancel()
        if self._input_delivery is not None:
            await self._input_delivery.cancel_session(session_id)
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

            if self._input_delivery is not None:
                await self._input_delivery.repository.disable_session(session.id)
                await self._input_delivery.cancel_session(session.id)

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
                    "Sandbox already gone during deletion session=%s sandbox=%s",
                    opaque_log_identifier(session.id, namespace="session"),
                    opaque_log_identifier(session.sandbox_id, namespace="sandbox"),
                )
                return
            if destroyed is False:
                raise RuntimeError("The analysis environment could not be released")

    def _track_chat_bootstrap(self, task: asyncio.Task, session_id: str) -> asyncio.Task:
        self._chat_bootstrap_tasks.add(task)
        sessions = getattr(self, "_chat_bootstrap_sessions", None)
        if sessions is None:
            sessions = self._chat_bootstrap_sessions = {}
        sessions[task] = session_id

        def on_done(done_task: asyncio.Task) -> None:
            self._chat_bootstrap_tasks.discard(done_task)
            self._chat_bootstrap_sessions.pop(done_task, None)
            if done_task.cancelled():
                return
            done_task.exception()

        task.add_done_callback(on_done)
        return task

    async def _handle_chat_bootstrap_error(self, session_id: str, exc: BaseException) -> None:
        session_ref = opaque_log_identifier(session_id, namespace="session")
        logger.error(
            "Chat bootstrap failed session=%s error_type=%s",
            session_ref,
            type(exc).__name__,
        )
        try:
            await self._session_repository.add_event(
                session_id,
                ErrorEvent(error=public_error_message(exc)),
            )
            await self._session_repository.update_status(session_id, SessionStatus.COMPLETED)
        except Exception as persist_error:
            logger.error(
                "Failed to persist chat bootstrap error session=%s error_type=%s",
                session_ref,
                type(persist_error).__name__,
            )

    async def _resume_claimed_chat_task(self, session_id: str, task: Optional[Task]) -> None:
        """Restart a claimed task when its queued message has not started yet."""
        if task is None or not task.done:
            return
        is_empty = getattr(task.input_stream, "is_empty", None)
        if not callable(is_empty) or await is_empty():
            return
        logger.info(
            "Restarting task with previously claimed queued message task=%s session=%s",
            opaque_log_identifier(task.id, namespace="task"),
            opaque_log_identifier(session_id, namespace="session"),
        )
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
        resume_from: Optional[str] = None,
        input_file_ids: Optional[List[str]] = None,
    ) -> Optional[Task]:
        """Serialize one session's bootstrap and refresh state inside the lock."""
        input_generation = None
        delivery = getattr(self, "_input_delivery", None)
        get_generation = getattr(delivery.repository, "generation", None) if delivery is not None else None
        if callable(get_generation):
            input_generation = await get_generation(session.id)
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
                resume_from=resume_from,
                input_generation=input_generation,
                input_file_ids=input_file_ids,
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
        input_generation: int | None = None,
        resume_from: Optional[str] = None,
        input_file_ids: Optional[List[str]] = None,
    ) -> Optional[Task]:
        if getattr(self, "_input_delivery", None) is not None:
            return await self._bootstrap_durable_input(session, user_id, message, timestamp, attachments,
                skills, mcp_servers, dataset_ids, mcp_access_all, client_message_id, input_generation, resume_from, input_file_ids)
        if resume_from:
            raise ContinuationRejected("当前服务不支持安全续作，请检查原任务。")
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
                        "Ignoring duplicate client message=%s session=%s",
                        opaque_log_identifier(client_message_id, namespace="message"),
                        opaque_log_identifier(session.id, namespace="session"),
                    )
                    if task is not None or session.status != SessionStatus.RUNNING:
                        await self._resume_claimed_chat_task(session.id, task)
                        return task

                    # Claims are persisted, while in-process Task instances are
                    # lost on a backend restart. Reclaim the orphaned claim while
                    # holding the per-session bootstrap lock.
                    logger.warning(
                        "Reclaiming client message=%s session=%s after task registry loss",
                        opaque_log_identifier(client_message_id, namespace="message"),
                        opaque_log_identifier(session.id, namespace="session"),
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
                        "Front Controller failed before task creation session=%s error_type=%s",
                        opaque_log_identifier(session.id, namespace="session"),
                        type(exc).__name__,
                    )
                    raise RuntimeError("The request could not be safely classified") from exc

            if task is None or task.done:
                if session.task_id and task is None:
                    logger.warning(
                        "Session references missing task; creating a new task session=%s task=%s",
                        opaque_log_identifier(session.id, namespace="session"),
                        opaque_log_identifier(session.task_id, namespace="task"),
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

            # Seal the producer identity before Redis assigns its independent
            # transport cursor. The private identity is never serialized.
            message_event.bind_producer_event_id()

            reserve_sequence = getattr(
                self._session_repository,
                "reserve_event_sequence",
                None,
            )
            if callable(reserve_sequence):
                # Input events use the same session sequence as output events.
                # Reserve before serializing so every Redis payload carries the
                # exact sequence later written to history.
                await reserve_sequence(session.id, message_event)
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
            logger.debug(
                "Queued user message session=%s chars=%d",
                opaque_log_identifier(session.id, namespace="session"),
                len(message),
            )
            return task
        except Exception as exc:
            release_claim = queued_event_id is None
            if queued_event_id is not None and task is not None:
                try:
                    release_claim = bool(
                        await task.input_stream.delete_message(queued_event_id)
                    )
                except Exception as cleanup_error:
                    # Keep the claim when queue cleanup is uncertain. A retry can
                    # resume the same queued task without enqueueing a duplicate.
                    logger.error(
                        "Failed to remove queued client message=%s task=%s error_type=%s",
                        opaque_log_identifier(client_message_id, namespace="message"),
                        opaque_log_identifier(task.id, namespace="task"),
                        type(cleanup_error).__name__,
                    )
            if client_message_claimed and release_claim and client_message_id:
                try:
                    await self._session_repository.release_client_message_id(
                        session.id,
                        client_message_id,
                    )
                except Exception as release_error:
                    logger.error(
                        "Failed to release client message=%s session=%s error_type=%s",
                        opaque_log_identifier(client_message_id, namespace="message"),
                        opaque_log_identifier(session.id, namespace="session"),
                        type(release_error).__name__,
                    )
            await self._handle_chat_bootstrap_error(session.id, exc)
            raise

    def start_input_recovery(self) -> None:
        if self._input_delivery is None or self._input_monitor is not None:
            return

        async def monitor() -> None:
            while True:
                try:
                    await self._input_delivery.maintain(self._schedule_accepted_input)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning("Input recovery failed error_type=%s", type(error).__name__)
                await asyncio.sleep(5)

        self._input_monitor = asyncio.create_task(monitor(), name="accepted-input-recovery")

    async def _schedule_accepted_input(self, record: AcceptedInput) -> None:
        identity = (record.session_id, record.key)
        if identity in self._input_dispatching or len(self._input_dispatching) >= 20:
            return
        self._input_dispatching.add(identity)

        async def dispatch() -> None:
            try:
                lock = self._session_bootstrap_locks.setdefault(record.session_id, asyncio.Lock())
                async with lock:
                    current = await self._input_delivery.repository.get(*identity)
                    if current is None or current.admission.state != "pending":
                        return
                    claimed = await self._input_delivery.claim(current)
                    if claimed is not None:
                        await self._dispatch_claimed_input(claimed)
            except InputLeaseLost:
                return
            except Exception as error:
                logger.warning("Accepted input preparation failed error_type=%s", type(error).__name__)
            finally:
                self._input_dispatching.discard(identity)

        self._track_chat_bootstrap(asyncio.create_task(dispatch()), record.session_id)

    @staticmethod
    def _validate_continuation_payload(resume_from, message, attachments, skills,
                                       mcp_servers, dataset_ids, mcp_access_all, client_message_id):
        if not resume_from:
            return
        if (not isinstance(resume_from, str) or re.fullmatch(r"[0-9a-f]{32}", resume_from) is None
                or not isinstance(client_message_id, str) or not client_message_id.strip()
                or len(client_message_id) > 128):
            raise ContinuationRejected("续作标识无效，请从原任务的续作入口重试。")
        # mcp_access_all is derived from the authenticated server identity, not
        # from ChatRequest. Compare it to the checkpoint at admission below.
        if ((message or "").strip() or attachments or skills or mcp_servers or dataset_ids):
            raise ContinuationRejected("续作不能更改原任务、数据范围或执行配置。")

    async def _resume_checkpoint(self, session, user_id, resume_from, client_message_id,
                                 *, history=None, event=None):
        getter = getattr(self._session_repository, "get_analysis_checkpoint", None)
        if not callable(getter):
            raise ContinuationRejected("当前服务不支持安全续作，请检查原任务。")
        checkpoint = await getter(session.id, resume_from)
        expiry = checkpoint.get("expires_at") if isinstance(checkpoint, dict) else None
        if isinstance(expiry, datetime) and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if (not isinstance(checkpoint, dict) or checkpoint.get("id") != resume_from
                or checkpoint.get("version") != 1 or checkpoint.get("owner_id") != user_id
                or session.user_id != user_id or not isinstance(expiry, datetime) or expiry <= datetime.now(UTC)
                or checkpoint.get("configuration_digest") != configuration_digest(session)
                or not session.sandbox_id or checkpoint.get("sandbox_id") != session.sandbox_id
                or (checkpoint.get("attachment_file_ids") and not checkpoint.get("analysis_input_manifest"))
                or not isinstance(checkpoint.get("goal"), str) or not checkpoint["goal"].strip()):
            raise ContinuationRejected("续作进度已失效，或任务配置已变化；系统未重新执行。")
        if checkpoint.get("claimed_by") not in (None, client_message_id):
            raise ContinuationRejected("该续作请求已提交，请查看原请求的执行进度。")
        if event is not None and checkpoint.get("claimed_by") != client_message_id:
            raise ContinuationRejected("续作请求的领取记录不匹配，系统未重新执行。")
        for field in ("dataset_ids", "skills", "mcp_servers", "target_files"):
            values = checkpoint.get(field)
            if (not isinstance(values, list) or len(values) > 64
                    or any(not isinstance(value, str) or not value or len(value) > 4096 for value in values)
                    or len(set(values)) != len(values)):
                raise ContinuationRejected("续作进度的数据范围记录不完整，系统未重新执行。")
        if not checkpoint["dataset_ids"] and not checkpoint.get("analysis_input_manifest"):
            raise ContinuationRejected("该任务没有可验证的数据集来源，不能安全续作。")
        source_seq = checkpoint.get("source_seq")
        if type(source_seq) is not int or source_seq < 1:
            raise ContinuationRejected("续作进度缺少可靠的原请求记录。")
        history = history if history is not None else await self._session_repository.get_events(session.id)
        user_sequences = [item.seq for item in history if isinstance(item, MessageEvent)
                          and item.role == "user" and type(item.seq) is int]
        if event is None:
            current_source_seq = max(user_sequences, default=0)
        else:
            if type(event.seq) is not int or max(user_sequences, default=0) != event.seq:
                raise ContinuationRejected("会话已有更新的请求，原续作已失效。")
            current_source_seq = max((seq for seq in user_sequences if seq < event.seq), default=0)
        if current_source_seq != source_seq:
            raise ContinuationRejected("会话已有新的分析请求，原续作已失效。")
        current_check = getattr(self._session_repository, "is_analysis_checkpoint_current", None)
        if event is not None and callable(current_check):
            if not await current_check(session.id, resume_from, user_id, client_message_id,
                                       source_seq=source_seq, resume_event_seq=event.seq):
                raise ContinuationRejected("续作进度已被新的输入取代，系统未重新执行。")
        return checkpoint

    async def _reject_claimed_continuation(self, record, reason):
        """Persist a terminal for immutable-request failures, never reschedule them."""
        event = ErrorEvent(error=str(reason))
        await self._input_delivery.prepare_event(record.session_id, record.key, event)
        await self._session_repository.add_event(record.session_id, event)
        await self._input_delivery.complete(record.session_id, record.key, event)
        await self._session_repository.update_status(record.session_id, SessionStatus.COMPLETED)

    async def _bootstrap_durable_input(self, session, user_id, message, timestamp, attachments,
                                       skills, mcp_servers, dataset_ids, mcp_access_all, client_message_id,
                                       input_generation=None, resume_from=None, input_file_ids=None):
        if resume_from and input_file_ids is not None:
            raise ContinuationRejected("续作不能修改原任务的资料范围。")
        self._validate_continuation_payload(resume_from, message, attachments, skills, mcp_servers,
                                            dataset_ids, mcp_access_all, client_message_id)
        if client_message_id:
            from app.domain.models.input_admission import input_key
            identity = input_key(MessageEvent(id=self._client_message_event_id(session.id, client_message_id),
                                              role="user", message=message))
            existing = await self._input_delivery.repository.get(session.id, identity)
            if existing is not None:
                previous_metadata = existing.event.metadata or {}
                if resume_from or previous_metadata.get("resume_from"):
                    if (not resume_from or previous_metadata.get("resume_from") != resume_from
                            or existing.admission.actor_user_id != user_id):
                        raise ContinuationRejected("消息标识已用于其他请求，不能重复执行续作。")
                    if existing.admission.state == "pending":
                        await self._schedule_accepted_input(existing)
                    return self._task_cls.get(existing.admission.task_id) if existing.admission.task_id else None
                requested_metadata = {"skills": skills or [], "mcp_servers": mcp_servers or [],
                    "dataset_ids": list(dict.fromkeys(dataset_ids)) if dataset_ids is not None else previous_metadata.get("dataset_ids", []),
                    "mcp_access_all": mcp_access_all}
                if "requested_input_file_ids" in previous_metadata:
                    requested_metadata["requested_input_file_ids"] = input_file_ids
                previous_files = [item.file_id for item in existing.event.attachments or []]
                requested_files = [item["file_id"] for item in attachments or [] if isinstance(item, dict) and item.get("file_id")]
                if (existing.admission.actor_user_id != user_id or existing.event.message != message
                        or previous_files != requested_files
                        or any(previous_metadata.get(key, False if key == "mcp_access_all" else []) != value
                               for key, value in requested_metadata.items())):
                    raise ValueError("Client message identity was reused with different input")
                # A reconnect attaches immediately, even while the original
                # task is still running. Preparation/recovery is independent.
                if existing.admission.state == "pending":
                    await self._schedule_accepted_input(existing)
                return self._task_cls.get(existing.admission.task_id) if existing.admission.task_id else None
        # Keep the current sequential conversation contract: accepting another
        # turn waits for the prior runner's cleanup before exposing its event.
        task = await self._get_task(session)
        if task is not None and (not task.done or not getattr(task, "accepting_input", True)):
            await task.wait_closed()
            latest = await self._session_repository.find_by_id_and_user_id(session.id, user_id)
            if latest is None:
                raise RuntimeError("Session not found")
            session = latest
        checkpoint = None
        if resume_from:
            checkpoint = await self._resume_checkpoint(session, user_id, resume_from, client_message_id)
            if checkpoint.get("mcp_access_all") is not mcp_access_all:
                raise ContinuationRejected("当前执行权限与原任务不同，不能安全续作。")
            claim = getattr(self._session_repository, "claim_analysis_checkpoint", None)
            if not callable(claim):
                raise ContinuationRejected("当前服务不支持安全续作，请检查原任务。")
            checkpoint = await claim(session.id, resume_from, user_id, client_message_id,
                                     expected_source_seq=checkpoint["source_seq"])
            if not checkpoint:
                raise ContinuationRejected("该续作进度已被领取或失效，请查看原请求。")
            message = CONTINUATION_MESSAGE
            dataset_ids, skills, mcp_servers = (list(checkpoint[field]) for field in ("dataset_ids", "skills", "mcp_servers"))
            mcp_access_all = bool(checkpoint.get("mcp_access_all", False))
        selected_inputs, submitted_inputs = await self._prepare_input_selection(
            session.id, user_id, attachments, input_file_ids,
            checkpoint=checkpoint,
        )
        effective_ids = list(dict.fromkeys(dataset_ids or session.dataset_ids or []))
        metadata = {"skills": skills or [], "mcp_servers": mcp_servers or [],
                    "dataset_ids": effective_ids, "mcp_access_all": mcp_access_all,
                    "requested_input_file_ids": input_file_ids,
                    "analysis_input_file_ids": [item.file_id for item in selected_inputs],
                    "analysis_input_files": input_snapshot(selected_inputs)}
        if client_message_id:
            metadata["client_message_id"] = client_message_id
        if resume_from:
            metadata["resume_from"] = resume_from
        event = MessageEvent(message=message, role="user", metadata=metadata,
            attachments=submitted_inputs or None)
        if timestamp is not None:
            event.timestamp = timestamp
        if client_message_id:
            event.id = self._client_message_event_id(session.id, client_message_id)
        admission_options = {"generation": input_generation} if input_generation is not None else {}
        record = await self._input_delivery.repository.accept(session.id, user_id, event, **admission_options)
        if record is None:
            # An event from before durable admission has unknown execution
            # status. Never turn this upgrade into automatic tool replay.
            return await self._get_task(session)
        if not resume_from:
            clear_checkpoint = getattr(self._session_repository, "clear_analysis_checkpoint", None)
            if callable(clear_checkpoint):
                await clear_checkpoint(session.id)
        if record.admission.state != "pending":
            return self._task_cls.get(record.admission.task_id) if record.admission.task_id else None
        try:
            await self._session_repository.update_latest_message(session.id, message, event.timestamp)
            claimed = await self._input_delivery.claim(record)
            if claimed is None:
                return await self._get_task(session)
            return await self._dispatch_claimed_input(claimed)
        except InputLeaseLost:
            return None
        except Exception as error:
            # Acceptance has committed. Recovery owns transient preparation
            # failures; publishing a business Error here would falsely close
            # the UI while the same accepted request remains scheduled.
            logger.warning("Accepted input preparation deferred error_type=%s", type(error).__name__)
            return None

    async def _dispatch_claimed_input(self, record: AcceptedInput) -> Optional[Task]:
        task = None
        try:
            session = await self._session_repository.find_by_id_and_user_id(
                record.session_id, record.admission.actor_user_id)
            if session is None:
                raise RuntimeError("Session not found")
            previous = await self._get_task(session)
            if previous is not None and (not previous.done or not getattr(previous, "accepting_input", True)):
                await previous.wait_closed()
            await self._input_delivery._require_live(record.session_id, record.key, states={"claimed"})
            event = record.event
            metadata = event.metadata or {}
            ids = list(metadata.get("dataset_ids") or [])
            # Continuation admission retains its full canonical-history checks.
            # Ordinary inputs use the private incremental view; this is not an
            # authorization source and must never include later queued inputs.
            from app.domain.services.execution_history import ExecutionHistory
            history_getter = getattr(self._session_repository, "get_execution_history", None)
            projection = None
            if not metadata.get("resume_from") and callable(history_getter) and type(event.seq) is int:
                candidate = await history_getter(record.session_id, before_seq=event.seq)
                if isinstance(candidate, ExecutionHistory):
                    projection = candidate
            history = (projection.resolver_events() if projection is not None
                       else await self._session_repository.get_events(record.session_id))
            checkpoint = None
            if metadata.get("resume_from"):
                checkpoint = await self._resume_checkpoint(
                    session, record.admission.actor_user_id, metadata["resume_from"],
                    metadata.get("client_message_id"), history=history, event=event,
                )
                if (ids != checkpoint["dataset_ids"]
                        or metadata.get("skills", []) != checkpoint["skills"]
                        or metadata.get("mcp_servers", []) != checkpoint["mcp_servers"]
                        or bool(metadata.get("mcp_access_all", False)) != bool(checkpoint.get("mcp_access_all", False))
                        or event.attachments):
                    raise ContinuationRejected("续作请求的数据范围或执行配置已变化。")
            # The newly accepted input is not prior conversational context.
            if projection is not None:
                snapshot = projection.with_event(event)
            else:
                snapshot = [item for item in history if item.seq is None or item.seq <= event.seq]
                history = [item for item in snapshot if item.seq is None or item.seq < event.seq]
            if not ids and checkpoint is None:
                ids = (list(projection.latest_dataset_ids) if projection is not None else
                       next((list(dict.fromkeys((item.metadata or {}).get("dataset_ids", [])))
                             for item in reversed(history) if isinstance(item, MessageEvent)
                             and (item.metadata or {}).get("dataset_ids")), []))
            try:
                datasets = [await self._dataset_service.get_dataset(item, user_id=record.admission.actor_user_id) for item in ids]
            except Exception as exc:
                if checkpoint is not None:
                    raise ContinuationRejected("原任务的数据来源不可用或访问权限已变化，不能续作。") from exc
                raise
            resolution = await self._dataset_request_resolver.resolve(
                question=checkpoint["goal"] if checkpoint is not None else event.message,
                datasets=datasets + upload_catalog_views(snapshot_files(metadata)),
                events=history, llm_overrides=session.llm_overrides, user_id=record.admission.actor_user_id,
                session_id=session.id, selected_skills=metadata.get("skills") or [],
                selected_mcp_servers=metadata.get("mcp_servers") or [],
                attachment_names=[item.filename for item in snapshot_files(metadata)])
            await self._input_delivery._require_live(record.session_id, record.key, states={"claimed"})
            if checkpoint is not None:
                current_session = await self._session_repository.find_by_id_and_user_id(session.id, record.admission.actor_user_id)
                if current_session is None:
                    raise ContinuationRejected("续作会话已不可用。")
                await self._resume_checkpoint(current_session, record.admission.actor_user_id,
                    metadata["resume_from"], metadata.get("client_message_id"), event=event)
                if resolution.mode != "reject":
                    if resolution.mode != "sandbox":
                        raise ContinuationRejected("前置决策未确认原执行范围，系统未重新执行；请检查原任务。")
                    original_targets = list(checkpoint["target_files"])
                    if resolution.target_files and set(resolution.target_files) != set(original_targets):
                        raise ContinuationRejected("前置决策的数据范围与原任务不一致，系统未重新执行。")
                    # An omitted advisory selection cannot widen a continuation.
                    # The registered original selection is authoritative.
                    resolution.target_files = original_targets
                    resolution.decision.execution.target_files = original_targets
            session.dataset_ids = ids
            await self._session_repository.update_status(session.id, SessionStatus.RUNNING)
            task = (await self._create_lightweight_task(session, resolution)
                    if resolution.mode in {"reject", "direct", "catalog"}
                    else await self._create_task(session, ids or None, front_controller_resolution=resolution,
                                                 session_events_snapshot=snapshot))
            await self._input_delivery.bind(record, task)
            await task.enqueue_input(event.model_dump_json())
            await task.run()
            return task
        except ContinuationRejected as error:
            if task is not None:
                task.cancel()
            await self._reject_claimed_continuation(record, error)
            return None
        except BaseException:
            if task is not None:
                task.cancel()
            await self._input_delivery.retry_preparation(record)
            raise

    async def chat(
        self,
        session_id: str,
        user_id: str,
        message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        latest_event_id: Optional[str] = None,
        latest_event_seq: Optional[int] = None,
        attachments: Optional[List[dict]] = None,
        skills: Optional[List[str]] = None,
        mcp_servers: Optional[List[str]] = None,
        dataset_ids: Optional[List[str]] = None,
        mcp_access_all: bool = False,
        llm_overrides: Optional[dict] = None,
        client_message_id: Optional[str] = None,
        resume_from: Optional[str] = None,
        input_file_ids: Optional[List[str]] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """
        Chat with an agent
        """

        try:
            if resume_from and input_file_ids is not None:
                raise ContinuationRejected("续作不能修改原任务的资料范围。")
            self._validate_continuation_payload(resume_from, message, attachments, skills, mcp_servers,
                                                dataset_ids, mcp_access_all, client_message_id)
            if resume_from and llm_overrides is not None:
                raise ContinuationRejected("续作不能改变原任务的模型或执行配置。")
            session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
            if not session:
                logger.error(
                    "Attempted to chat with non-existent session=%s user=%s",
                    opaque_log_identifier(session_id, namespace="session"),
                    opaque_log_identifier(user_id, namespace="user"),
                )
                raise RuntimeError("Session not found")

            if llm_overrides is not None:
                session.llm_overrides = llm_overrides
                await self._session_repository.save(session)

            task = await self._get_task(session)

            if message or resume_from:
                bootstrap_task = self._track_chat_bootstrap(
                    asyncio.create_task(
                        self._bootstrap_chat_task(
                            session=session,
                            user_id=user_id,
                            message=message or "",
                            timestamp=timestamp,
                            attachments=attachments,
                            skills=skills,
                            mcp_servers=mcp_servers,
                            dataset_ids=dataset_ids,
                            mcp_access_all=mcp_access_all,
                            client_message_id=client_message_id,
                            resume_from=resume_from,
                            input_file_ids=input_file_ids,
                        )
                    ),
                    session_id,
                )
                task = await asyncio.shield(bootstrap_task)

            if latest_event_seq is None and latest_event_id not in {None, "", "0", "0-0", "$"}:
                resolve_seq = getattr(self._session_repository, "resolve_event_sequence", None)
                if callable(resolve_seq):
                    latest_event_seq = await resolve_seq(session_id, latest_event_id)

            stream_input_seq = None
            stream_input_key = None
            delivery = getattr(self, "_input_delivery", None)
            if delivery is not None:
                current_input = await delivery.repository.latest(session_id)
                if current_input is not None:
                    stream_input_seq = current_input.event.seq
                    stream_input_key = current_input.key
            
            logger.info(
                "Session stream started session=%s",
                opaque_log_identifier(session_id, namespace="session"),
            )
            logger.debug(
                "Session task selected session=%s task=%s",
                opaque_log_identifier(session_id, namespace="session"),
                opaque_log_identifier(getattr(task, "id", None), namespace="task"),
            )
            await self._session_repository.update_unread_message_count(session_id, 0)

            # Versioned clients resume from durable history by sequence first,
            # then attach to the current Redis task stream.  Legacy clients that
            # only send a Redis ``event_id`` retain the previous behavior.
            last_seen_seq = max(0, latest_event_seq) if latest_event_seq is not None else None
            replay_terminal_seq: Optional[int] = None
            if last_seen_seq is not None:
                get_events_after = getattr(
                    self._session_repository,
                    "get_events_after",
                    None,
                )
                if callable(get_events_after):
                    replay_events = await get_events_after(session_id, last_seen_seq)
                else:
                    get_events = getattr(self._session_repository, "get_events", None)
                    historical_events = await get_events(session_id) if callable(get_events) else []
                    replay_events = [
                        item
                        for item in historical_events
                        if item.seq is not None and item.seq > last_seen_seq
                    ]
                for replay_event in replay_events:
                    event_seq = replay_event.seq
                    if event_seq is None or event_seq <= last_seen_seq:
                        continue
                    last_seen_seq = event_seq
                    # User messages have always been rendered optimistically and
                    # were never emitted on the output SSE stream.  Advancing the
                    # cursor without yielding preserves that UI contract.
                    if isinstance(replay_event, MessageEvent) and replay_event.role == "user":
                        continue
                    if (isinstance(replay_event, (DoneEvent, ErrorEvent, WaitEvent))
                            and not await self._terminal_matches_input(session_id, stream_input_key, stream_input_seq, replay_event)):
                        continue
                    yield replay_event
                    replay_terminal_seq = (
                        event_seq
                        if isinstance(replay_event, (DoneEvent, ErrorEvent, WaitEvent))
                        else None
                    )

                # A terminal event closes the stream even if the runner has not
                # yet completed its final cleanup.  A later user event has a
                # greater sequence and clears this condition for the next turn.
                if replay_terminal_seq == last_seen_seq:
                    return

            redis_start_id = "0-0" if last_seen_seq is not None else latest_event_id
            stream_terminal = False
            while task:
                # A task can finish in the narrow window between the durable
                # history query above and attaching to Redis. Always drain an
                # already-finished stream non-blockingly before returning.
                # While it is running, use a bounded block so a task that exits
                # without publishing cannot leave the SSE request hung forever.
                task_was_done = task.done
                try:
                    event_id, event_str = await task.output_stream.get(
                        start_id=redis_start_id,
                        block_ms=None if task_was_done else 1000,
                    )
                except Exception as error:
                    if getattr(self, "_input_delivery", None) is None:
                        raise
                    logger.warning("Live input stream unavailable; using durable events error_type=%s", type(error).__name__)
                    break
                latest_event_id = event_id
                if event_str is None:
                    logger.debug(
                        "No event found in session queue session=%s",
                        opaque_log_identifier(session_id, namespace="session"),
                    )
                    # If the task completed during this bounded read, loop once
                    # more: its final XADD may have landed immediately after
                    # the timeout. Only an empty read that *started* after the
                    # task was already done proves the stream is drained.
                    if task_was_done:
                        break
                    continue
                redis_start_id = event_id
                event = TypeAdapter(AgentEvent).validate_json(event_str)
                event.id = event_id
                if event.seq is not None:
                    if last_seen_seq is not None and event.seq <= last_seen_seq:
                        continue
                    last_seen_seq = event.seq
                if (isinstance(event, (DoneEvent, ErrorEvent, WaitEvent))
                        and not await self._terminal_matches_input(session_id, stream_input_key, stream_input_seq, event)):
                    continue
                logger.debug(
                    "Got event from session queue session=%s event_type=%s",
                    opaque_log_identifier(session_id, namespace="session"),
                    type(event).__name__,
                )
                yield event
                if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                    stream_terminal = True
                    break
            if not stream_terminal and getattr(self, "_input_delivery", None) is not None:
                async for event in self._tail_accepted_input(session_id, last_seen_seq, stream_input_seq, stream_input_key):
                    yield event
            
            logger.info(
                "Session stream completed session=%s",
                opaque_log_identifier(session_id, namespace="session"),
            )

        except asyncio.CancelledError:
            logger.info(
                "Session stream disconnected; agent task continues session=%s",
                opaque_log_identifier(session_id, namespace="session"),
            )
            raise
        except Exception as e:
            logger.error(
                "Session stream failed session=%s error_type=%s",
                opaque_log_identifier(session_id, namespace="session"),
                type(e).__name__,
            )
            event = ErrorEvent(error=public_error_message(e))
            try:
                await self._session_repository.add_event(session_id, event)
            except Exception as persist_error:
                logger.warning(
                    "Failed to persist session stream error session=%s error_type=%s",
                    opaque_log_identifier(session_id, namespace="session"),
                    type(persist_error).__name__,
                )
            yield event # TODO: raise api exception

    async def _terminal_matches_input(self, session_id, key, input_seq, event) -> bool:
        if input_seq is None or event.seq is None:
            return True
        if event.seq < input_seq:
            return False
        delivery = getattr(self, "_input_delivery", None)
        matches = getattr(delivery.repository, "matches_terminal", None) if delivery is not None else None
        # A late cancellation notice can have a *higher* seq than the next
        # accepted input. Stable producer identity supplies its true ownership.
        return await matches(session_id, key, event.seq, event.type) if key and callable(matches) else True

    async def _tail_accepted_input(self, session_id: str, cursor: int | None, input_seq: int | None = None, input_key: str | None = None):
        """Keep the existing SSE open through a process/Redis recovery gap.

        Mongo is the source of truth. This bounded polling path also follows
        replacement tasks, including tasks owned by a different worker.
        """
        while True:
            pending = await self._input_delivery.repository.unsettled(session_id)
            if cursor is None:
                if pending is None:
                    return
                cursor = max(0, pending.event.seq - 1)
            events = await self._session_repository.get_events_after(session_id, cursor)
            terminal_seq = None
            for event in events:
                if event.seq is None or event.seq <= cursor:
                    continue
                cursor = event.seq
                if isinstance(event, MessageEvent) and event.role == "user":
                    terminal_seq = None
                    continue
                if (isinstance(event, (DoneEvent, ErrorEvent, WaitEvent))
                        and not await self._terminal_matches_input(session_id, input_key, input_seq, event)):
                    continue
                yield event
                terminal_seq = cursor if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)) else None
            if terminal_seq == cursor or pending is None:
                return
            await asyncio.sleep(0.5)

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

    async def _prepare_input_selection(self, session_id, user_id, attachments, input_file_ids, *, checkpoint=None):
        """Freeze only explicitly authorized user inputs at message admission."""
        history = await self._session_repository.get_events(session_id)
        if checkpoint and checkpoint.get("analysis_input_manifest"):
            context = AnalysisInputContext.model_validate(checkpoint["analysis_input_manifest"])
            selected = []
            for source in context.sources:
                if source.kind != "upload":
                    continue
                for item in source.files:
                    from pathlib import PurePosixPath
                    path = PurePosixPath(item.runtime_path)
                    selected.append(FileInfo(file_id=item.file_id, filename=path.name, size=item.size,
                        content_type=item.content_type, metadata={"analysis_input_namespace": path.parent.name,
                                                                  "analysis_input_filename": path.name}))
            submitted = []
        else:
            incoming = []
            for item in attachments or []:
                if not isinstance(item, dict) or not isinstance(item.get("file_id"), str) or not item["file_id"]:
                    raise ValueError("上传资料缺少有效文件标识。")
                info = await self._file_storage.get_file_info(item["file_id"], user_id)
                if info is None:
                    raise ValueError("上传资料已不可用或无权访问，请重新选择文件。")
                incoming.append(info)
            selected, submitted = select_input_files(history, incoming, input_file_ids)
        # Reauthorize even inherited inputs; stale event records are not permission.
        for item in selected:
            current = await self._file_storage.get_file_info(item.file_id, user_id)
            if current is None:
                raise ValueError("所选分析资料已不可用或无权访问，请调整资料范围。")
            if current.size != item.size:
                raise ValueError("分析资料内容已变化，请重新选择文件。")
            item.user_id = current.user_id or user_id
            item.upload_date = current.upload_date
        by_id = {item.file_id: item for item in selected}
        return selected, [by_id[item.file_id] for item in submitted]
