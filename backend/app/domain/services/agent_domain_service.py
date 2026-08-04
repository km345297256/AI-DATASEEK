from typing import Optional, AsyncGenerator, List
import asyncio
import io
import logging
from datetime import datetime
from app.domain.models.session import Session, SessionStatus
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.models.event import BaseEvent, ErrorEvent, DoneEvent, MessageEvent, WaitEvent, AgentEvent
from pydantic import TypeAdapter
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.external.task import Task
from typing import Type
from app.domain.external.file import FileStorage
from app.domain.external.sandbox_runtime import SandboxRuntime
from app.domain.models.file import FileInfo
from app.domain.repositories.mcp_repository import MCPRepository
from app.infrastructure.external.sandbox.sandbox_pool import get_sandbox_pool
from app.infrastructure.external.sandbox.runtime import get_default_sandbox_runtime

# Setup logging
logger = logging.getLogger(__name__)

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
        self._chat_bootstrap_tasks: set[asyncio.Task] = set()
        logger.info("AgentDomainService initialization completed")
            
    async def shutdown(self) -> None:
        """Clean up all Agent's resources"""
        logger.info("Starting to close all Agents")
        await self._task_cls.destroy()
        logger.info("All agents closed successfully")

    async def _create_task(self, session: Session, dataset_ids: Optional[List[str]] = None) -> Task:
        """Create a new agent task"""
        sandbox_runtime = self._sandbox_runtime
        sandbox = None
        sandbox_replaced = False
        sandbox_id = session.sandbox_id
        requested_dataset_ids = list(dict.fromkeys(dataset_ids or []))
        if sandbox_id and set(session.sandbox_dataset_ids) != set(requested_dataset_ids):
            try:
                stale_sandbox = await sandbox_runtime.restore(sandbox_id)
                await stale_sandbox.destroy()
            except Exception as exc:
                logger.warning("Failed to destroy sandbox %s before dataset remount: %s", sandbox_id, exc)
            logger.info(
                "Replacing session %s sandbox because dataset mounts changed from %s to %s",
                session.id,
                session.sandbox_dataset_ids,
                requested_dataset_ids,
            )
            session.sandbox_id = None
            session.sandbox_dataset_ids = []
            sandbox_id = None
            sandbox_replaced = True
        if sandbox_id:
            try:
                sandbox = await sandbox_runtime.restore(sandbox_id)
                if hasattr(sandbox, "is_paused") and await sandbox.is_paused():
                    logger.info("Session %s sandbox %s is paused; resuming", session.id, sandbox_id)
                    if not await sandbox.resume():
                        logger.warning("Session %s sandbox %s failed to resume; creating a replacement", session.id, sandbox_id)
                        sandbox = None
                if hasattr(sandbox, "is_available") and not await sandbox.is_available():
                    logger.warning("Session %s sandbox %s is unavailable; creating a replacement", session.id, sandbox_id)
                    sandbox = None
            except Exception as e:
                logger.warning("Session %s sandbox %s could not be restored: %s", session.id, sandbox_id, e)
                sandbox = None
                session.sandbox_id = None
            sandbox_replaced = sandbox is None

        if not sandbox:
            pool = get_sandbox_pool() if not dataset_ids else None
            if pool:
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
                await self._hydrate_replacement_sandbox(session, sandbox, previous_sandbox_id=sandbox_id)

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
        )

        task = self._task_cls.create(task_runner)
        session.task_id = task.id
        await self._session_repository.save(session)

        # Update record with task_id now that we have it
        await sandbox_runtime.assign(sandbox, session, task.id)

        return task

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
        for file_info in session.files:
            if not file_info.file_id or not file_info.file_path or file_info.file_path in seen_paths:
                continue
            seen_paths.add(file_info.file_path)
            try:
                file_data, stored_info = await self._file_storage.download_file(file_info.file_id, session.user_id)
                raw = file_data.read()
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                result = await sandbox.file_upload(
                    io.BytesIO(raw),
                    file_info.file_path,
                    filename=stored_info.filename or file_info.filename,
                )
                if result.success:
                    restored += 1
                else:
                    failed += 1
                    logger.warning(
                        "Failed to hydrate file %s into replacement sandbox %s: %s",
                        file_info.file_path,
                        sandbox.id,
                        result.message,
                    )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Failed to hydrate file %s (%s) for session %s into replacement sandbox %s: %s",
                    file_info.file_path,
                    file_info.file_id,
                    session.id,
                    sandbox.id,
                    exc,
                )
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
            await self._session_repository.add_event(session_id, ErrorEvent(error=str(exc)))
            await self._session_repository.update_status(session_id, SessionStatus.COMPLETED)
        except Exception:
            logger.exception("Failed to persist chat bootstrap error for session %s", session_id)

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
    ) -> Task:
        try:
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

            task = await self._get_task(session)
            if task is None or task.done:
                if session.task_id and task is None:
                    logger.warning(
                        "Session %s references missing task %s; creating a new task",
                        session.id,
                        session.task_id,
                    )
                task = (
                    await self._create_task(session, effective_dataset_ids)
                    if effective_dataset_ids
                    else await self._create_task(session)
                )
                if not task:
                    raise RuntimeError("Failed to create task")

            await self._session_repository.update_latest_message(session.id, message, timestamp or datetime.now())

            message_event = MessageEvent(
                message=message,
                role="user",
                attachments=await self._resolve_message_attachments(attachments, user_id),
                metadata={
                    "skills": skills or [],
                    "mcp_servers": mcp_servers or [],
                    "dataset_ids": effective_dataset_ids,
                    "mcp_access_all": mcp_access_all,
                },
            )

            event_id = await task.input_stream.put(message_event.model_dump_json())
            message_event.id = event_id
            await self._session_repository.add_event(session.id, message_event)

            await task.run()
            logger.debug("Put message into Session %s's event queue: %s...", session.id, message[:50])
            return task
        except Exception as exc:
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
            event = ErrorEvent(error=str(e))
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
