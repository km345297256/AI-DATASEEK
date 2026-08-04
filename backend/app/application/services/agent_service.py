from typing import AsyncGenerator, Optional, List, Type
import logging
from datetime import datetime
from app.domain.models.session import Session, SessionSummary
from app.domain.repositories.session_repository import SessionRepository

from app.interfaces.schemas.session import ShellViewResponse
from app.interfaces.schemas.file import FileViewResponse
from app.domain.models.agent import Agent
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.models.event import AgentEvent
from app.domain.external.sandbox import Sandbox
from app.domain.external.sandbox_runtime import SandboxRuntime
from app.domain.external.search import SearchEngine
from app.domain.external.file import FileStorage
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.external.task import Task
from app.domain.models.file import FileInfo
from app.domain.models.user import User
from app.core.config import get_settings
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.session import SessionStatus
from app.infrastructure.external.sandbox.runtime import get_default_sandbox_runtime

# Set up logger
logger = logging.getLogger(__name__)

class AgentService:
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
        logger.info("Initializing AgentService")
        self._agent_repository = agent_repository
        self._session_repository = session_repository
        self._file_storage = file_storage
        self._sandbox_runtime = sandbox_runtime or get_default_sandbox_runtime(sandbox_cls)
        self._agent_domain_service = AgentDomainService(
            self._agent_repository,
            self._session_repository,
            sandbox_cls,
            task_cls,
            file_storage,
            mcp_repository,
            search_engine,
            self._sandbox_runtime,
        )
        self._search_engine = search_engine
        self._sandbox_cls = sandbox_cls
    
    async def create_session(
        self,
        user_id: str,
        llm_overrides: Optional[dict] = None,
    ) -> Session:
        logger.info(f"Creating new session for user: {user_id}")
        agent = await self._create_agent()
        session = Session(
            agent_id=agent.id,
            user_id=user_id,
            llm_overrides=llm_overrides,
        )
        logger.info(f"Created new Session with ID: {session.id} for user: {user_id}")
        await self._session_repository.save(session)
        return session

    async def _create_agent(self) -> Agent:
        logger.info("Creating new agent")
        settings = get_settings()
        agent = Agent(
            model_name=settings.model_name,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
        logger.info(f"Created new Agent with ID: {agent.id}")
        
        # Save agent to repository
        await self._agent_repository.save(agent)
        logger.info(f"Saved agent {agent.id} to repository")
        
        logger.info(f"Agent created successfully with ID: {agent.id}")
        return agent

    async def chat(
        self,
        session_id: str,
        user_id: str,
        message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        event_id: Optional[str] = None,
        attachments: Optional[List[dict]] = None,
        skills: Optional[List[str]] = None,
        mcp_servers: Optional[List[str]] = None,
        dataset_ids: Optional[List[str]] = None,
        mcp_access_all: bool = False,
        llm_overrides: Optional[dict] = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        logger.info(f"Starting chat with session {session_id}: {message[:50]}...")
        # Directly use the domain service's chat method, which will check if the session exists
        async for event in self._agent_domain_service.chat(
            session_id=session_id,
            user_id=user_id,
            message=message,
            timestamp=timestamp,
            latest_event_id=event_id,
            attachments=attachments,
            skills=skills,
            mcp_servers=mcp_servers,
            dataset_ids=dataset_ids,
            mcp_access_all=mcp_access_all,
            llm_overrides=llm_overrides,
        ):
            logger.debug(f"Received event: {event}")
            yield event
        logger.info(f"Chat with session {session_id} completed")
    
    async def get_session(self, session_id: str, user_id: Optional[str] = None) -> Optional[Session]:
        """Get a session by ID, ensuring it belongs to the user"""
        logger.info(f"Getting session {session_id} for user {user_id}")
        if not user_id:
            session = await self._session_repository.find_by_id(session_id)
        else:
            session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
        return session

    async def get_session_events(self, session_id: str, user_id: Optional[str] = None) -> List[AgentEvent]:
        """Get all events for a session"""
        logger.info(f"Getting events for session {session_id}")
        if user_id:
            session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
            if not session:
                raise RuntimeError("Session not found")
        return await self._session_repository.get_events(session_id)
    
    async def get_all_sessions(self, user_id: str) -> List[SessionSummary]:
        """Get all sessions for a specific user (lightweight summaries)"""
        logger.info(f"Getting all sessions for user {user_id}")
        return await self._session_repository.find_summaries_by_user_id(user_id)

    async def get_dataset_sessions(self, user_id: str, dataset_id: str) -> List[SessionSummary]:
        """Get lightweight history for the dataset chat demo."""
        logger.info("Getting dataset %s sessions for user %s", dataset_id, user_id)
        return await self._session_repository.find_dataset_summaries_by_user_id(user_id, dataset_id)

    async def delete_session(self, session_id: str, user_id: str) -> None:
        """Delete a session, ensuring it belongs to the user"""
        logger.info(f"Deleting session {session_id} for user {user_id}")
        # First verify the session belongs to the user
        session = await self._session_repository.find_owned_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        await self._session_repository.delete(session_id)
        logger.info(f"Session {session_id} deleted successfully")

    async def update_session_title(self, session_id: str, user_id: str, title: str) -> None:
        """Update a session title, ensuring it belongs to the user."""
        session = await self._session_repository.find_owned_by_id_and_user_id(session_id, user_id)
        if not session:
            raise RuntimeError("Session not found")
        await self._session_repository.update_title_manually(session_id, title.strip())

    async def stop_session(self, session_id: str, user_id: str) -> None:
        """Stop a session, ensuring it belongs to the user"""
        logger.info(f"Stopping session {session_id} for user {user_id}")
        # First verify the session belongs to the user
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        await self._agent_domain_service.stop_session(session_id)
        logger.info(f"Session {session_id} stopped successfully")

    async def clear_unread_message_count(self, session_id: str, user_id: str) -> None:
        """Clear the unread message count for a session, ensuring it belongs to the user"""
        logger.info(f"Clearing unread message count for session {session_id} for user {user_id}")
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            raise RuntimeError("Session not found")
        await self._session_repository.update_unread_message_count(session_id, 0)
        logger.info(f"Unread message count cleared for session {session_id}")

    async def shutdown(self):
        logger.info("Closing all agents and cleaning up resources")
        # Clean up all Agents and their associated sandboxes
        await self._agent_domain_service.shutdown()
        logger.info("All agents closed successfully")

    async def _restore_session_sandbox(self, session: Session) -> Sandbox:
        if not session.sandbox_id:
            raise RuntimeError("Session has no sandbox environment")
        sandbox = await self._sandbox_runtime.restore(session.sandbox_id)
        if hasattr(sandbox, "is_paused") and await sandbox.is_paused():
            logger.info("Session %s sandbox %s is paused; resuming for direct access", session.id, session.sandbox_id)
            if not await sandbox.resume():
                raise RuntimeError("Failed to resume sandbox environment")
            await sandbox.ensure_sandbox()
        return sandbox

    async def shell_view(self, session_id: str, shell_session_id: str, user_id: str) -> ShellViewResponse:
        """View shell session output, ensuring session belongs to the user"""
        logger.info(f"Getting shell view for session {session_id} for user {user_id}")
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        sandbox = await self._restore_session_sandbox(session)
        if not sandbox:
            raise RuntimeError("Sandbox environment not found")
        
        result = await sandbox.view_shell(shell_session_id, console=True)
        if result.success:
            return ShellViewResponse(**result.data)
        else:
            raise RuntimeError(f"Failed to get shell output: {result.message}")

    async def get_vnc_url(self, session_id: str) -> str:
        """Get VNC URL for a session, ensuring it belongs to the user"""
        logger.info(f"Getting VNC URL for session {session_id}")
        
        session = await self._session_repository.find_by_id(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            raise RuntimeError("Session not found")
        
        sandbox = await self._restore_session_sandbox(session)
        if not sandbox:
            raise RuntimeError("Sandbox environment not found")
        
        return sandbox.vnc_url

    async def file_view(self, session_id: str, file_path: str, user_id: str) -> FileViewResponse:
        """View file content, ensuring session belongs to the user"""
        logger.info(f"Getting file view for session {session_id} for user {user_id}")
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")

        if session.status != SessionStatus.RUNNING:
            stored_file = next(
                (
                    file
                    for file in session.files
                    if file.file_id and (file.file_path == file_path or file.filename == file_path)
                ),
                None,
            )
            if stored_file and stored_file.file_id:
                file_data, _ = await self._file_storage.download_file(stored_file.file_id, user_id)
                content = file_data.read()
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
                return FileViewResponse(content=content, file=file_path)
            if session.status == SessionStatus.COMPLETED:
                raise RuntimeError("File is not available in server storage; resume the task environment to read it")
        
        sandbox = await self._restore_session_sandbox(session)
        if not sandbox:
            raise RuntimeError("Sandbox environment not found")
        
        result = await sandbox.file_read(file_path)
        if result.success:
            return FileViewResponse(**result.data)
        else:
            raise RuntimeError(f"Failed to read file: {result.message}")
    
    async def is_session_shared(self, session_id: str) -> bool:
        """Check if a session is shared"""
        logger.info(f"Checking if session {session_id} is shared")
        session = await self._session_repository.find_by_id(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            raise RuntimeError("Session not found")
        return session.is_shared

    async def get_session_files(self, session_id: str, user_id: Optional[str] = None) -> List[FileInfo]:
        """Get files for a session, ensuring it belongs to the user"""
        logger.info(f"Getting files for session {session_id} for user {user_id}")
        session = await self.get_session(session_id, user_id)
        return session.files
    
    async def get_shared_session_files(self, session_id: str) -> List[FileInfo]:
        """Get files for a shared session"""
        logger.info(f"Getting files for shared session {session_id}")
        session = await self._session_repository.find_by_id(session_id)
        if not session or not session.is_shared:
            logger.error(f"Shared session {session_id} not found or not shared")
            raise RuntimeError("Session not found")
        return session.files

    async def share_session(self, session_id: str, user_id: str) -> None:
        """Share a session, ensuring it belongs to the user"""
        logger.info(f"Sharing session {session_id} for user {user_id}")
        # First verify the session belongs to the user
        session = await self._session_repository.find_owned_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        await self._session_repository.update_shared_status(session_id, True)
        logger.info(f"Session {session_id} shared successfully")

    async def unshare_session(self, session_id: str, user_id: str) -> None:
        """Unshare a session, ensuring it belongs to the user"""
        logger.info(f"Unsharing session {session_id} for user {user_id}")
        # First verify the session belongs to the user
        session = await self._session_repository.find_owned_by_id_and_user_id(session_id, user_id)
        if not session:
            logger.error(f"Session {session_id} not found for user {user_id}")
            raise RuntimeError("Session not found")
        
        await self._session_repository.update_shared_status(session_id, False)
        logger.info(f"Session {session_id} unshared successfully")

    async def get_shared_session(self, session_id: str) -> Optional[Session]:
        """Get a shared session by ID (no user authentication required)"""
        logger.info(f"Getting shared session {session_id}")
        session = await self._session_repository.find_by_id(session_id)
        if not session or not session.is_shared:
            logger.error(f"Shared session {session_id} not found or not shared")
            return None
        return session

    async def get_session_collaborators(self, session_id: str, user_id: str) -> List[str]:
        session = await self._session_repository.find_by_id_and_user_id(session_id, user_id)
        if not session:
            raise RuntimeError("Session not found")
        return session.collaborator_user_ids

    async def update_session_collaborators(self, session_id: str, owner_user_id: str, collaborator_user_ids: List[str]) -> List[str]:
        session = await self._session_repository.find_owned_by_id_and_user_id(session_id, owner_user_id)
        if not session:
            raise RuntimeError("Session not found")
        unique_ids = []
        for collaborator_id in collaborator_user_ids:
            if collaborator_id and collaborator_id != owner_user_id and collaborator_id not in unique_ids:
                unique_ids.append(collaborator_id)
        await self._session_repository.update_collaborators(session_id, unique_ids)
        return unique_ids
