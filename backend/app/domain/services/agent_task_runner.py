from typing import Any, Optional, AsyncGenerator, List
import asyncio
import json
import logging
import os
import io
from pathlib import PurePosixPath
import debugpy
from pydantic import TypeAdapter
from app.domain.models.message import Message
from app.domain.models.event import (
    BaseEvent,
    ErrorEvent,
    TitleEvent,
    MessageEvent,
    DoneEvent,
    ToolEvent,
    WaitEvent,
    StepEvent,
    StepStatus,
    FileToolContent,
    ShellToolContent,
    SearchToolContent,
    BrowserToolContent,
    ToolStatus,
    AgentEvent,
    McpToolContent,
    SkillToolContent,
)
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow
from app.domain.external.sandbox import Sandbox
from app.domain.external.browser import Browser
from app.domain.external.search import SearchEngine
from app.domain.external.file import FileStorage
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.external.task import TaskRunner, Task
from app.domain.repositories.session_repository import SessionRepository
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.session import SessionStatus
from app.domain.models.file import FileInfo
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.models.tool_result import ToolResult
from app.domain.models.search import SearchResults
from app.domain.models.mcp_config import MCPConfig, can_access_mcp
from app.domain.services.completion_advice_service import get_completion_advice_service
from app.domain.services.safety import SafetyReviewAgent
from app.domain.services.audit_service import AuditService
from app.domain.models.audit import AuditRiskLevel, AuditStatus
from app.domain.models.safety import SafetyReview
from app.application.services.data_center_dataset_service import DataCenterDatasetService

logger = logging.getLogger(__name__)


def _rewind_or_buffer_stream(file_data):
    if isinstance(file_data, (bytes, bytearray)):
        return io.BytesIO(file_data)
    if hasattr(file_data, "seek"):
        try:
            file_data.seek(0)
            return file_data
        except (OSError, io.UnsupportedOperation):
            pass
    if hasattr(file_data, "read"):
        return io.BytesIO(file_data.read())
    return file_data

ARTIFACT_SEARCH_ROOTS = ("/home/ubuntu",)
ARTIFACT_EXTENSIONS = (
    ".avif",
    ".csv",
    ".geojson",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".npy",
    ".npz",
    ".obj",
    ".parquet",
    ".pkl",
    ".pdf",
    ".png",
    ".py",
    ".svg",
    ".tif",
    ".tiff",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".yaml",
    ".yml",
    ".zip",
)
ARTIFACT_EXCLUDED_PARTS = {
    ".cache",
    ".config",
    ".local",
    ".npm",
    ".venv",
    "__pycache__",
    "node_modules",
    "upload",
}
MAX_AUTO_SYNC_ARTIFACTS = 500
MAX_EVENT_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_EVENT_PREVIEW_BYTES = 256 * 1024

class AgentTaskRunner(TaskRunner):
    """Agent task that can be cancelled"""
    MAX_EVENT_PAYLOAD_BYTES = MAX_EVENT_PAYLOAD_BYTES

    def __init__(
        self,
        session_id: str,
        agent_id: str,
        user_id: str,
        sandbox: Sandbox,
        browser: Browser,
        agent_repository: AgentRepository,
        session_repository: SessionRepository,
        file_storage: FileStorage,
        mcp_repository: MCPRepository,
        search_engine: Optional[SearchEngine] = None,
        llm_overrides: Optional[dict] = None,
    ):
        self._session_id = session_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._sandbox = sandbox
        self._browser = browser
        self._search_engine = search_engine
        self._repository = agent_repository
        self._session_repository = session_repository
        self._file_storage = file_storage
        self._mcp_repository = mcp_repository
        self._mcp_tool = MCPToolkit()
        self._safety_reviewer = SafetyReviewAgent(
            usage_context={"user_id": user_id, "session_id": session_id},
        )
        self._audit_service = AuditService()
        self._completion_advice_service = get_completion_advice_service()
        self._flow = PlanActFlow(
            self._agent_id,
            self._user_id,
            self._repository,
            self._session_id,
            self._session_repository,
            self._sandbox,
            self._browser,
            self._mcp_tool,
            self._search_engine,
            llm_overrides=llm_overrides,
            file_storage=self._file_storage,
        )
        self._generated_files: List[FileInfo] = []
        self._artifact_baseline_paths: set[str] = set()
        self._dataset_service = DataCenterDatasetService()
        self._mounted_dataset_ids: set[str] = set()
        # Only files materialized from the data-center catalog are protected from
        # attachment publication.  Generated sidecars (reports, previews, etc.)
        # in the same directory must remain publishable artifacts.
        self._protected_dataset_paths: set[str] = set()
        self._protected_dataset_roots: set[str] = set()

    async def _put_and_add_event(self, task: Task, event: AgentEvent) -> None:
        event = self._bound_event_payload(event)
        event_id = await task.output_stream.put(event.model_dump_json())
        event.id = event_id
        await self._session_repository.add_event(self._session_id, event)

    def _bound_event_payload(self, event: AgentEvent) -> AgentEvent:
        """Keep Redis and Mongo session-event documents well below BSON's 16MB limit."""
        if self._event_payload_size(event) <= self.MAX_EVENT_PAYLOAD_BYTES:
            return event

        logger.warning(
            "Agent %s bounded oversized %s event before persistence (%d bytes)",
            self._agent_id,
            event.type,
            self._event_payload_size(event),
        )
        if isinstance(event, ToolEvent):
            event.function_args = self._event_preview(event.function_args)
            event.function_result = self._event_preview(event.function_result)
            if isinstance(event.tool_content, FileToolContent):
                event.tool_content.content = self._event_preview_text(event.tool_content.content)
            elif isinstance(event.tool_content, ShellToolContent):
                event.tool_content.console = [self._event_preview(event.tool_content.console)]
            elif event.tool_content is not None and hasattr(event.tool_content, "result"):
                event.tool_content.result = self._event_preview(event.tool_content.result)
            elif event.tool_content is not None:
                event.tool_content = None
        elif isinstance(event, MessageEvent):
            event.message = self._event_preview_text(event.message)
        elif isinstance(event, ErrorEvent):
            event.error = self._event_preview_text(event.error)

        if self._event_payload_size(event) <= self.MAX_EVENT_PAYLOAD_BYTES:
            return event
        logger.error(
            "Agent %s event remained oversized after bounding; replacing it with an error event",
            self._agent_id,
        )
        return ErrorEvent(error="Task event was too large to persist; inline output was omitted.")

    @staticmethod
    def _event_payload_size(event: AgentEvent) -> int:
        return len(event.model_dump_json().encode("utf-8"))

    @staticmethod
    def _event_preview(value: Any) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        encoded = serialized.encode("utf-8")
        return {
            "truncated": True,
            "original_bytes": len(encoded),
            "preview": encoded[:MAX_EVENT_PREVIEW_BYTES].decode("utf-8", errors="ignore"),
        }

    @staticmethod
    def _event_preview_text(value: Any) -> str:
        preview = AgentTaskRunner._event_preview(value)
        return (
            f"[Inline output truncated from {preview['original_bytes']} bytes before persistence]\n"
            f"{preview['preview']}"
        )
    
    async def _pop_event(self, task: Task) -> AgentEvent:
        event_id, event_str = await task.input_stream.pop()
        if event_str is None:
            logger.warning(f"Agent {self._agent_id} received empty message")
            return
        event = TypeAdapter(AgentEvent).validate_json(event_str)
        event.id = event_id
        return event

    async def _upload_file_to_storage(self, file_data, file_name: str, metadata: Optional[dict] = None) -> FileInfo:
        if isinstance(file_data, bytes):
            file_data = io.BytesIO(file_data)
        if hasattr(file_data, "seek"):
            file_data.seek(0)
        try:
            return await self._file_storage.upload_file(file_data, file_name, self._user_id, metadata=metadata)
        except TypeError as exc:
            if "metadata" not in str(exc):
                raise
            return await self._file_storage.upload_file(file_data, file_name, self._user_id)
    
    async def _get_browser_screenshot(self) -> str:
        screenshot = await self._browser.screenshot()
        result = await self._upload_file_to_storage(
            screenshot,
            "screenshot.png",
            metadata={"session_id": self._session_id, "source": "browser_screenshot"},
        )
        return result.file_id

    async def _sync_file_to_storage(self, file_path: str) -> Optional[FileInfo]:
        """Upload or update file and return FileInfo"""
        try:
            if not file_path:
                return None
            file_info = await self._session_repository.get_file_by_path(self._session_id, file_path)
            file_data = await self._sandbox.file_download(file_path)
            if file_info:
                await self._session_repository.remove_file(self._session_id, file_info.file_id)
            file_name = file_path.split("/")[-1]
            file_info = await self._upload_file_to_storage(
                file_data,
                file_name,
                metadata={"session_id": self._session_id, "file_path": file_path, "source": "sandbox_artifact"},
            )
            file_info.file_path = file_path
            await self._session_repository.add_file(self._session_id, file_info)
            return file_info
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to sync file: {e}")

    def _remember_generated_file(self, file_info: Optional[FileInfo]) -> None:
        if not file_info or not file_info.file_path:
            return
        existing_index = next(
            (index for index, item in enumerate(self._generated_files) if item.file_path == file_info.file_path),
            None,
        )
        if existing_index is not None:
            self._generated_files[existing_index] = file_info
            return
        self._generated_files.append(file_info)

    def _is_syncable_artifact(self, file_path: str) -> bool:
        if not file_path:
            return False
        path = PurePosixPath(file_path)
        if any(part in ARTIFACT_EXCLUDED_PARTS for part in path.parts):
            return False
        return path.suffix.lower() in ARTIFACT_EXTENSIONS

    def _is_data_center_dataset_path(self, file_path: str) -> bool:
        if not file_path:
            return False
        normalized = PurePosixPath(file_path)
        if str(normalized) in getattr(self, "_protected_dataset_paths", set()):
            return True
        return any(
            normalized == PurePosixPath(root) or PurePosixPath(root) in normalized.parents
            for root in getattr(self, "_protected_dataset_roots", set())
        )

    def _remember_mounted_dataset_paths(self, datasets: list[Any]) -> None:
        """Protect read-only mounted source trees from artifact publication."""
        protected_paths = getattr(self, "_protected_dataset_paths", None)
        if protected_paths is None:
            protected_paths = set()
            self._protected_dataset_paths = protected_paths
        protected_roots = getattr(self, "_protected_dataset_roots", None)
        if protected_roots is None:
            protected_roots = set()
            self._protected_dataset_roots = protected_roots
        for dataset in datasets:
            sandbox_path = PurePosixPath(dataset.sandbox_path)
            protected_roots.add(str(sandbox_path))
            protected_paths.add(str(sandbox_path / "DATASET_MANIFEST.json"))
            for item in dataset.files:
                protected_paths.add(str(sandbox_path / item.name))

    async def _sync_explicit_paths_to_storage(self, file_paths: List[str]) -> List[FileInfo]:
        attachments: List[FileInfo] = []
        seen_paths = set()
        for file_path in file_paths:
            if not file_path or file_path in seen_paths or self._is_data_center_dataset_path(file_path):
                continue
            seen_paths.add(file_path)
            file_info = await self._sync_file_to_storage(file_path)
            if file_info:
                attachments.append(file_info)
                self._remember_generated_file(file_info)
        return attachments

    async def _list_sandbox_artifacts(self) -> List[str]:
        discovered_paths: List[str] = []
        seen_paths = set()
        try:
            for root in ARTIFACT_SEARCH_ROOTS:
                result = await self._sandbox.file_find(root, "**/*")
                if not result.success or not result.data:
                    continue
                files = result.data.get("files", []) if isinstance(result.data, dict) else []
                for file_path in files:
                    if (
                        file_path in seen_paths
                        or self._is_data_center_dataset_path(file_path)
                        or not self._is_syncable_artifact(file_path)
                    ):
                        continue
                    seen_paths.add(file_path)
                    discovered_paths.append(file_path)
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to list sandbox artifacts: {e}")
        return discovered_paths

    async def _capture_artifact_baseline(self) -> None:
        self._artifact_baseline_paths = set(await self._list_sandbox_artifacts())

    async def _sync_discovered_artifacts_to_storage(self) -> List[FileInfo]:
        current_paths = await self._list_sandbox_artifacts()
        file_paths = [
            file_path
            for file_path in current_paths
            if file_path not in self._artifact_baseline_paths
        ]
        if not file_paths:
            return []
        file_paths = file_paths[:MAX_AUTO_SYNC_ARTIFACTS]
        return await self._sync_explicit_paths_to_storage(file_paths)
    
    async def _sync_file_to_sandbox(self, file_id: str) -> Optional[FileInfo]:
        """Download file from storage to sandbox"""
        try:
            file_data, file_info = await self._file_storage.download_file(file_id, self._user_id)
            file_path = "/home/ubuntu/upload/" + file_info.filename
            file_data = _rewind_or_buffer_stream(file_data)
            result = await self._sandbox.file_upload(file_data, file_path, filename=file_info.filename)
            if result.success:
                file_info.file_path = file_path
                return file_info
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to sync file: {e}")

    async def _sync_message_attachments_to_storage(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        try:
            if event.attachments:
                attachments.extend(await self._sync_explicit_paths_to_storage([
                    attachment.file_path for attachment in event.attachments
                ]))
            event.attachments = attachments
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to sync attachments to storage: {e}")

    async def _sync_step_attachments_to_storage(self, event: StepEvent) -> None:
        """Sync files explicitly reported by a completed step."""
        try:
            if event.status == StepStatus.COMPLETED and event.step.attachments:
                await self._sync_explicit_paths_to_storage(event.step.attachments)
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to sync step attachments to storage: {e}")

    def _should_attach_generated_files_to_message(self) -> bool:
        """Only the final summary message should render auto-collected files."""
        return getattr(self._flow, "status", None) == AgentStatus.SUMMARIZING

    def _shell_console_for_event(self, console: list, event: ToolEvent) -> list:
        """Return the console slice that belongs to the current shell tool event."""
        if not isinstance(console, list):
            return []
        if event.function_name == "shell_view":
            return console
        command = event.function_args.get("command")
        if command:
            for record in reversed(console):
                if isinstance(record, dict) and record.get("command") == command:
                    return [record]
                if getattr(record, "command", None) == command:
                    return [record]
        return console[-1:] if console else []
    
    async def _sync_message_attachments_to_sandbox(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        try:
            if event.attachments:
                for attachment in event.attachments:
                    file_info = await self._sync_file_to_sandbox(attachment.file_id)
                    if file_info:
                        attachments.append(file_info)
                        await self._session_repository.add_file(self._session_id, file_info)
                    else:
                        attachments.append(attachment)
                        logger.warning(
                            "Agent %s kept unsynced attachment %s (%s)",
                            self._agent_id,
                            attachment.file_id,
                            attachment.filename,
                        )
            event.attachments = attachments
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to sync attachments to event: {e}")
    

    # TODO: refactor this function
    async def _handle_tool_event(self, event: ToolEvent):
        """Generate tool content"""
        try:
            if event.status == ToolStatus.CALLED:
                if event.tool_name == "browser":
                    event.tool_content = BrowserToolContent(screenshot=await self._get_browser_screenshot())
                elif event.tool_name == "search":
                    search_results: ToolResult[SearchResults] = event.function_result
                    logger.debug(f"Search tool results: {search_results}")
                    event.tool_content = SearchToolContent(results=search_results.data.results)
                elif event.tool_name == "shell":
                    if "id" in event.function_args:
                        shell_result = await self._sandbox.view_shell(event.function_args["id"], console=True)
                        console = self._shell_console_for_event(shell_result.data.get("console", []), event)
                        event.tool_content = ShellToolContent(console=console)
                    else:
                        event.tool_content = ShellToolContent(console="(No Console)")
                elif event.tool_name == "file":
                    if event.function_name == "file_find_by_name":
                        event.tool_content = FileToolContent(content=event.function_result.model_dump_json() if hasattr(event.function_result, "model_dump_json") else str(event.function_result))
                    elif event.function_name == "file_find_in_content":
                        event.tool_content = FileToolContent(content=event.function_result.model_dump_json() if hasattr(event.function_result, "model_dump_json") else str(event.function_result))
                    elif "file" in event.function_args:
                        file_path = event.function_args["file"]
                        file_read_result = await self._sandbox.file_read(file_path)
                        file_content: str = file_read_result.data.get("content", "")
                        event.tool_content = FileToolContent(content=file_content)
                        if not self._is_data_center_dataset_path(file_path):
                            file_info = await self._sync_file_to_storage(file_path)
                            self._remember_generated_file(file_info)
                    else:
                        event.tool_content = FileToolContent(content="(No Content)")
                elif event.tool_name == "mcp":
                    logger.debug(f"Processing MCP tool event: function_result={event.function_result}")
                    if event.function_result:
                        if hasattr(event.function_result, 'data') and event.function_result.data:
                            logger.debug(f"MCP tool result data: {event.function_result.data}")
                            event.tool_content = McpToolContent(result=event.function_result.data)
                        elif hasattr(event.function_result, 'success') and event.function_result.success:
                            logger.debug(f"MCP tool result (success, no data): {event.function_result}")
                            result_data = event.function_result.model_dump() if hasattr(event.function_result, 'model_dump') else str(event.function_result)
                            event.tool_content = McpToolContent(result=result_data)
                        else:
                            logger.debug(f"MCP tool result (fallback): {event.function_result}")
                            event.tool_content = McpToolContent(result=str(event.function_result))
                    else:
                        logger.warning("MCP tool: No function_result found")
                        event.tool_content = McpToolContent(result="No result available")
                    
                    logger.debug(f"MCP tool_content set to: {event.tool_content}")
                    if event.tool_content:
                        logger.debug(f"MCP tool_content.result: {event.tool_content.result}")
                        logger.debug(f"MCP tool_content dict: {event.tool_content.model_dump()}")
                elif event.tool_name == "skill":
                    if event.function_result:
                        if hasattr(event.function_result, 'data') and event.function_result.data:
                            event.tool_content = SkillToolContent(result=event.function_result.data)
                        elif hasattr(event.function_result, 'success') and event.function_result.success:
                            result_data = event.function_result.model_dump() if hasattr(event.function_result, 'model_dump') else str(event.function_result)
                            event.tool_content = SkillToolContent(result=result_data)
                        else:
                            event.tool_content = SkillToolContent(result=str(event.function_result))
                    else:
                        event.tool_content = SkillToolContent(result="No result available")
                elif event.tool_name == "message":
                    # Progress/user-interaction events are already represented by their
                    # own message stream and do not need additional tool content.
                    pass
                else:
                    logger.warning(f"Agent {self._agent_id} received unknown tool event: {event.tool_name}")
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} failed to generate tool content: {e}")

    async def run(self, task: Task) -> None:
        """Process agent's message queue and run the agent's flow"""
        try:
            logger.info(f"Agent {self._agent_id} message processing task started")
            await self._sandbox.ensure_sandbox()
            artifact_baseline_initialized = False
            while not await task.input_stream.is_empty():
                event = await self._pop_event(task)
                message = ""
                metadata = {}
                if isinstance(event, MessageEvent):
                    message = event.message or ""
                    metadata = event.metadata or {}
                    await self._sync_message_attachments_to_sandbox(event)
                dataset_service = getattr(self, "_dataset_service", None)
                if dataset_service is None:
                    dataset_service = DataCenterDatasetService()
                    self._dataset_service = dataset_service
                mounted_dataset_ids = getattr(self, "_mounted_dataset_ids", None)
                if mounted_dataset_ids is None:
                    mounted_dataset_ids = set()
                    self._mounted_dataset_ids = mounted_dataset_ids
                requested_dataset_ids = metadata.get("dataset_ids", []) or sorted(mounted_dataset_ids)
                datasets = (
                    await dataset_service.mounted_datasets(
                        requested_dataset_ids,
                        user_id=self._user_id,
                    )
                    if requested_dataset_ids
                    else []
                )
                mounted_dataset_ids.update(item.dataset_id for item in datasets)
                self._remember_mounted_dataset_paths(datasets)
                # Capture the baseline after catalog files have been materialized;
                # otherwise the read-only source files look like new artifacts.
                if not artifact_baseline_initialized:
                    await self._capture_artifact_baseline()
                    artifact_baseline_initialized = True
                    
                logger.info(f"Agent {self._agent_id} received new message: {message[:50]}...")

                sandbox_attachment_paths = [
                    attachment.file_path
                    for attachment in (event.attachments or [])
                    if attachment.file_path
                ]
                logger.info(
                    "Agent %s message attachments: request=%d file_ids=%d sandbox_paths=%d",
                    self._agent_id,
                    len(event.attachments or []),
                    len([attachment for attachment in (event.attachments or []) if attachment.file_id]),
                    len(sandbox_attachment_paths),
                )
                message_obj = Message(
                    message=message,
                    attachments=sandbox_attachment_paths,
                    attachment_file_ids=[
                        attachment.file_id
                        for attachment in (event.attachments or [])
                        if attachment.file_id
                    ],
                    attachment_file_infos=list(event.attachments or []),
                    skills=metadata.get("skills", []),
                    mcp_servers=metadata.get("mcp_servers", []),
                    datasets=datasets,
                    mcp_access_all=bool(metadata.get("mcp_access_all", False)),
                )

                async for event in self._run_flow(message_obj):
                    await self._put_and_add_event(task, event)
                    if isinstance(event, TitleEvent):
                        await self._session_repository.update_title(self._session_id, event.title)
                    elif isinstance(event, MessageEvent):
                        await self._session_repository.update_latest_message(self._session_id, event.message, event.timestamp)
                        await self._session_repository.increment_unread_message_count(self._session_id)
                    elif isinstance(event, WaitEvent):
                        await self._session_repository.update_status(self._session_id, SessionStatus.WAITING)
                        return

            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except asyncio.CancelledError:
            logger.info(f"Agent {self._agent_id} task cancelled")
            await self._put_and_add_event(task, DoneEvent())
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except Exception as e:
            logger.exception(f"Agent {self._agent_id} task encountered exception: {str(e)}")
            
            # If debugger is attached, trigger breakpoint for debugging
            # You can also manually set ENABLE_DEBUG_BREAK=1 environment variable
            if debugpy.is_client_connected() or os.getenv('ENABLE_DEBUG_BREAK'):
                logger.debug("Debugger detected, triggering breakpoint")
                import traceback
                traceback.print_exc()
                debugpy.breakpoint()  # This will pause execution if a debugger is attached
            
            await self._put_and_add_event(task, ErrorEvent(error=f"Task error: {str(e)}"))
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)

    async def _initialize_mcp_tool(self, selected_servers: List[str], *, is_admin: bool = False) -> None:
        await self._mcp_tool.cleanup()
        available_config = await self._mcp_repository.get_mcp_config()
        accessible_servers = {
            name: server_config
            for name, server_config in available_config.mcpServers.items()
            if can_access_mcp(server_config, self._user_id, is_admin=is_admin)
        }
        config = MCPConfig(mcpServers=accessible_servers)
        selected = set(selected_servers)
        if selected:
            config = MCPConfig(
                mcpServers={
                    name: server_config
                    for name, server_config in accessible_servers.items()
                    if name in selected
                }
            )
        else:
            config = MCPConfig(mcpServers={})
        await self._mcp_tool.initialized(config, available_config=available_config)
    
    async def _run_flow(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """Process a single message through the agent's flow and yield events"""
        if not message.message:
            logger.warning(f"Agent {self._agent_id} received empty message")
            yield ErrorEvent(error="No message")
            return

        review = await self._safety_reviewer.review(
            message.message,
            await self._attachment_review_excerpts(message),
        )
        await self._record_safety_audit(review)
        if not review.allowed:
            logger.warning(
                "Agent %s rejected user message before Planner: risk=%s categories=%s",
                self._agent_id,
                review.risk_level,
                ",".join(review.categories),
            )
            yield MessageEvent(
                role="assistant",
                message=(
                    "请求未通过安全审核。\n\n"
                    f"判定原因：{review.reason or '请求命中了系统安全策略。'}\n\n"
                    f"修改建议：{review.suggestion or '请移除可能违规或越权的内容后重试。'}"
                ),
                metadata={
                    "safety_review": {
                        "decision": review.decision,
                        "risk_level": review.risk_level,
                        "categories": review.categories,
                        "reason": review.reason,
                        "suggestion": review.suggestion,
                    }
                },
            )
            yield DoneEvent()
            return

        await self._initialize_mcp_tool(message.mcp_servers, is_admin=message.mcp_access_all)

        async for event in self._flow.run(message):
            if isinstance(event, ToolEvent):
                # TODO: move to tool function
                await self._handle_tool_event(event)
                if event.status == ToolStatus.CALLED:
                    await self._sync_discovered_artifacts_to_storage()
            elif isinstance(event, StepEvent):
                await self._sync_step_attachments_to_storage(event)
                if event.status == StepStatus.COMPLETED:
                    await self._sync_discovered_artifacts_to_storage()
            elif isinstance(event, MessageEvent):
                if self._should_attach_generated_files_to_message():
                    await self._sync_discovered_artifacts_to_storage()
                await self._sync_message_attachments_to_storage(event)
                if (
                    not event.attachments
                    and self._generated_files
                    and self._should_attach_generated_files_to_message()
                ):
                    event.attachments = self._generated_files
            elif isinstance(event, DoneEvent):
                await self._sync_discovered_artifacts_to_storage()
                session = await self._session_repository.find_by_id(self._session_id)
                if session:
                    try:
                        events = await self._session_repository.get_events(self._session_id)
                        advice = await self._completion_advice_service.analyze(events)
                        event.advice = self._completion_advice_service.to_payload(advice)
                    except Exception as exc:
                        logger.warning("Failed to build completion advice for session %s: %s", self._session_id, exc)
            yield event

        logger.info(f"Agent {self._agent_id} completed processing one message")

    async def _attachment_review_excerpts(self, message: Message) -> list[dict[str, str]]:
        """Read small text excerpts for review without executing attachments."""
        excerpts: list[dict[str, str]] = []
        for info in message.attachment_file_infos[:10]:
            item = {"filename": info.filename or "unknown", "content": ""}
            path = info.file_path
            suffix = PurePosixPath(info.filename or path or "").suffix.lower()
            reviewable_text = (info.content_type or "").startswith(("text/", "application/json")) or suffix in {
                ".csv", ".json", ".log", ".md", ".py", ".sh", ".txt", ".xml", ".yaml", ".yml",
            }
            if path and reviewable_text:
                try:
                    result = await self._sandbox.file_read(path)
                    content = (result.data or {}).get("content", "") if result else ""
                    item["content"] = str(content)[:8000]
                except Exception as exc:
                    logger.info("Safety review could not read attachment %s: %s", info.filename, exc)
            excerpts.append(item)
        return excerpts

    async def _record_safety_audit(self, review: SafetyReview) -> None:
        risk_level = {
            "low": AuditRiskLevel.LOW,
            "medium": AuditRiskLevel.MEDIUM,
            "high": AuditRiskLevel.HIGH,
            "critical": AuditRiskLevel.CRITICAL,
        }[review.risk_level]
        try:
            await self._audit_service.record(
                actor_user_id=self._user_id,
                action="agent_message.safety_review",
                resource_type="session",
                resource_id=self._session_id,
                session_id=self._session_id,
                status=AuditStatus.SUCCESS if review.allowed else AuditStatus.DENIED,
                risk_level=risk_level,
                metadata={
                    "decision": review.decision,
                    "categories": review.categories,
                    "reason": review.reason,
                    "suggestion": review.suggestion,
                },
            )
        except Exception as exc:
            logger.warning("Failed to persist safety review audit for session %s: %s", self._session_id, exc)

    async def on_done(self, task: Task) -> None:
        """Called when the task is done"""
        logger.info(f"Agent {self._agent_id} task done")
        if self._browser and hasattr(self._browser, "cleanup"):
            try:
                await self._browser.cleanup()
            except Exception as exc:
                logger.warning("Agent %s failed to cleanup browser before pausing sandbox: %s", self._agent_id, exc)
        if self._sandbox and hasattr(self._sandbox, "pause"):
            paused = await self._sandbox.pause()
            if paused:
                logger.info("Agent %s paused sandbox %s after task completion", self._agent_id, self._sandbox.id)
            else:
                logger.warning("Agent %s failed to pause sandbox %s after task completion", self._agent_id, self._sandbox.id)


    async def destroy(self) -> None:
        """Destroy the task and release resources"""
        logger.info("Starting to destroy agent task")
        
        # Destroy sandbox environment
        if self._sandbox:
            logger.debug(f"Destroying Agent {self._agent_id}'s sandbox environment")
            await self._sandbox.destroy()
        
        if self._mcp_tool:
            logger.debug(f"Destroying Agent {self._agent_id}'s MCP tool")
            await self._mcp_tool.cleanup()
        
        logger.debug(f"Agent {self._agent_id} has been fully closed and resources cleared")
