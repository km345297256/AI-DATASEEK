from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query
from sse_starlette.sse import EventSourceResponse
from typing import AsyncGenerator, List, Optional
from sse_starlette.event import ServerSentEvent
from datetime import datetime
import asyncio
import websockets
import logging
from app.interfaces.dependencies import get_file_service, get_user_repository

from app.application.services.agent_service import AgentService
from app.application.services.token_service import TokenService
from app.application.services.agent_profile_service import AgentProfileService
from app.application.services.model_configuration_service import resolve_agent_profile
from app.application.errors.exceptions import NotFoundError, UnauthorizedError
from app.interfaces.dependencies import get_agent_service, get_current_user, get_optional_current_user, get_token_service, verify_signature_websocket, get_agent_profile_service
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.session import (
    ChatRequest, ShellViewRequest, CreateSessionResponse, GetSessionResponse,
    ListSessionItem, ListSessionResponse, ShellViewResponse,
    ShareSessionResponse, SharedSessionResponse, CreateSessionRequest,
    UpdateSessionTitleRequest, TaskFeedbackRequest, TaskFeedbackResponse,
)
from app.interfaces.schemas.file import FileInfoResponse, FileViewRequest, FileViewResponse
from app.interfaces.schemas.resource import AccessTokenRequest, SignedUrlResponse
from app.interfaces.schemas.event import EventMapper
from app.domain.models.file import FileInfo
from app.domain.models.user import User
from app.domain.repositories.user_repository import UserRepository
from app.core.config import get_settings
from app.domain.services.skills import SkillRegistry
from app.infrastructure.repositories.skill_repository import MongoSkillRepository
from app.infrastructure.repositories.mongo_mcp_repository import MongoMCPRepository
from app.domain.models.mcp_config import can_access_mcp, is_mcp_owned_by
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.infrastructure.models.documents import TaskFeedbackDocument

logger = logging.getLogger(__name__)
SESSION_POLL_INTERVAL = 5

router = APIRouter(prefix="/sessions", tags=["sessions"])


def merge_skill_names(*groups: List[str]) -> List[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for skill_name in (name for group in groups for name in group):
        normalized = skill_name.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(skill_name.strip())
    return merged


async def _installed_skill_names(user: User, requested: List[str]) -> List[str]:
    settings = get_settings()
    registry = SkillRegistry(
        settings.skills_dir,
        settings.skills_enabled,
        user_id=user.id,
        repository=MongoSkillRepository(),
        user_skills_dir=settings.user_skills_dir,
    )
    await registry.load()
    installed = {
        skill.name.strip().lower()
        for skill in registry.list_skills()
        if (
            (skill.owner_user_id or skill.user_id) == user.id
            or skill.id in user.installed_skill_ids
        )
    }
    return [name for name in requested if name.strip().lower() in installed]


async def _installed_mcp_names(user: User, requested: List[str]) -> List[str]:
    config = await MongoMCPRepository().get_mcp_config()
    installed = {
        name
        for name, server in config.mcpServers.items()
        if (
            can_access_mcp(server, user.id, is_admin=user.role == "admin")
            and (
                is_mcp_owned_by(server, user.id)
                or name in user.installed_mcp_names
            )
        )
    }
    return [name for name in requested if name in installed]


def _sort_session_files(files: List[FileInfo], sort_by: str, sort_order: str) -> List[FileInfo]:
    if sort_by not in {"filename", "size", "upload_date"}:
        sort_by = "upload_date"
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"

    def value_key(file: FileInfo):
        if sort_by == "filename":
            return ((file.filename or file.file_path or "").lower(), file.file_id or "")
        if sort_by == "size":
            return (file.size or 0, file.filename or file.file_path or "")
        return (file.upload_date or datetime.min, file.filename or file.file_path or "")

    present_files = [file for file in files if getattr(file, sort_by) is not None]
    missing_files = [file for file in files if getattr(file, sort_by) is None]
    return sorted(present_files, key=value_key, reverse=sort_order == "desc") + missing_files


async def _agent_profile_overrides(
    profile_service: AgentProfileService,
    profile_id: Optional[str],
    current_user: User,
) -> Optional[dict]:
    if not profile_id:
        return None
    profile = await profile_service.get_profile(profile_id)
    if not profile:
        raise NotFoundError("Agent profile not found")
    if profile.user_id is not None and profile.user_id != current_user.id:
        raise UnauthorizedError("Not authorized to use this agent profile")
    model_settings, profile_data = await resolve_agent_profile(profile)
    return {k: v for k, v in {
        **model_settings,
        'system_prompt': profile.system_prompt,
        'agent_profile': profile_data,
    }.items() if v is not None}


@router.put("", response_model=APIResponse[CreateSessionResponse])
async def create_session(
    request: Optional[CreateSessionRequest] = None,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
    profile_service: AgentProfileService = Depends(get_agent_profile_service),
) -> APIResponse[CreateSessionResponse]:
    llm_overrides = await _agent_profile_overrides(
        profile_service,
        request.agent_profile_id if request else None,
        current_user,
    )
    session = await agent_service.create_session(
        current_user.id,
        llm_overrides=llm_overrides,
    )
    return APIResponse.success(CreateSessionResponse(
        session_id=session.id,
        created_at=int(session.created_at.timestamp()),
    ))

@router.get("/{session_id}", response_model=APIResponse[GetSessionResponse])
async def get_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> APIResponse[GetSessionResponse]:
    session = await agent_service.get_session(session_id, current_user.id)
    if not session:
        raise NotFoundError("Session not found")
    events = await agent_service.get_session_events(session_id, current_user.id)
    return APIResponse.success(GetSessionResponse(
        session_id=session.id,
        created_at=int(session.created_at.timestamp()),
        title=session.title,
        title_manually_set=session.title_manually_set,
        status=session.status,
        events=await EventMapper.events_to_sse_events(events),
        is_shared=session.is_shared,
        is_owner=True,
        collaborators=[],
    ))

@router.delete("/{session_id}", response_model=APIResponse[None])
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[None]:
    await agent_service.delete_session(session_id, current_user.id)
    return APIResponse.success()


@router.patch("/{session_id}/title", response_model=APIResponse[None])
async def update_session_title(
    session_id: str,
    request: UpdateSessionTitleRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> APIResponse[None]:
    await agent_service.update_session_title(session_id, current_user.id, request.title)
    return APIResponse.success()


@router.post("/{session_id}/stop", response_model=APIResponse[None])
async def stop_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[None]:
    await agent_service.stop_session(session_id, current_user.id)
    return APIResponse.success()

@router.post("/{session_id}/clear_unread_message_count", response_model=APIResponse[None])
async def clear_unread_message_count(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[None]:
    await agent_service.clear_unread_message_count(session_id, current_user.id)
    return APIResponse.success()

@router.get("", response_model=APIResponse[ListSessionResponse])
async def get_all_sessions(
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ListSessionResponse]:
    summaries = await agent_service.get_all_sessions(current_user.id)
    session_items = [
        ListSessionItem(
            session_id=s.id,
            title=s.title,
            status=s.status,
            unread_message_count=s.unread_message_count,
            latest_message=s.latest_message,
            latest_message_at=int(s.latest_message_at.timestamp()) if s.latest_message_at else None,
            is_shared=s.is_shared,
            is_owner=s.user_id == current_user.id,
        ) for s in summaries
    ]
    return APIResponse.success(ListSessionResponse(sessions=session_items))

@router.post("")
async def stream_sessions(
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> EventSourceResponse:
    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        while True:
            summaries = await agent_service.get_all_sessions(current_user.id)
            session_items = [
                ListSessionItem(
                    session_id=s.id,
                    title=s.title,
                    status=s.status,
                    unread_message_count=s.unread_message_count,
                    latest_message=s.latest_message,
                    latest_message_at=int(s.latest_message_at.timestamp()) if s.latest_message_at else None,
                    is_shared=s.is_shared,
                    is_owner=s.user_id == current_user.id,
                ) for s in summaries
            ]
            yield ServerSentEvent(
                event="sessions",
                data=ListSessionResponse(sessions=session_items).model_dump_json()
            )
            await asyncio.sleep(SESSION_POLL_INTERVAL)
    return EventSourceResponse(event_generator())


@router.post("/{session_id}/chat")
async def chat(
    session_id: str,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
    profile_service: AgentProfileService = Depends(get_agent_profile_service),
    user_repository: UserRepository = Depends(get_user_repository),
) -> EventSourceResponse:
    dataset_service = DataCenterDatasetService()
    for dataset_id in dict.fromkeys(request.dataset_ids or []):
        await dataset_service.get_dataset(dataset_id, user_id=current_user.id)

    llm_overrides = await _agent_profile_overrides(
        profile_service,
        request.agent_profile_id,
        current_user,
    )
    stored_user = await user_repository.get_user_by_id(current_user.id)
    user = stored_user or current_user
    effective_skills = await _installed_skill_names(user, merge_skill_names(
        user.auto_enabled_skills or [],
        request.skills or [],
    ))
    effective_mcp_servers = await _installed_mcp_names(user, request.mcp_servers or [])

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        async for event in agent_service.chat(
            session_id=session_id,
            user_id=current_user.id,
            message=request.message,
            timestamp=datetime.fromtimestamp(request.timestamp) if request.timestamp else None,
            event_id=request.event_id,
            attachments=request.attachments,
            skills=effective_skills,
            mcp_servers=effective_mcp_servers,
            dataset_ids=request.dataset_ids or [],
            mcp_access_all=user.role == "admin",
            llm_overrides=llm_overrides,
            client_message_id=request.client_message_id,
        ):
            logger.debug(f"Received event from chat: {event}")
            sse_event = await EventMapper.event_to_sse_event(event)
            logger.debug(f"Received event: {sse_event}")
            if sse_event:
                yield ServerSentEvent(
                    event=sse_event.event,
                    data=sse_event.data.model_dump_json() if sse_event.data else None
                )

    return EventSourceResponse(event_generator())

@router.post("/{session_id}/shell")
async def view_shell(
    session_id: str,
    request: ShellViewRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ShellViewResponse]:
    """View shell session output
    
    If the agent does not exist or fails to get shell output, an appropriate exception will be thrown and handled by the global exception handler
    
    Args:
        session_id: Session ID
        request: Shell view request containing session ID
        
    Returns:
        APIResponse with shell output
    """
    result = await agent_service.shell_view(session_id, request.session_id, current_user.id)
    return APIResponse.success(result)

@router.post("/{session_id}/file")
async def view_file(
    session_id: str,
    request: FileViewRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[FileViewResponse]:
    """View file content
    
    If the agent does not exist or fails to get file content, an appropriate exception will be thrown and handled by the global exception handler
    
    Args:
        session_id: Session ID
        request: File view request containing file path
        
    Returns:
        APIResponse with file content
    """
    result = await agent_service.file_view(session_id, request.file, current_user.id)
    return APIResponse.success(result)

@router.websocket("/{session_id}/vnc")
async def vnc_websocket(
    websocket: WebSocket,
    session_id: str,
    signature: str = Depends(verify_signature_websocket),
    agent_service: AgentService = Depends(get_agent_service)
) -> None:
    """VNC WebSocket endpoint (binary mode)
    
    Establishes a connection with the VNC WebSocket service in the sandbox environment and forwards data bidirectionally
    Supports authentication via signed URL with signature verification
    
    Args:
        websocket: WebSocket connection
        session_id: Session ID
        signature: Verified signature from dependency injection
    """
    
    await websocket.accept(subprotocol="binary")
    logger.info(f"Accepted WebSocket connection for session {session_id}")
    
    try:
        # Get sandbox environment address with user validation
        sandbox_ws_url = await agent_service.get_vnc_url(session_id)

        logger.info(f"Connecting to VNC WebSocket at {sandbox_ws_url}")
    
        # Connect to sandbox WebSocket
        async with websockets.connect(sandbox_ws_url) as sandbox_ws:
            logger.info(f"Connected to VNC WebSocket at {sandbox_ws_url}")
            # Create two tasks to forward data bidirectionally
            async def forward_to_sandbox():
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await sandbox_ws.send(data)
                except WebSocketDisconnect:
                    logger.info("Web -> VNC connection closed")
                    pass
                except Exception as e:
                    logger.error(f"Error forwarding data to sandbox: {e}")
            
            async def forward_from_sandbox():
                try:
                    while True:
                        data = await sandbox_ws.recv()
                        await websocket.send_bytes(data)
                except websockets.exceptions.ConnectionClosed:
                    logger.info("VNC -> Web connection closed")
                    pass
                except Exception as e:
                    logger.error(f"Error forwarding data from sandbox: {e}")
            
            # Run two forwarding tasks concurrently
            forward_task1 = asyncio.create_task(forward_to_sandbox())
            forward_task2 = asyncio.create_task(forward_from_sandbox())
            
            # Wait for either task to complete (meaning connection has closed)
            done, pending = await asyncio.wait(
                [forward_task1, forward_task2],
                return_when=asyncio.FIRST_COMPLETED
            )

            logger.info("WebSocket connection closed")
            
            # Cancel pending tasks
            for task in pending:
                task.cancel()
    
    except ConnectionError as e:
        logger.error(f"Unable to connect to sandbox environment: {str(e)}")
        await websocket.close(code=1011, reason="Unable to connect to sandbox VNC")
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")
        await websocket.close(code=1011, reason="VNC WebSocket error")

@router.get("/{session_id}/files")
async def get_session_files(
    session_id: str,
    sort_by: str = Query("upload_date", pattern="^(filename|size|upload_date)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    current_user: Optional[User] = Depends(get_optional_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[List[FileInfoResponse]]:
    if not current_user and not await agent_service.is_session_shared(session_id):
        raise UnauthorizedError()
    files = await agent_service.get_session_files(session_id, current_user.id if current_user else None)
    return APIResponse.success([
        FileInfoResponse.public_from_file_info(file)
        for file in _sort_session_files(files, sort_by, sort_order)
    ])


@router.post("/{session_id}/vnc/signed-url", response_model=APIResponse[SignedUrlResponse])
async def create_vnc_signed_url(
    session_id: str,
    request_data: AccessTokenRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
    token_service: TokenService = Depends(get_token_service)
) -> APIResponse[SignedUrlResponse]:
    """Generate signed URL for VNC WebSocket access
    
    This endpoint creates a signed URL that allows temporary access to the VNC
    WebSocket for a specific session without requiring authentication headers.
    """
    
    # Validate expiration time (max 15 minutes)
    expire_minutes = request_data.expire_minutes
    if expire_minutes > 15:
        expire_minutes = 15
    
    # Check if session exists and belongs to user
    session = await agent_service.get_session(session_id, current_user.id)
    if not session:
        raise NotFoundError("Session not found")
    
    # Create signed URL for VNC WebSocket
    ws_base_url = f"/api/v1/sessions/{session_id}/vnc"
    signed_url = token_service.create_signed_url(
        base_url=ws_base_url,
        expire_minutes=expire_minutes
    )
    
    logger.info(f"Created signed URL for VNC access for user {current_user.id}, session {session_id}")
    
    return APIResponse.success(SignedUrlResponse(
        signed_url=signed_url,
        expires_in=expire_minutes * 60,
    ))


@router.post("/{session_id}/share", response_model=APIResponse[ShareSessionResponse])
async def share_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ShareSessionResponse]:
    """Share a session to make it publicly accessible
    
    This endpoint marks a session as shared, allowing it to be accessed
    without authentication using the shared session endpoint.
    """
    await agent_service.share_session(session_id, current_user.id)
    return APIResponse.success(ShareSessionResponse(
        session_id=session_id,
        is_shared=True
    ))

@router.get("/{session_id}/share/files")
async def get_shared_session_files(
    session_id: str,
    sort_by: str = Query("upload_date", pattern="^(filename|size|upload_date)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[List[FileInfoResponse]]:
    files = await agent_service.get_shared_session_files(session_id)
    files = _sort_session_files(files, sort_by, sort_order)
    file_service = get_file_service()
    for file in files:
        await file_service.enrich_with_file_url(file)
    return APIResponse.success([
        FileInfoResponse.public_from_file_info(file)
        for file in files
    ])


@router.delete("/{session_id}/share", response_model=APIResponse[ShareSessionResponse])
async def unshare_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[ShareSessionResponse]:
    """Unshare a session to make it private again
    
    This endpoint marks a session as not shared, removing public access.
    """
    await agent_service.unshare_session(session_id, current_user.id)
    return APIResponse.success(ShareSessionResponse(
        session_id=session_id,
        is_shared=False
    ))


@router.get("/{session_id}/feedback", response_model=APIResponse[TaskFeedbackResponse])
async def get_task_feedback(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> APIResponse[TaskFeedbackResponse]:
    session = await agent_service.get_session(session_id, current_user.id)
    if not session:
        raise NotFoundError("Session not found")
    feedback = await TaskFeedbackDocument.find_one({"session_id": session_id, "user_id": current_user.id})
    if not feedback:
        return APIResponse.success(TaskFeedbackResponse())
    return APIResponse.success(TaskFeedbackResponse(
        preference=feedback.preference,
        dislike_reasons=feedback.dislike_reasons,
        detail=feedback.detail,
    ))


@router.put("/{session_id}/feedback", response_model=APIResponse[TaskFeedbackResponse])
async def save_task_feedback(
    session_id: str,
    request: TaskFeedbackRequest,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> APIResponse[TaskFeedbackResponse]:
    session = await agent_service.get_session(session_id, current_user.id)
    if not session:
        raise NotFoundError("Session not found")
    now = datetime.now().astimezone()
    reasons = request.dislike_reasons if request.preference == "dislike" else []
    detail = request.detail if request.preference == "dislike" else ""
    feedback = await TaskFeedbackDocument.find_one({"session_id": session_id, "user_id": current_user.id})
    if feedback:
        feedback.session_title = session.title
        feedback.preference = request.preference
        feedback.dislike_reasons = reasons
        feedback.detail = detail
        feedback.updated_at = now
        await feedback.save()
    else:
        feedback = TaskFeedbackDocument(
            session_id=session_id,
            user_id=current_user.id,
            session_title=session.title,
            preference=request.preference,
            dislike_reasons=reasons,
            detail=detail,
            created_at=now,
            updated_at=now,
        )
        await feedback.insert()
    return APIResponse.success(TaskFeedbackResponse(
        preference=feedback.preference,
        dislike_reasons=feedback.dislike_reasons,
        detail=feedback.detail,
    ))


@router.delete("/{session_id}/feedback", response_model=APIResponse[TaskFeedbackResponse])
async def delete_task_feedback(
    session_id: str,
    current_user: User = Depends(get_current_user),
    agent_service: AgentService = Depends(get_agent_service),
) -> APIResponse[TaskFeedbackResponse]:
    session = await agent_service.get_session(session_id, current_user.id)
    if not session:
        raise NotFoundError("Session not found")
    feedback = await TaskFeedbackDocument.find_one({"session_id": session_id, "user_id": current_user.id})
    if feedback:
        await feedback.delete()
    return APIResponse.success(TaskFeedbackResponse())


@router.get("/shared/{session_id}", response_model=APIResponse[SharedSessionResponse])
async def get_shared_session(
    session_id: str,
    agent_service: AgentService = Depends(get_agent_service)
) -> APIResponse[SharedSessionResponse]:
    """Get a shared session without authentication
    
    This endpoint allows public access to sessions that have been marked as shared.
    No authentication is required, but the session must be explicitly shared.
    """
    session = await agent_service.get_shared_session(session_id)
    if not session:
        raise NotFoundError("Shared session not found")
    events = await agent_service.get_session_events(session_id)
    return APIResponse.success(SharedSessionResponse(
        session_id=session.id,
        title=session.title,
        status=session.status,
        events=await EventMapper.events_to_sse_events(events),
        is_shared=session.is_shared
    ))
