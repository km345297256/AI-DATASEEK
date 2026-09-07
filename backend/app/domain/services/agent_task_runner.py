from typing import Any, Optional, AsyncGenerator, List
import asyncio
import hashlib
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
from app.domain.utils.public_error import public_error_message
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow
from app.domain.external.sandbox import Sandbox
from app.domain.external.browser import Browser
from app.domain.external.search import SearchEngine
from app.domain.external.file import FileStorage
from app.domain.external.plugin_runtime import PluginRuntime
from app.domain.external.spill import SpillArtifactStore
from app.domain.repositories.agent_repository import AgentRepository
from app.domain.external.task import TaskRunner, Task
from app.domain.repositories.session_repository import SessionRepository
from app.domain.repositories.mcp_repository import MCPRepository
from app.domain.models.session import SessionStatus
from app.domain.models.file import FileInfo
from app.domain.models.execution_environment import ExecutionEnvironmentSnapshot
from app.domain.services.execution_environment import create_agent_execution_snapshot
from app.domain.services.model_runtime import ModelBudgetStopped, model_execution_scope, model_stop_reason
from app.infrastructure.repositories.mongo_model_trace_repository import get_model_trace_repository
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.services.tools.interceptors import (
    AuditServiceToolTraceSink,
    CompositeToolTraceSink,
    JsonLoggingToolTraceSink,
)
from app.domain.services.tools.pipeline import opaque_log_identifier
from app.domain.services.tools.spill import SPILL_READ_TOOL_NAME
from app.domain.services.tools.analysis_job import AnalysisJobCancelled, AnalysisJobInterrupted
from app.domain.services.tools.authorization import ToolAuthorizationStopped
from app.domain.services.tools.spill_projection import (
    durable_spill_read_projection,
    durable_spill_result_projection,
    sanitize_spill_public_data,
    sanitize_spill_public_text,
    spill_notice_from_result,
)
from app.domain.models.tool_result import ToolResult
from app.domain.models.analysis_job import AnalysisJobView
from app.domain.models.search import SearchResults
from app.domain.models.mcp_config import MCPConfig, can_access_mcp
from app.domain.services.completion_advice_service import get_completion_advice_service
from app.domain.services.safety.policy import deterministic_review
from app.domain.services.safety.policy_store import get_safety_policy_store
from app.domain.services.audit_service import AuditService
from app.domain.models.audit import AuditRiskLevel, AuditStatus
from app.domain.models.safety import SafetyReview
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.application.services.dataset_request_resolver import FrontControllerResolution
from app.infrastructure.external.sso_client import record_analysis_tool_usage

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
        content = file_data.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        return io.BytesIO(content)
    return file_data


# Agents are instructed to publish generated outputs here.  Keeping automatic
# discovery inside this boundary avoids recursively walking datasets, package
# caches and other sandbox working files.  Explicit step/message attachments
# remain supported outside this directory.
ARTIFACT_SEARCH_ROOTS = ("/home/ubuntu/output",)
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
    ".prj",
    ".py",
    ".rar",
    ".shp",
    ".shx",
    ".dbf",
    ".cpg",
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
    # `dataset_unpack` uses these as private working trees. Publishing them
    # would duplicate mounted source data and can dominate artifact latency.
    "unpacked",
    "unpacked_archives",
    "upload",
}
MAX_AUTO_SYNC_ARTIFACTS = 500
MAX_EVENT_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_EVENT_PREVIEW_BYTES = 256 * 1024
ARTIFACT_HASH_METADATA_KEY = "artifact_sha256"
ARTIFACT_SIZE_METADATA_KEY = "artifact_size"
ARTIFACT_HASH_CHUNK_BYTES = 1024 * 1024

ArtifactFingerprint = tuple[int, str]


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
        front_controller_resolution: Optional[FrontControllerResolution] = None,
        plugin_runtime: Optional[PluginRuntime] = None,
        spill_artifact_store: Optional[SpillArtifactStore] = None,
        analysis_job_service=None,
        tool_approval_service=None,
        credential_service=None,
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
        self._plugin_runtime = plugin_runtime
        self._spill_artifact_store = spill_artifact_store
        self._analysis_job_service = analysis_job_service
        self._analysis_job_tools: dict[str, ToolEvent] = {}
        self._analysis_job_views: dict[str, AnalysisJobView] = {}
        self._tool_approval_service = tool_approval_service
        self._tool_approval_views: dict[str, Any] = {}
        self._llm_overrides = dict(llm_overrides or {})
        self._execution_snapshot: ExecutionEnvironmentSnapshot | None = None
        self._mcp_tool = MCPToolkit()
        self._front_controller_resolution = front_controller_resolution
        self._safety_policy_store = get_safety_policy_store()
        self._audit_service = AuditService()
        self._tool_trace_sink = CompositeToolTraceSink((
            JsonLoggingToolTraceSink(),
            AuditServiceToolTraceSink(
                self._audit_service,
                actor_user_id=self._user_id,
                session_id=self._session_id,
            ),
        ))
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
            plugin_runtime=plugin_runtime,
            spill_artifact_store=spill_artifact_store,
            analysis_job_service=analysis_job_service,
            tool_approval_service=tool_approval_service,
            credential_service=credential_service,
        )
        self._generated_files: List[FileInfo] = []
        self._artifact_baseline_paths: set[str] = set()
        self._artifact_fingerprints: dict[str, ArtifactFingerprint] = {}
        self._dataset_service = DataCenterDatasetService()
        self._mounted_dataset_ids: set[str] = set()
        self._active_datasets: list[Any] = []
        self._reported_analysis_tool_usage: set[tuple[str, str, str]] = set()
        # Only files materialized from the data-center catalog are protected from
        # attachment publication.  Generated sidecars (reports, previews, etc.)
        # in the same directory must remain publishable artifacts.
        self._protected_dataset_paths: set[str] = set()
        self._protected_dataset_roots: set[str] = set()
        # Tool-owned working trees (for example recursive archive extraction)
        # are evidence inputs, not generated deliverables. Track their exact
        # roots from tool arguments instead of relying on a directory name.
        self._private_artifact_roots: set[str] = set()

    async def _record_execution_snapshot(
        self,
        *,
        task_id: str,
        message: Message,
        trigger_event_seq: int | None = None,
    ) -> ExecutionEnvironmentSnapshot:
        dataset_ids = [
            getattr(dataset, "dataset_id", None)
            for dataset in (message.datasets or [])
            if getattr(dataset, "dataset_id", None)
        ]
        snapshot = create_agent_execution_snapshot(
            task_id=task_id,
            session_id=self._session_id,
            flow=self._flow,
            sandbox=self._sandbox,
            dataset_ids=dataset_ids,
            resolution=self._front_controller_resolution,
            llm_overrides=self._llm_overrides,
            requested_mcp_servers=message.mcp_servers or [],
            requested_skill_count=len(set(message.skills or [])),
            trigger_event_seq=trigger_event_seq,
        )
        existing = self._execution_snapshot
        if existing is not None:
            if (
                existing.task_id != task_id
                or existing.fingerprint != snapshot.fingerprint
                or existing.trigger_event_seq != snapshot.trigger_event_seq
            ):
                raise RuntimeError("Task execution environment changed after it was frozen")
            return existing

        persist = getattr(self._session_repository, "add_execution_snapshot", None)
        if callable(persist):
            try:
                await persist(snapshot)
            except Exception as exc:
                logger.error(
                    "Failed to persist execution snapshot session=%s task=%s error_type=%s",
                    opaque_log_identifier(self._session_id, namespace="session"),
                    opaque_log_identifier(task_id, namespace="task"),
                    type(exc).__name__,
                )
                raise RuntimeError(
                    "The task execution environment could not be recorded"
                ) from exc
        else:
            # Backward compatibility for isolated test repositories. The
            # production Mongo repository always implements this operation.
            logger.warning(
                "Session repository has no execution snapshot store session=%s",
                opaque_log_identifier(self._session_id, namespace="session"),
            )
        self._execution_snapshot = snapshot
        return snapshot

    def _execution_task_id(self, task: Task) -> str | None:
        """Return a production task id, allowing only legacy test doubles to omit it."""
        task_id = getattr(task, "id", None)
        if isinstance(task_id, str) and task_id.strip():
            return task_id
        if callable(getattr(self._session_repository, "add_execution_snapshot", None)):
            raise RuntimeError("Persisted agent task is missing its task id")
        logger.warning(
            "Legacy untracked agent task session=%s",
            opaque_log_identifier(self._session_id, namespace="session"),
        )
        return None

    async def _put_and_add_event(self, task: Task, event: AgentEvent) -> None:
        if isinstance(event, ToolEvent):
            approvals = getattr(self, "_tool_approval_views", {})
            if event.tool_approval is None and event.tool_call_id in approvals:
                event = event.model_copy(update={"tool_approval": approvals[event.tool_call_id]})
            views = getattr(self, "_analysis_job_views", {})
            if event.analysis_job is None:
                if event.status == ToolStatus.CALLING:
                    templates = getattr(self, "_analysis_job_tools", None)
                    if templates is not None:
                        templates[event.tool_call_id] = event.model_copy()
                if event.tool_call_id in views:
                    event = event.model_copy(update={"analysis_job": views[event.tool_call_id]})
        event = self._durable_event_projection(event)
        event = self._bound_event_payload(event)
        event.bind_producer_event_id()
        reserve_sequence = getattr(
            self._session_repository,
            "reserve_event_sequence",
            None,
        )
        if callable(reserve_sequence):
            # The sequence must be inside the Redis payload; assigning it after
            # XADD would make live events and persisted history disagree.
            await reserve_sequence(self._session_id, event)
        event_id = await task.output_stream.put(event.model_dump_json())
        event.id = event_id
        await self._session_repository.add_event(self._session_id, event)

    async def _publish_tool_approval(self, task: Task, view, context) -> None:
        call_id = str(context.tool_call_id)
        previous = self._tool_approval_views.get(call_id)
        if previous is not None and previous.approval_id == view.approval_id and previous.revision >= view.revision:
            return
        self._tool_approval_views[call_id] = view
        template = self._analysis_job_tools.get(call_id)
        await self._put_and_add_event(task, ToolEvent(
            tool_call_id=call_id,
            tool_name=template.tool_name if template is not None else "plugin",
            function_name=template.function_name if template is not None else context.tool_name,
            function_args=sanitize_spill_public_data(template.function_args) if template is not None else {},
            status=ToolStatus.CALLED if view.status in {"rejected", "expired", "cancelled"} else ToolStatus.CALLING,
            tool_approval=view,
        ))

    async def _publish_analysis_job(self, task: Task, view: AnalysisJobView, context) -> None:
        call_id = str(context.tool_call_id)
        previous = self._analysis_job_views.get(call_id)
        if previous is not None and previous.job_id == view.job_id and previous.revision >= view.revision:
            return
        self._analysis_job_views[call_id] = view
        template = self._analysis_job_tools.get(call_id)
        terminal = view.status in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
        # Only display arguments already emitted by the Agent are repeated.
        # Compiled analysis intentionally emits a short label, never its code.
        await self._put_and_add_event(task, ToolEvent(
            tool_call_id=call_id,
            tool_name=template.tool_name if template is not None else "plugin",
            function_name=template.function_name if template is not None else context.tool_name,
            function_args=sanitize_spill_public_data(template.function_args) if template is not None else {},
            status=ToolStatus.CALLED if terminal else ToolStatus.CALLING,
            analysis_job=view,
        ))

    @staticmethod
    def _durable_event_projection(event: AgentEvent) -> AgentEvent:
        """Clone and sanitize a spill event at the Redis/Mongo boundary.

        The Agent may use the raw tool result during its current invocation,
        while durable history retains only the bounded artifact reference and
        a credential/path-safe preview.  Non-spill events are unchanged.
        """

        if not isinstance(event, ToolEvent):
            return event
        is_spill_read = event.function_name == SPILL_READ_TOOL_NAME
        if not is_spill_read and spill_notice_from_result(event.function_result) is None:
            return event

        # Shallow clone first: untrusted arguments may be deeply nested or
        # cyclic. The bounded sanitizer creates their independent safe copy.
        durable = event.model_copy()
        durable.function_result = (
            durable_spill_read_projection(event.function_result)
            if is_spill_read
            else durable_spill_result_projection(event.function_result)
        )
        safe_args = sanitize_spill_public_data(durable.function_args)
        durable.function_args = safe_args if isinstance(safe_args, dict) else {}
        # A spill has one universal typed UI card. The declarative descriptor
        # is redundant and could otherwise consume the complete event budget.
        durable.presentation = None

        if is_spill_read:
            durable.tool_content = None
            return durable

        content = durable.tool_content
        if isinstance(content, FileToolContent):
            durable.tool_content = content.model_copy(update={
                "content": sanitize_spill_public_text(content.content),
            })
        elif isinstance(content, ShellToolContent):
            durable.tool_content = content.model_copy(update={
                "console": sanitize_spill_public_data(content.console),
            })
        elif isinstance(content, McpToolContent):
            durable.tool_content = content.model_copy(update={
                "result": sanitize_spill_public_data(content.result),
            })
        elif isinstance(content, SkillToolContent):
            durable.tool_content = content.model_copy(update={
                "result": sanitize_spill_public_data(content.result),
            })
        return durable

    def _bound_event_payload(self, event: AgentEvent) -> AgentEvent:
        """Keep Redis and Mongo session-event documents well below BSON's 16MB limit."""
        if self._event_payload_size(event) <= self.MAX_EVENT_PAYLOAD_BYTES:
            return event

        logger.warning(
            "Bounded oversized event before persistence agent=%s event_type=%s bytes=%d",
            opaque_log_identifier(self._agent_id, namespace="agent"),
            event.type,
            self._event_payload_size(event),
        )
        if isinstance(event, ToolEvent):
            spill_notice = spill_notice_from_result(event.function_result)
            event.function_args = self._event_preview(event.function_args)
            # A spill notice is already bounded and is the sole route back to
            # the complete output. Preserve it when oversized arguments or UI
            # content trigger this final BSON/Redis defense.
            if spill_notice is None:
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
            "Event remained oversized after bounding; replacing it agent=%s",
            opaque_log_identifier(self._agent_id, namespace="agent"),
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
        return self._decode_input_event(event_id, event_str)

    def _decode_input_event(self, event_id, event_str) -> AgentEvent:
        if event_str is None:
            logger.warning(
                "Agent received empty message agent=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
            )
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

    def _artifact_fingerprint_state(self) -> dict[str, ArtifactFingerprint]:
        """Return fingerprint state, including for runners built directly in tests."""
        state = getattr(self, "_artifact_fingerprints", None)
        if state is None:
            state = {}
            self._artifact_fingerprints = state
        return state

    def _artifact_baseline_state(self) -> set[str]:
        baseline = getattr(self, "_artifact_baseline_paths", None)
        if baseline is None:
            baseline = set()
            self._artifact_baseline_paths = baseline
        return baseline

    @staticmethod
    def _fingerprint_stream(file_data) -> tuple[Any, ArtifactFingerprint]:
        """Hash a downloaded stream without retaining a second full-size copy."""
        stream = _rewind_or_buffer_stream(file_data)
        if not hasattr(stream, "read"):
            raise TypeError("Downloaded artifact is not a readable stream")

        try:
            stream.seek(0)
        except (AttributeError, OSError, io.UnsupportedOperation):
            content = stream.read()
            if isinstance(content, str):
                content = content.encode("utf-8")
            stream = io.BytesIO(content)

        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = stream.read(ARTIFACT_HASH_CHUNK_BYTES)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            digest.update(chunk)
            size += len(chunk)
        stream.seek(0)
        return stream, (size, digest.hexdigest())

    async def _read_artifact_with_fingerprint(
        self,
        file_path: str,
    ) -> tuple[Any, ArtifactFingerprint]:
        file_data = await self._sandbox.file_download(file_path)
        return self._fingerprint_stream(file_data)

    @staticmethod
    def _file_matches_fingerprint(
        file_info: Optional[FileInfo],
        fingerprint: ArtifactFingerprint,
    ) -> bool:
        if not file_info or not file_info.metadata:
            return False
        expected_size, expected_hash = fingerprint
        metadata = file_info.metadata
        try:
            stored_size = int(metadata.get(ARTIFACT_SIZE_METADATA_KEY))
        except (TypeError, ValueError):
            return False
        return (
            stored_size == expected_size
            and metadata.get(ARTIFACT_HASH_METADATA_KEY) == expected_hash
        )

    def _remember_artifact_fingerprint(
        self,
        file_path: str,
        fingerprint: ArtifactFingerprint,
    ) -> None:
        self._artifact_baseline_state().add(file_path)
        self._artifact_fingerprint_state()[file_path] = fingerprint

    @staticmethod
    def _fingerprint_from_file_info(
        file_info: Optional[FileInfo],
    ) -> Optional[ArtifactFingerprint]:
        """Read a durable artifact fingerprint without downloading its body."""
        if not file_info or not file_info.metadata:
            return None
        try:
            artifact_size = int(file_info.metadata.get(ARTIFACT_SIZE_METADATA_KEY))
        except (TypeError, ValueError):
            return None
        artifact_hash = file_info.metadata.get(ARTIFACT_HASH_METADATA_KEY)
        if not artifact_hash:
            return None
        return artifact_size, str(artifact_hash)

    def _can_delete_replaced_storage_file(self, file_info: Optional[FileInfo]) -> bool:
        """Only delete storage objects that this session created as artifacts."""
        if not file_info or not file_info.file_id or not file_info.metadata:
            return False
        metadata = file_info.metadata
        return (
            metadata.get("source") == "sandbox_artifact"
            and str(metadata.get("session_id") or "") == str(self._session_id)
        )

    async def _delete_replaced_storage_file(self, file_info: Optional[FileInfo]) -> None:
        if not self._can_delete_replaced_storage_file(file_info):
            return
        try:
            deleted = await self._file_storage.delete_file(file_info.file_id, self._user_id)
            if not deleted:
                logger.warning(
                    "Could not delete replaced artifact object agent=%s object=%s",
                    opaque_log_identifier(self._agent_id, namespace="agent"),
                    opaque_log_identifier(file_info.file_id, namespace="object"),
                )
        except Exception as exc:
            # The new attachment is already durable.  Object cleanup is
            # intentionally best-effort and must not make the task fail.
            logger.warning(
                "Failed to delete replaced artifact object agent=%s object=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                opaque_log_identifier(file_info.file_id, namespace="object"),
                type(exc).__name__,
            )

    async def _sync_file_to_storage(
        self,
        file_path: str,
        *,
        file_data=None,
        fingerprint: Optional[ArtifactFingerprint] = None,
    ) -> Optional[FileInfo]:
        """Upload a changed file once and return its current FileInfo."""
        try:
            if not file_path:
                return None
            if file_data is None:
                file_data, fingerprint = await self._read_artifact_with_fingerprint(file_path)
            elif fingerprint is None:
                file_data, fingerprint = self._fingerprint_stream(file_data)
            else:
                file_data = _rewind_or_buffer_stream(file_data)
            assert fingerprint is not None

            existing_file = await self._session_repository.get_file_by_path(
                self._session_id,
                file_path,
            )
            if self._file_matches_fingerprint(existing_file, fingerprint):
                existing_file.file_path = file_path
                self._remember_artifact_fingerprint(file_path, fingerprint)
                return existing_file

            file_name = file_path.split("/")[-1]
            artifact_size, artifact_hash = fingerprint
            storage_metadata = {
                "session_id": self._session_id,
                "file_path": file_path,
                "source": "sandbox_artifact",
                ARTIFACT_SIZE_METADATA_KEY: artifact_size,
                ARTIFACT_HASH_METADATA_KEY: artifact_hash,
            }
            file_info = await self._upload_file_to_storage(
                file_data,
                file_name,
                metadata=storage_metadata,
            )
            file_info.file_path = file_path
            file_info.metadata = {**(file_info.metadata or {}), **storage_metadata}
            # Upload first so a transient storage failure cannot remove the last
            # working attachment reference from the session.
            await self._session_repository.add_file(self._session_id, file_info)
            if existing_file and existing_file.file_id:
                await self._session_repository.remove_file(
                    self._session_id,
                    existing_file.file_id,
                )
                await self._delete_replaced_storage_file(existing_file)
            # Only advance the baseline after both storage and repository writes
            # have succeeded; otherwise the next discovery pass must retry.
            self._remember_artifact_fingerprint(file_path, fingerprint)
            return file_info
        except Exception as e:
            logger.error(
                "Failed to sync artifact file agent=%s file=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                opaque_log_identifier(file_path, namespace="file"),
                type(e).__name__,
            )

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

    @staticmethod
    def _file_delivery_key(file_info: FileInfo) -> str:
        return str(file_info.file_id or file_info.file_path or file_info.filename)

    @classmethod
    def _unique_files(cls, files: List[FileInfo]) -> List[FileInfo]:
        unique: List[FileInfo] = []
        seen: set[str] = set()
        for file_info in files:
            key = cls._file_delivery_key(file_info)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(file_info)
        return unique

    def _is_syncable_artifact(self, file_path: str) -> bool:
        if not file_path:
            return False
        path = PurePosixPath(file_path)
        if path.name == "result.json" and any(
            part.startswith("analysis-") for part in path.parts
        ):
            return False
        if any(part in ARTIFACT_EXCLUDED_PARTS for part in path.parts):
            return False
        if self._is_private_artifact_path(file_path):
            return False
        return path.suffix.lower() in ARTIFACT_EXTENSIONS

    def _is_private_artifact_path(self, file_path: str) -> bool:
        if not file_path:
            return False
        path = PurePosixPath(file_path)
        return any(
            path == PurePosixPath(root) or PurePosixPath(root) in path.parents
            for root in getattr(self, "_private_artifact_roots", set())
        )

    def _remember_private_tool_output(self, event: ToolEvent) -> None:
        """Exclude exact dataset-unpack working roots from artifact delivery."""
        if event.function_name != "dataset_unpack":
            return
        output_dir = (event.function_args or {}).get("output_dir")
        if not isinstance(output_dir, str):
            return
        path = PurePosixPath(output_dir)
        output_root = PurePosixPath("/home/ubuntu/output")
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path == output_root
            or not path.is_relative_to(output_root)
        ):
            return
        roots = getattr(self, "_private_artifact_roots", None)
        if roots is None:
            roots = set()
            self._private_artifact_roots = roots
        roots.add(str(path))

    @staticmethod
    def _is_in_artifact_search_roots(file_path: str) -> bool:
        path = PurePosixPath(file_path)
        if not path.is_absolute() or ".." in path.parts:
            return False
        return any(
            path == PurePosixPath(root) or PurePosixPath(root) in path.parents
            for root in ARTIFACT_SEARCH_ROOTS
        )

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
            if (
                not file_path
                or file_path in seen_paths
                or self._is_data_center_dataset_path(file_path)
                or self._is_private_artifact_path(file_path)
            ):
                continue
            seen_paths.add(file_path)
            file_info = await self._sync_file_to_storage(file_path)
            if file_info:
                attachments.append(file_info)
                self._remember_generated_file(file_info)
        return attachments

    def _known_generated_file(self, file_path: str) -> Optional[FileInfo]:
        """Return a file already synchronized during this runner lifecycle."""
        if file_path not in self._artifact_fingerprint_state():
            return None
        return next(
            (
                item
                for item in reversed(self._generated_files)
                if item.file_path == file_path and item.file_id
            ),
            None,
        )

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
                        or not self._is_in_artifact_search_roots(file_path)
                        or self._is_data_center_dataset_path(file_path)
                        or not self._is_syncable_artifact(file_path)
                    ):
                        continue
                    seen_paths.add(file_path)
                    discovered_paths.append(file_path)
        except Exception as e:
            logger.error(
                "Failed to list sandbox artifacts agent=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                type(e).__name__,
            )
        return discovered_paths

    async def _capture_artifact_baseline(self) -> None:
        baseline_paths = await self._list_sandbox_artifacts()
        self._artifact_baseline_paths = set(baseline_paths)
        self._artifact_fingerprints = {}
        # Session artifact uploads already carry size + sha256 metadata. Reuse
        # that metadata instead of downloading every historical output. Legacy
        # session files have no metadata, so hash them once at task start; a
        # later overwrite can then be delivered normally.
        files_by_path: dict[str, FileInfo] = {}
        try:
            session = await self._session_repository.find_by_id(self._session_id)
            files_by_path = {
                item.file_path: item
                for item in (getattr(session, "files", None) or [])
                if item.file_path
            }
        except Exception as exc:
            logger.warning(
                "Could not load artifact baseline metadata agent=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                type(exc).__name__,
            )

        for file_path in baseline_paths:
            fingerprint = self._fingerprint_from_file_info(files_by_path.get(file_path))
            if fingerprint is None:
                try:
                    _, fingerprint = await self._read_artifact_with_fingerprint(file_path)
                except Exception as exc:
                    # Retain the path-only compatibility fallback when a legacy
                    # body cannot be read. Discovery will observe it once without
                    # publishing pre-task output.
                    logger.warning(
                        "Could not fingerprint legacy baseline artifact agent=%s file=%s error_type=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        opaque_log_identifier(file_path, namespace="file"),
                        type(exc).__name__,
                    )
                    continue
            self._artifact_fingerprints[file_path] = fingerprint

    async def _sync_discovered_artifacts_to_storage(
        self,
        *,
        skip_paths: Optional[set[str]] = None,
    ) -> List[FileInfo]:
        current_paths = await self._list_sandbox_artifacts()
        current_path_set = set(current_paths)
        baseline = self._artifact_baseline_state()
        fingerprints = self._artifact_fingerprint_state()

        for removed_path in baseline - current_path_set:
            baseline.discard(removed_path)
            fingerprints.pop(removed_path, None)

        attachments: List[FileInfo] = []
        skipped = skip_paths or set()
        for file_path in current_paths:
            if len(attachments) >= MAX_AUTO_SYNC_ARTIFACTS:
                break
            if file_path in skipped:
                continue
            try:
                file_data, fingerprint = await self._read_artifact_with_fingerprint(file_path)
            except Exception as exc:
                logger.warning(
                    "Could not fingerprint artifact agent=%s file=%s error_type=%s",
                    opaque_log_identifier(self._agent_id, namespace="agent"),
                    opaque_log_identifier(file_path, namespace="file"),
                    type(exc).__name__,
                )
                continue

            previous_fingerprint = fingerprints.get(file_path)
            if previous_fingerprint == fingerprint:
                continue
            if file_path in baseline and previous_fingerprint is None:
                # Compatibility for a baseline captured before fingerprinting was
                # available: observe it once without publishing pre-task output.
                fingerprints[file_path] = fingerprint
                continue

            file_info = await self._sync_file_to_storage(
                file_path,
                file_data=file_data,
                fingerprint=fingerprint,
            )
            if file_info:
                attachments.append(file_info)
                self._remember_generated_file(file_info)
        return attachments
    
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
            logger.error(
                "Failed to sync storage file into sandbox agent=%s object=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                opaque_log_identifier(file_id, namespace="object"),
                type(e).__name__,
            )

    async def _sync_message_attachments_to_storage(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        try:
            if event.attachments:
                paths_to_sync: List[str] = []
                for attachment in event.attachments:
                    if not attachment.file_path:
                        continue
                    known = self._known_generated_file(attachment.file_path)
                    if known is not None:
                        attachments.append(known)
                    else:
                        paths_to_sync.append(attachment.file_path)
                attachments.extend(await self._sync_explicit_paths_to_storage(paths_to_sync))
            event.attachments = attachments
        except Exception as e:
            logger.error(
                "Failed to sync attachments to storage agent=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                type(e).__name__,
            )

    async def _sync_step_attachments_to_storage(self, event: StepEvent) -> List[FileInfo]:
        """Sync files explicitly reported by a completed step."""
        try:
            if event.status == StepStatus.COMPLETED and event.step.attachments:
                return await self._sync_explicit_paths_to_storage(event.step.attachments)
        except Exception as e:
            logger.error(
                "Failed to sync step attachments to storage agent=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                type(e).__name__,
            )
        return []

    def _should_attach_generated_files_to_message(self) -> bool:
        """Return whether the current assistant message is the final summary."""
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

    @staticmethod
    def _completed_shell_console_from_result(event: ToolEvent) -> Optional[list[dict[str, Any]]]:
        """Build a durable console snapshot from a completed shell result.

        ``shell_run`` and the bounded dataset shell capabilities already return
        the exact command and its completed output. Prefer that authoritative
        result over a second sandbox lookup, which can race with sandbox pause
        or cleanup after the command has finished.
        """
        notice = spill_notice_from_result(event.function_result)
        if notice is not None:
            projected_result = event.function_result
            if isinstance(projected_result, ToolResult):
                succeeded = projected_result.success
            elif isinstance(projected_result, dict):
                succeeded = projected_result.get("success") is not False
            else:
                succeeded = False
            locator = (
                notice.reference.locator
                if notice.reference is not None
                else "unavailable"
            )
            return [{
                "ps1": "$",
                "command": str((event.function_args or {}).get("command") or event.function_name),
                "output": (
                    f"{sanitize_spill_public_text(notice.preview)}\n\n"
                    f"[Oversized output: {notice.original_bytes} bytes; "
                    f"spill={notice.status}; locator={locator}]"
                ),
                "status": "completed" if succeeded else "failed",
                "returncode": 0 if succeeded else 1,
            }]

        function_result = event.function_result
        if isinstance(function_result, ToolResult):
            result_data = function_result.data
        elif isinstance(function_result, dict):
            nested_data = function_result.get("data")
            result_data = nested_data if isinstance(nested_data, dict) else function_result
        else:
            result_data = getattr(function_result, "data", None)

        if not isinstance(result_data, dict) or result_data.get("status") != "completed":
            return None
        if "output" not in result_data:
            return None

        command = result_data.get("command")
        if not isinstance(command, str) or not command:
            command = (event.function_args or {}).get("command")
        if not isinstance(command, str) or not command:
            return None

        output = result_data.get("output")
        if output is None:
            output = ""
        elif not isinstance(output, str):
            output = str(output)

        return [{
            "ps1": "$",
            "command": command,
            "output": output,
            "status": "completed",
            "returncode": result_data.get("returncode"),
        }]

    @staticmethod
    def _dataset_analysis_console(event: ToolEvent) -> list[dict[str, Any]]:
        """Render the high-level analysis result without exposing generated code."""
        notice = spill_notice_from_result(event.function_result)
        if notice is not None:
            if isinstance(event.function_result, ToolResult):
                success = event.function_result.success
            elif isinstance(event.function_result, dict):
                success = event.function_result.get("success") is not False
            else:
                success = False
            output = (
                "数据集分析已完成；完整输出已保存为当前会话的私有溢出结果。"
                if success and notice.status == "stored"
                else "数据集分析已完成，但完整输出暂时无法保存。"
                if success
                else "数据集分析执行失败；详细输出已保存为当前会话的私有溢出结果。"
                if notice.status == "stored"
                else "数据集分析执行失败，且详细输出暂时无法保存。"
            )
            return [{
                "ps1": "$",
                "command": "分析数据集并生成成果",
                "output": output,
                "status": "completed" if success else "failed",
                "returncode": 0 if success else 1,
            }]
        result = event.function_result if isinstance(event.function_result, dict) else {}
        success = bool(result.get("success"))
        output = result.get("result") if success else result.get("error") or result.get("result")
        if not isinstance(output, str) or not output.strip():
            output = "数据集分析已完成。" if success else "数据集分析未能生成有效结果。"
        return [{
            "ps1": "$",
            "command": "分析数据集并生成成果",
            "output": output.strip(),
            "status": "completed" if success else "failed",
            "returncode": 0 if success else 1,
        }]

    @staticmethod
    def _dataset_quicklook_console(event: ToolEvent) -> list[dict[str, Any]]:
        """Render quicklook's compact summary instead of its full evidence JSON."""
        function_result = event.function_result
        notice = spill_notice_from_result(function_result)
        if notice is not None:
            if isinstance(function_result, ToolResult):
                success = function_result.success
            elif isinstance(function_result, dict):
                success = function_result.get("success") is not False
            else:
                success = False
            output = (
                "数据集快速探查已完成；完整证据已保存为当前会话的私有溢出结果。"
                if success and notice.status == "stored"
                else "数据集快速探查已完成，但完整证据暂时无法保存。"
                if success
                else "数据集快速探查失败；详细输出已保存为当前会话的私有溢出结果。"
                if notice.status == "stored"
                else "数据集快速探查失败，且详细输出暂时无法保存。"
            )
            return [{
                "ps1": "$",
                "command": "快速探查数据集",
                "output": output,
                "status": "completed" if success else "failed",
                "returncode": 0 if success else 1,
            }]
        if isinstance(function_result, ToolResult):
            data = function_result.data if isinstance(function_result.data, dict) else {}
        elif isinstance(function_result, dict):
            data = function_result.get("data") if isinstance(function_result.get("data"), dict) else function_result
        else:
            data = {}
        raw_output = data.get("output") or ""
        payload: dict[str, Any] = {}
        if isinstance(raw_output, str):
            try:
                parsed = json.loads(raw_output)
                if isinstance(parsed, dict):
                    payload = parsed
            except (TypeError, ValueError):
                pass
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        success = bool(payload.get("success", data.get("success", False)))
        if success:
            lines = [
                "数据集快速探查已完成",
                f"文件：{summary.get('files_analyzed', 0)} 个",
                f"图表：{summary.get('plot_count', 0)} 张",
                f"耗时：{summary.get('elapsed_seconds', 0)} 秒",
            ]
            failed = summary.get("files_failed", 0)
            if failed:
                lines.append(f"失败：{failed} 个文件")
            evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
            discovery = evidence.get("discovery") if isinstance(evidence.get("discovery"), dict) else {}
            if discovery.get("truncated"):
                lines.append("提示：文件数量较多，结果基于有界抽样")
            output = "\n".join(lines)
        else:
            error = payload.get("error") or data.get("error") or "快速探查未生成有效结果"
            output = f"数据集快速探查失败：{str(error)[:500]}"
        return [{
            "ps1": "$",
            "command": "快速探查数据集",
            "output": output,
            "status": "completed" if success else "failed",
            "returncode": 0 if success else 1,
        }]
    
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
                            "Kept unsynced attachment agent=%s object=%s filename_chars=%d",
                            opaque_log_identifier(self._agent_id, namespace="agent"),
                            opaque_log_identifier(attachment.file_id, namespace="object"),
                            len(attachment.filename or ""),
                        )
            event.attachments = attachments
        except Exception as e:
            logger.error(
                "Failed to sync attachments to event agent=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                type(e).__name__,
            )
    

    # TODO: refactor this function
    async def _handle_tool_event(self, event: ToolEvent):
        """Generate tool content"""
        try:
            if event.status == ToolStatus.CALLED:
                if event.tool_name == "browser":
                    event.tool_content = BrowserToolContent(screenshot=await self._get_browser_screenshot())
                elif event.tool_name == "search":
                    search_results: ToolResult[SearchResults] = event.function_result
                    logger.debug(
                        "Search tool result prepared agent=%s success=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        getattr(search_results, "success", None),
                    )
                    event.tool_content = SearchToolContent(results=search_results.data.results)
                elif event.tool_name == "shell":
                    if event.function_name == "dataset_analysis_run":
                        completed_console = self._dataset_analysis_console(event)
                    elif event.function_name == "dataset_quicklook":
                        completed_console = self._dataset_quicklook_console(event)
                    else:
                        completed_console = self._completed_shell_console_from_result(event)
                    if completed_console is not None:
                        event.tool_content = ShellToolContent(console=completed_console)
                    elif "id" in event.function_args:
                        shell_result = await self._sandbox.view_shell(event.function_args["id"], console=True)
                        shell_data = shell_result.data if isinstance(shell_result.data, dict) else {}
                        console = self._shell_console_for_event(shell_data.get("console", []), event)
                        event.tool_content = ShellToolContent(console=console)
                    else:
                        event.tool_content = ShellToolContent(console="(No Console)")
                elif event.tool_name == "file":
                    spill_notice = spill_notice_from_result(event.function_result)
                    if spill_notice is not None:
                        locator = (
                            spill_notice.reference.locator
                            if spill_notice.reference is not None
                            else "unavailable"
                        )
                        event.tool_content = FileToolContent(content=(
                            f"{sanitize_spill_public_text(spill_notice.preview)}\n\n"
                            f"[Oversized output: {spill_notice.original_bytes} bytes; "
                            f"spill={spill_notice.status}; locator={locator}]"
                        ))
                    elif event.function_name == "file_find_by_name":
                        event.tool_content = FileToolContent(content=event.function_result.model_dump_json() if hasattr(event.function_result, "model_dump_json") else str(event.function_result))
                    elif event.function_name == "file_find_in_content":
                        event.tool_content = FileToolContent(content=event.function_result.model_dump_json() if hasattr(event.function_result, "model_dump_json") else str(event.function_result))
                    elif "file" in event.function_args:
                        file_path = event.function_args["file"]
                        file_read_result = await self._sandbox.file_read(file_path)
                        file_content: str = file_read_result.data.get("content", "")
                        event.tool_content = FileToolContent(content=file_content)
                    else:
                        event.tool_content = FileToolContent(content="(No Content)")
                elif event.tool_name == "mcp":
                    logger.debug(
                        "Processing MCP tool event agent=%s success=%s result_type=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        getattr(event.function_result, "success", None),
                        type(event.function_result).__name__,
                    )
                    if event.function_result:
                        if hasattr(event.function_result, 'data') and event.function_result.data:
                            event.tool_content = McpToolContent(result=event.function_result.data)
                        elif hasattr(event.function_result, 'success') and event.function_result.success:
                            result_data = event.function_result.model_dump() if hasattr(event.function_result, 'model_dump') else str(event.function_result)
                            event.tool_content = McpToolContent(result=result_data)
                        else:
                            event.tool_content = McpToolContent(result=str(event.function_result))
                    else:
                        logger.warning("MCP tool: No function_result found")
                        event.tool_content = McpToolContent(result="No result available")
                    
                    logger.debug(
                        "MCP tool content prepared agent=%s content_type=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        type(event.tool_content).__name__ if event.tool_content else None,
                    )
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
                    logger.warning(
                        "Received unknown tool event agent=%s tool=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        opaque_log_identifier(event.tool_name, namespace="tool"),
                    )
            if event.status == ToolStatus.CALLED:
                await self._report_analysis_tool_usage(event)
        except Exception as e:
            logger.error(
                "Failed to generate tool content agent=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                type(e).__name__,
            )

    async def _report_analysis_tool_usage(self, event: ToolEvent) -> None:
        """Report successful scientific Tool calls for SSO-linked submissions."""
        function_name = str(event.function_name or "")
        if not (
            function_name.startswith("scientific_")
            or function_name.startswith("geoscience_")
        ):
            return
        result = event.function_result
        if not isinstance(result, ToolResult) or not result.success:
            return
        reports = []
        for dataset in self._active_datasets:
            metadata = dataset.metadata if isinstance(dataset.metadata, dict) else {}
            uid = metadata.get("sso_uid")
            dataset_id = str(getattr(dataset, "dataset_id", ""))
            title = str(getattr(dataset, "name", "") or "数据集").strip()
            if not isinstance(uid, str) or not uid.strip() or not dataset_id:
                continue
            key = (event.tool_call_id, dataset_id, function_name)
            if key in self._reported_analysis_tool_usage:
                continue
            self._reported_analysis_tool_usage.add(key)
            reports.append(record_analysis_tool_usage(
                uid=uid.strip(),
                title=title,
                tool_id=function_name,
            ))
        if reports:
            await asyncio.gather(*reports)

    async def run(self, task: Task) -> None:
        task_id = getattr(task, "id", None)
        if isinstance(task_id, str) and task_id:
            with model_execution_scope(user_id=self._user_id, session_id=self._session_id,
                                       task_id=task_id, store=get_model_trace_repository()):
                await self._run_with_model_budget(task)
        else:
            # Isolated legacy Task test doubles have no persisted identity.
            await self._run_with_model_budget(task)

    async def _run_with_model_budget(self, task: Task) -> None:
        """Process agent's message queue and run the agent's flow"""
        if getattr(self, "_analysis_job_service", None) is not None or getattr(self, "_tool_approval_service", None) is not None:
            task_id = self._execution_task_id(task)
            self._flow._analysis_job_identity_provider = lambda: {
                "task_id": task_id,
                "execution_snapshot_id": (
                    self._execution_snapshot.task_id if self._execution_snapshot else None
                ),
                "catalog_revision": self._flow.plugin_toolkit.catalog_revision,
                "sandbox_id": self._sandbox.id,
            }
            self._flow._analysis_job_event_sink = lambda view, context: self._publish_analysis_job(task, view, context)
            self._flow._tool_approval_event_sink = lambda view, context: self._publish_tool_approval(task, view, context)
        try:
            logger.info(
                "Message processing task started agent=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
            )
            ensure_api_ready = getattr(self._sandbox, "ensure_api_ready", None)
            if callable(ensure_api_ready):
                await ensure_api_ready()
            else:
                await self._sandbox.ensure_sandbox()
            artifact_baseline_initialized = False
            while True:
                pop_input_or_close = getattr(task, "pop_input_or_close", None)
                if callable(pop_input_or_close):
                    event_id, event_str = await pop_input_or_close()
                    if event_str is None:
                        break
                    event = self._decode_input_event(event_id, event_str)
                else:
                    if await task.input_stream.is_empty():
                        break
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
                self._active_datasets = list(datasets)
                self._remember_mounted_dataset_paths(datasets)
                # Capture the baseline after catalog files have been materialized;
                # otherwise the read-only source files look like new artifacts.
                if not artifact_baseline_initialized:
                    await self._capture_artifact_baseline()
                    artifact_baseline_initialized = True
                    
                logger.info(
                    "Agent received new message agent=%s chars=%d",
                    opaque_log_identifier(self._agent_id, namespace="agent"),
                    len(message),
                )

                sandbox_attachment_paths = [
                    attachment.file_path
                    for attachment in (event.attachments or [])
                    if attachment.file_path
                ]
                logger.info(
                    "Agent message attachments agent=%s request=%d file_ids=%d sandbox_paths=%d",
                    opaque_log_identifier(self._agent_id, namespace="agent"),
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
                    controller_target_files=list(
                        self._front_controller_resolution.target_files
                        if getattr(self, "_front_controller_resolution", None) is not None
                        else []
                    ),
                    mcp_access_all=bool(metadata.get("mcp_access_all", False)),
                )

                # Generated attachments belong to one user turn.  Keeping files
                # from an earlier turn here makes later summaries re-deliver
                # stale artifacts even when nothing changed.
                self._generated_files = []
                execution_task_id = self._execution_task_id(task)
                flow_events = (
                    self._run_flow(
                        message_obj,
                        task_id=execution_task_id,
                        trigger_event_seq=event.seq,
                    )
                    if execution_task_id is not None
                    else self._run_flow(message_obj)
                )
                async for event in flow_events:
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
        except asyncio.CancelledError as error:
            logger.info(
                "Agent task cancelled agent=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
            )
            if isinstance(error, ModelBudgetStopped) or model_stop_reason():
                await self._put_and_add_event(task, MessageEvent(
                    message=("本轮执行已在模型运行边界停止：上下文或本轮 Token/调用预算不足，或运行记录暂时无法保存。"
                             "已有分析结果会保留。请缩小问题范围或检查模型运行记录后继续。"),
                ))
            elif isinstance(error, ToolAuthorizationStopped):
                await self._put_and_add_event(task, MessageEvent(
                    message="本次工具调用未获得有效授权（可能已拒绝、过期、凭据未配置或权限检查未通过），本轮执行已停止。请检查调用权限和凭据配置后重新发起。",
                ))
            elif isinstance(error, AnalysisJobCancelled):
                await self._put_and_add_event(task, MessageEvent(
                    message="当前分析作业已取消，本轮执行已停止。你可以调整要求后继续分析。",
                ))
            elif isinstance(error, AnalysisJobInterrupted):
                await self._put_and_add_event(task, MessageEvent(
                    message="当前分析作业的执行已中断，本轮执行已停止。系统没有自动重跑，请检查已有结果后再继续。",
                ))
            await self._put_and_add_event(task, DoneEvent())
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except Exception as e:
            logger.error(
                "Agent %s task encountered exception error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                type(e).__name__,
            )
            
            # If debugger is attached, trigger breakpoint for debugging
            # You can also manually set ENABLE_DEBUG_BREAK=1 environment variable
            if debugpy.is_client_connected() or os.getenv('ENABLE_DEBUG_BREAK'):
                logger.debug(
                    "Debugger detected; triggering breakpoint error_type=%s",
                    type(e).__name__,
                )
                debugpy.breakpoint()  # This will pause execution if a debugger is attached
            
            await self._put_and_add_event(
                task,
                ErrorEvent(error=public_error_message(f"Task error: {e}")),
            )
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
        # MCP tools are discovered asynchronously after PlanActFlow is built.
        # Recheck here so no dynamic provider can create an ambiguous schema.
        flow = getattr(self, "_flow", None)
        validate_names = getattr(flow, "validate_plugin_tool_names", None)
        if callable(validate_names):
            try:
                validate_names()
            except Exception:
                await self._mcp_tool.cleanup()
                raise
        configure_execution = getattr(flow, "configure_tool_execution", None)
        if callable(configure_execution):
            try:
                configure_execution(
                    trace_sink=getattr(self, "_tool_trace_sink", None),
                )
            except Exception:
                await self._mcp_tool.cleanup()
                raise
    
    async def _run_flow(
        self,
        message: Message,
        *,
        task_id: str | None = None,
        trigger_event_seq: int | None = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Process a single message through the agent's flow and yield events"""
        if not message.message:
            if task_id:
                await self._record_execution_snapshot(
                    task_id=task_id,
                    message=message,
                    trigger_event_seq=trigger_event_seq,
                )
            logger.warning(
                "Agent received empty message agent=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
            )
            yield ErrorEvent(error="No message")
            return

        resolution = getattr(self, "_front_controller_resolution", None)
        review = resolution.decision.safety if resolution else SafetyReview(
            decision="reject",
            risk_level="high",
            categories=["front_controller_decision_missing"],
            reason="服务端前置决策缺失，任务未执行。",
            suggestion="请重新发送该任务。",
        )
        if review.allowed and resolution.mode != "sandbox":
            review = SafetyReview(
                decision="reject",
                risk_level="high",
                categories=["front_controller_decision_invalid"],
                reason="前置决策与执行器不一致，任务未执行。",
                suggestion="请重新发送该任务。",
            )
        if review.allowed and message.attachment_file_infos:
            try:
                rules = await self._safety_policy_store.list_enabled()
                attachment_review = deterministic_review(
                    json.dumps({
                        "user_message": message.message[:12000],
                        "attachments": await self._attachment_review_excerpts(message),
                    }, ensure_ascii=False),
                    rules,
                )
                if attachment_review:
                    review = attachment_review
            except Exception as exc:
                logger.error(
                    "Attachment safety policy check failed closed agent=%s error_type=%s",
                    opaque_log_identifier(self._agent_id, namespace="agent"),
                    type(exc).__name__,
                )
                review = SafetyReview(
                    decision="reject",
                    risk_level="high",
                    categories=["safety_policy_unavailable"],
                    reason="附件安全策略暂时不可用，任务未执行。",
                    suggestion="请稍后重新发送该任务。",
                )
        await self._record_safety_audit(review)
        if not review.allowed:
            if task_id:
                await self._record_execution_snapshot(
                    task_id=task_id,
                    message=message,
                    trigger_event_seq=trigger_event_seq,
                )
            logger.warning(
                "Rejected user message before Planner agent=%s risk=%s category_count=%d",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                review.risk_level,
                len(review.categories),
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
                    },
                    "front_controller": resolution.controller_metadata if resolution else {},
                },
            )
            yield DoneEvent()
            return

        try:
            await self._initialize_mcp_tool(
                message.mcp_servers,
                is_admin=message.mcp_access_all,
            )
        except Exception:
            if task_id:
                await self._record_execution_snapshot(
                    task_id=task_id,
                    message=message,
                    trigger_event_seq=trigger_event_seq,
                )
            raise
        prepare_environment = getattr(
            self._flow,
            "prepare_execution_environment",
            None,
        )
        if callable(prepare_environment):
            try:
                prepare_environment(message)
            except Exception:
                if task_id:
                    await self._record_execution_snapshot(
                        task_id=task_id,
                        message=message,
                        trigger_event_seq=trigger_event_seq,
                    )
                raise
        if task_id:
            await self._record_execution_snapshot(
                task_id=task_id,
                message=message,
                trigger_event_seq=trigger_event_seq,
            )

        artifact_discovery_dirty = bool(getattr(self, "_generated_files", []))
        artifact_discovery_ran = False
        delivered_file_keys: set[str] = set()
        completed_step_count = 0
        early_artifact_delivery_count = 0
        skip_next_step_result: Optional[str] = None
        # Completion advice only needs the current turn.  Keeping this compact
        # also avoids loading and serializing the full session at Done time.
        turn_events: List[BaseEvent] = [
            MessageEvent(role="user", message=message.message)
        ]

        async for event in self._flow.run(message):
            pre_events: List[BaseEvent] = []
            post_events: List[BaseEvent] = []
            suppress_event = False
            if isinstance(event, ToolEvent):
                # TODO: move to tool function
                self._remember_private_tool_output(event)
                await self._handle_tool_event(event)
                if event.status == ToolStatus.CALLED:
                    if event.function_name == "dataset_analysis_run":
                        # A successful compiled analysis has a validated result
                        # contract, but model-authored manifests can omit files
                        # they actually created (most often PNG plots). Discover
                        # the bounded output directory as a reconciliation pass
                        # so explicit and undeclared real artifacts are delivered
                        # together. Failed runs remain isolated from discovery
                        # to avoid publishing unverified leftovers.
                        result = event.function_result
                        success = (
                            result.get("success")
                            if isinstance(result, dict)
                            else getattr(result, "success", False)
                        )
                        artifact_discovery_dirty = bool(success)
                    else:
                        # Tools may create or replace files. Defer discovery until
                        # the step boundary instead of scanning after every event.
                        artifact_discovery_dirty = True
            elif isinstance(event, StepEvent):
                explicit_files = await self._sync_step_attachments_to_storage(event)
                artifact_policy = str(
                    (event.step.inputs or {}).get("artifact_policy") or "optional"
                )
                force_artifact_discovery = artifact_policy in {
                    "required",
                    "capability",
                }
                if event.status == StepStatus.COMPLETED and (
                    artifact_discovery_dirty
                    or (force_artifact_discovery and not artifact_discovery_ran)
                ):
                    # Explicit step attachments were just downloaded, hashed and
                    # uploaded.  Do not download the same bytes again during the
                    # output-directory discovery pass in this step.
                    explicit_paths = {
                        file_info.file_path
                        for file_info in explicit_files
                        if file_info.file_path
                    }
                    if explicit_paths:
                        discovered_files = await self._sync_discovered_artifacts_to_storage(
                            skip_paths=explicit_paths,
                        )
                    else:
                        discovered_files = await self._sync_discovered_artifacts_to_storage()
                    artifact_discovery_dirty = False
                    artifact_discovery_ran = True
                else:
                    discovered_files = []

                if event.status == StepStatus.COMPLETED:
                    completed_step_count += 1
                    new_files = [
                        file_info
                        for file_info in self._unique_files(explicit_files + discovered_files)
                        if self._file_delivery_key(file_info) not in delivered_file_keys
                    ]
                    if new_files:
                        result_message = (event.step.result or "").strip()
                        delivery_message = result_message or f"已生成 {len(new_files)} 个结果文件。"
                        delivery_event = MessageEvent(
                            role="assistant",
                            message=delivery_message,
                            attachments=new_files,
                            metadata={
                                "artifact_delivery": True,
                                "step_id": event.step.id,
                            },
                        )
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info) for file_info in new_files
                        )
                        early_artifact_delivery_count += 1
                        post_events.append(delivery_event)
                        if result_message:
                            # ExecutionAgent emits step.result immediately after
                            # StepEvent.  The delivery event above is that same
                            # answer with its files attached, so discard the
                            # following attachment-free duplicate.
                            skip_next_step_result = result_message
            elif isinstance(event, MessageEvent):
                is_summary = self._should_attach_generated_files_to_message()
                summary_discovered_files: List[FileInfo] = []
                normalized_message = (event.message or "").strip()
                if (
                    skip_next_step_result is not None
                    and not is_summary
                    and not event.attachments
                    and normalized_message == skip_next_step_result
                ):
                    suppress_event = True
                    skip_next_step_result = None
                elif skip_next_step_result is not None and not is_summary:
                    skip_next_step_result = None

                if not suppress_event and is_summary and artifact_discovery_dirty:
                    summary_discovered_files = await self._sync_discovered_artifacts_to_storage()
                    artifact_discovery_dirty = False
                    artifact_discovery_ran = True
                if not suppress_event:
                    await self._sync_message_attachments_to_storage(event)
                    event.attachments = [
                        file_info
                        for file_info in self._unique_files(
                            list(event.attachments or []) + summary_discovered_files
                        )
                        if self._file_delivery_key(file_info) not in delivered_file_keys
                    ]
                    if event.attachments:
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info)
                            for file_info in event.attachments
                        )
                    # A one-step result with artifacts was already delivered at
                    # the completed-step boundary.  Do not show a second LLM
                    # rendition of the same answer merely to carry those files.
                    if (
                        is_summary
                        and completed_step_count == 1
                        and early_artifact_delivery_count == 1
                        and not event.attachments
                    ):
                        suppress_event = True
            elif isinstance(event, ErrorEvent):
                # The live SSE consumer treats ErrorEvent as terminal.  Publish
                # any durable partial outputs first; otherwise they are uploaded
                # later at Done time and exist in history, but the connected user
                # never receives them before the stream closes.
                if artifact_discovery_dirty:
                    partial_files = await self._sync_discovered_artifacts_to_storage()
                    artifact_discovery_dirty = False
                    artifact_discovery_ran = True
                    partial_files = [
                        file_info
                        for file_info in partial_files
                        if self._file_delivery_key(file_info) not in delivered_file_keys
                    ]
                    if partial_files:
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info)
                            for file_info in partial_files
                        )
                        pre_events.append(
                            MessageEvent(
                                role="assistant",
                                message=(
                                    f"任务未能完整完成，但已保留 {len(partial_files)} "
                                    "个阶段性结果文件。"
                                ),
                                attachments=partial_files,
                                metadata={
                                    "artifact_delivery": True,
                                    "partial": True,
                                },
                            )
                        )
            elif isinstance(event, WaitEvent):
                if artifact_discovery_dirty:
                    late_files = await self._sync_discovered_artifacts_to_storage()
                    artifact_discovery_dirty = False
                    artifact_discovery_ran = True
                    late_files = [
                        file_info
                        for file_info in late_files
                        if self._file_delivery_key(file_info) not in delivered_file_keys
                    ]
                    if late_files:
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info) for file_info in late_files
                        )
                        pre_events.append(
                            MessageEvent(
                                role="assistant",
                                message=f"已生成 {len(late_files)} 个结果文件。",
                                attachments=late_files,
                                metadata={"artifact_delivery": True},
                            )
                        )
            elif isinstance(event, DoneEvent):
                if artifact_discovery_dirty:
                    late_files = await self._sync_discovered_artifacts_to_storage()
                    artifact_discovery_dirty = False
                    artifact_discovery_ran = True
                    late_files = [
                        file_info
                        for file_info in late_files
                        if self._file_delivery_key(file_info) not in delivered_file_keys
                    ]
                    if late_files:
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info) for file_info in late_files
                        )
                        pre_events.append(
                            MessageEvent(
                                role="assistant",
                                message=f"已生成 {len(late_files)} 个结果文件。",
                                attachments=late_files,
                                metadata={"artifact_delivery": True},
                            )
                        )
                completion_advice_service = getattr(
                    self,
                    "_completion_advice_service",
                    None,
                )
                if completion_advice_service is not None:
                    try:
                        advice = completion_advice_service.analyze_fast([*turn_events, *pre_events])
                        event.advice = completion_advice_service.to_payload(advice)
                    except Exception as exc:
                        logger.warning(
                            "Failed to build completion advice session=%s error_type=%s",
                            opaque_log_identifier(self._session_id, namespace="session"),
                            type(exc).__name__,
                        )
            for pre_event in pre_events:
                yield pre_event
                turn_events.append(pre_event)
            if not suppress_event:
                yield event
                turn_events.append(event)
            for post_event in post_events:
                yield post_event
                turn_events.append(post_event)

        logger.info(
            "Agent completed processing one message agent=%s",
            opaque_log_identifier(self._agent_id, namespace="agent"),
        )

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
                    logger.info(
                        "Safety review could not read attachment file=%s filename_chars=%d error_type=%s",
                        opaque_log_identifier(path, namespace="file"),
                        len(info.filename or ""),
                        type(exc).__name__,
                    )
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
                    "front_controller": (
                        self._front_controller_resolution.controller_metadata
                        if getattr(self, "_front_controller_resolution", None)
                        else {}
                    ),
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist safety review audit session=%s error_type=%s",
                opaque_log_identifier(self._session_id, namespace="session"),
                type(exc).__name__,
            )

    async def on_done(self, task: Task) -> None:
        """Called when the task is done"""
        logger.info(
            "Agent task done agent=%s",
            opaque_log_identifier(self._agent_id, namespace="agent"),
        )
        try:
            if self._browser and hasattr(self._browser, "cleanup"):
                try:
                    await self._browser.cleanup()
                except Exception as exc:
                    logger.warning(
                        "Failed to cleanup browser before pausing sandbox agent=%s error_type=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        type(exc).__name__,
                    )
            if self._sandbox and hasattr(self._sandbox, "pause"):
                paused = await self._sandbox.pause()
                if paused:
                    logger.info(
                        "Paused sandbox after task completion agent=%s sandbox=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        opaque_log_identifier(self._sandbox.id, namespace="sandbox"),
                    )
                else:
                    logger.warning(
                        "Failed to pause sandbox after task completion agent=%s sandbox=%s",
                        opaque_log_identifier(self._agent_id, namespace="agent"),
                        opaque_log_identifier(self._sandbox.id, namespace="sandbox"),
                    )
        finally:
            try:
                mcp_tool = getattr(self, "_mcp_tool", None)
                if mcp_tool:
                    try:
                        await mcp_tool.cleanup()
                    except Exception as exc:
                        logger.warning(
                            "Failed to cleanup MCP tools after task completion agent=%s error_type=%s",
                            opaque_log_identifier(self._agent_id, namespace="agent"),
                            type(exc).__name__,
                        )
            finally:
                drain = getattr(getattr(self, "_flow", None), "drain_spill_saves", None)
                if callable(drain):
                    # wait_closed() is the session-deletion barrier. No
                    # detached save may insert a record after owner cleanup.
                    await drain()


    async def destroy(self) -> None:
        """Destroy the task and release resources"""
        logger.info("Starting to destroy agent task")
        
        # Destroy sandbox environment
        if self._sandbox:
            logger.debug(
                "Destroying sandbox environment agent=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
            )
            await self._sandbox.destroy()
        
        if self._mcp_tool:
            logger.debug(
                "Destroying MCP tool agent=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
            )
            await self._mcp_tool.cleanup()
        
        logger.debug(
            "Agent fully closed and resources cleared agent=%s",
            opaque_log_identifier(self._agent_id, namespace="agent"),
        )
