from typing import Any, Optional, AsyncGenerator, List
from contextlib import aclosing
import asyncio
import hashlib
import json
import logging
import os
import io
import re
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
    PlanEvent,
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
from app.domain.utils.robust_json_parser import ToolCallParseError
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
from app.domain.models.analysis_input import (
    assign_upload_namespace, build_analysis_inputs, upload_runtime_path, UPLOAD_INPUT_ROOT,
)
from app.domain.services.analysis_input_selection import snapshot_files
from app.domain.services.analysis_terminal import terminal_analysis_message
from app.domain.models.execution_environment import ExecutionEnvironmentSnapshot
from app.domain.services.execution_environment import create_agent_execution_snapshot
from app.domain.services.model_runtime import ModelBudgetStopped, model_execution_scope, model_stop_reason, analysis_budget_scope
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
        input_delivery=None,
        session_events_snapshot: list | None = None,
        effective_dataset_ids: list[str] | None = None,
    ):
        self._session_id = session_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._sandbox = sandbox
        self._browser = browser
        self._search_engine = search_engine
        self._repository = agent_repository
        self._session_repository = session_repository
        self._input_delivery = input_delivery
        self._accepted_input_key: str | None = None
        self._accepted_input_finished = False
        self._session_events_snapshot = session_events_snapshot
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
        from app.domain.services.tools.mcp_images import MCPImageContext
        from app.domain.models.spill import SpillArtifactOwner
        from app.core.config import get_settings
        image_settings = get_settings()
        mcp_images = None
        if image_settings.mcp_image_results_enabled and image_settings.spill_enabled and spill_artifact_store is not None:
            mcp_images = MCPImageContext(store=spill_artifact_store,
                owner=SpillArtifactOwner(user_id=user_id, session_id=session_id),
                provider=str(self._llm_overrides.get("model_provider") or image_settings.model_provider).lower().strip(),
                model_name=self._llm_overrides.get("model_name") or image_settings.model_name,
                settings=image_settings)
        self._mcp_tool = MCPToolkit(image_context=mcp_images)
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
        self._mounted_dataset_ids: set[str] = set(effective_dataset_ids or [])
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
        input_delivery = getattr(self, "_input_delivery", None)
        input_identity = getattr(self, "_accepted_input_key", None)
        if input_delivery is not None and input_identity is not None:
            if getattr(self, "_accepted_input_finished", False):
                from app.domain.services.input_delivery import InputLeaseLost
                raise InputLeaseLost()
            await input_delivery.prepare_event(self._session_id, input_identity, event)
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
        await self._session_repository.add_event(self._session_id, event)
        if isinstance(event, MessageEvent) and event.role == "assistant":
            metadata = event.metadata or {}
            if metadata.get("analysis_outcome"):
                # Per-step delivery is not the final verdict of a multi-step
                # turn. A later interrupted step must remain visibly unfinished.
                if not metadata.get("step_id"):
                    self._analysis_outcome_published = True
                if metadata["analysis_outcome"].get("status") in {"succeeded", "partial"}:
                    self._analysis_has_verified_result = True
            preserved = getattr(self, "_prior_artifact_file_ids", None)
            if preserved is None:
                preserved = self._prior_artifact_file_ids = set()
            preserved.update(item.file_id for item in event.attachments or [] if item.file_id)
        try:
            event_id = await task.output_stream.put(event.model_dump_json())
            event.id = event_id
            record_alias = getattr(self._session_repository, "record_event_transport_alias", None)
            if callable(record_alias):
                await record_alias(self._session_id, event)
        except Exception as error:
            # A committed event is already recoverable via seq. A transport
            # outage must not cause the Agent to repeat its successful tool.
            logger.warning("Durable event live publication unavailable error_type=%s", type(error).__name__)
        if input_delivery is not None and input_identity is not None:
            await input_delivery.complete(self._session_id, input_identity, event)
            if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                self._accepted_input_finished = True

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
        event.bind_producer_event_id()
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
        if file_info.file_id in getattr(self, "_analysis_preserved_file_ids", set()):
            return False
        # Output paths are working copies, but previously published object IDs
        # are immutable history. A later turn may replace the path, never the
        # storage object still referenced by an earlier message.
        if (file_info.file_id in getattr(self, "_prior_artifact_file_ids", set())
                or getattr(self, "_artifact_history_known", True) is False):
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
            # A local repair must never replace/delete a previously verified
            # uploaded object. Validation later observes the current sandbox
            # bytes independently and detects changed or missing working copies.
            for pinned in getattr(self, "_analysis_verified_files", {}).values():
                if file_path in pinned:
                    return pinned[file_path]
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
        if not self._is_deliverable_artifact(file_path):
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

    def _is_deliverable_artifact(self, file_path: str) -> bool:
        """Analysis implementation files are private unless code was requested.

        A model's attachment list is not a request to deliver its helper script.
        The contract is set for each user turn before discovery or uploads run;
        legacy storage-only callers without a request keep their existing scope.
        """
        from app.domain.services.analysis_completion import artifact_kind
        if not isinstance(file_path, str) or not file_path:
            return False
        requested = getattr(self, "_requested_code_deliverables", None)
        if requested is None or artifact_kind(file_path) != "code":
            return True
        suffix = PurePosixPath(file_path).suffix.lower().lstrip(".")
        return any((not item.formats or suffix in item.formats)
                   and (not item.output_paths or file_path in item.output_paths
                        or item.min_count > len(item.output_paths)) for item in requested)

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
                or not self._is_deliverable_artifact(file_path)
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
        self._prior_artifact_file_ids = set()
        self._artifact_history_known = False
        try:
            session = await self._session_repository.find_by_id(self._session_id)
            self._prior_artifact_file_ids = {
                item.file_id for item in (getattr(session, "files", None) or []) if item.file_id
            }
            self._artifact_history_known = session is not None
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

    async def _sandbox_artifact_fingerprints(
        self, paths: list[str],
    ) -> tuple[dict[str, ArtifactFingerprint], set[str]]:
        """Hash known outputs near the data; old sandbox versions fall back."""
        reader = getattr(self._sandbox, "file_fingerprints", None)
        if not callable(reader) or not paths:
            return {}, set()
        fingerprints: dict[str, ArtifactFingerprint] = {}
        unavailable: set[str] = set()
        for offset in range(0, len(paths), 256):
            batch = paths[offset:offset + 256]
            try:
                result = await reader(batch)
                data = result.data if result.success else None
                if not isinstance(data, dict) or data.get("version") != 1:
                    continue
                for item in data.get("files", []):
                    if not isinstance(item, dict) or item.get("path") not in batch:
                        continue
                    size, digest = item.get("size"), item.get("sha256")
                    if type(size) is int and size >= 0 and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                        fingerprints[item["path"]] = (size, digest)
                for item in data.get("errors", []):
                    if isinstance(item, dict) and item.get("path") in batch:
                        unavailable.add(item["path"])
            except Exception as exc:
                logger.debug("Artifact manifest fallback error_type=%s", type(exc).__name__)
        return fingerprints, unavailable

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
        remote_fingerprints, unavailable = await self._sandbox_artifact_fingerprints([
            path for path in current_paths if path in fingerprints and path not in skipped
        ])
        for file_path in current_paths:
            if len(attachments) >= MAX_AUTO_SYNC_ARTIFACTS:
                break
            if file_path in skipped or file_path in unavailable or not self._is_deliverable_artifact(file_path):
                continue
            if file_path in remote_fingerprints and remote_fingerprints[file_path] == fingerprints.get(file_path):
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
            pending_paths = getattr(self, "_pending_artifact_paths", None)
            if pending_paths is None:
                pending_paths = self._pending_artifact_paths = set()
            pending_paths.add(file_path)
            file_info = await self._sync_file_to_storage(
                file_path,
                file_data=file_data,
                fingerprint=fingerprint,
            )
            if file_info:
                attachments.append(file_info)
                self._remember_generated_file(file_info)
        return attachments
    
    async def _validate_staged_artifact_files(self, files: List[FileInfo]) -> List[FileInfo]:
        """Check staged uploads without declaring completion or scheduling repair.

        A durable object alone is not a usable result. Every attachment leaving
        a non-step boundary needs a content receipt bound to its uploaded bytes.
        Unavailable/malformed batches are withheld, without suppressing the
        surrounding question, error, completion event, or cancellation.
        """
        from app.domain.services.analysis_completion import artifact_kind, verified_deliveries

        candidates = []
        paths = set()
        for info in files:
            path = info.file_path
            if (not isinstance(path, str) or not path.startswith("/home/ubuntu/output/")
                    or not self._is_deliverable_artifact(path)
                    or str(PurePosixPath(path)) != path or ".." in PurePosixPath(path).parts
                    or "\\" in path or any(ord(char) < 32 for char in path)
                    or not artifact_kind(path)):
                continue
            candidates.append(info)
            paths.add(path)
        if not candidates:
            return []
        validate = getattr(getattr(self, "_sandbox", None), "validate_artifacts", None)
        if not callable(validate):
            return []
        items = [{"path": path, "kind": artifact_kind(path)} for path in sorted(paths)]
        records = []
        for offset in range(0, len(items), 32):
            batch = items[offset:offset + 32]
            try:
                async with asyncio.timeout(35):
                    result = await validate(batch)
                data = getattr(result, "data", None)
                batch_ok = (getattr(result, "success", None) is True and isinstance(data, dict)
                            and type(data.get("version")) is int and data["version"] == 1)
                batch_records = data.get("files") if batch_ok else None
                batch_ok = (batch_ok and isinstance(batch_records, list)
                            and len(batch_records) == len(batch)
                            and all(isinstance(item, dict) and isinstance(item.get("path"), str)
                                    for item in batch_records)
                            and {item["path"] for item in batch_records} == {item["path"] for item in batch})
                if batch_ok:
                    records.extend(batch_records)
            except Exception as exc:
                # CancelledError intentionally propagates. No model/tool retry
                # is appropriate merely because verification is unavailable.
                logger.warning("Staged artifact validation unavailable error_type=%s", type(exc).__name__)
        return verified_deliveries(records, candidates)

    async def _sync_file_to_sandbox(self, file_id: str, *, input_info: FileInfo | None = None) -> Optional[FileInfo]:
        """Download file from storage to sandbox"""
        try:
            file_data, file_info = await self._file_storage.download_file(file_id, self._user_id)
            identity = input_info or assign_upload_namespace([file_info])[0]
            file_path = upload_runtime_path(identity)
            file_data = _rewind_or_buffer_stream(file_data)
            result = await self._sandbox.file_upload(file_data, file_path, filename=file_info.filename)
            if result.success:
                file_info.file_path = file_path
                file_info.metadata = dict(identity.metadata or {})
                return file_info
        except Exception as e:
            logger.error(
                "Failed to sync storage file into sandbox agent=%s object=%s error_type=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
                opaque_log_identifier(file_id, namespace="object"),
                type(e).__name__,
            )

    async def _sync_analysis_inputs_to_sandbox(self, event: MessageEvent) -> list[FileInfo]:
        """Materialize the accepted input snapshot, not a mutable session file list."""
        selected = snapshot_files(event.metadata)
        materialized = []
        for info in selected:
            if (event.metadata or {}).get("resume_from"):
                # Resume must observe existing bytes. Re-uploading first would
                # mask modifications/missing inputs from checkpoint fingerprints.
                actual = info.model_copy(deep=True, update={"file_path": upload_runtime_path(info)})
            else:
                actual = await self._sync_file_to_sandbox(info.file_id, input_info=info)
            if actual is None:
                raise ValueError("分析资料无法读取，任务未执行；请检查已选择的文件。")
            materialized.append(actual)
            if not (event.metadata or {}).get("resume_from"):
                await self._session_repository.add_file(self._session_id, actual)
        by_id = {item.file_id: item for item in materialized}
        event.attachments = [by_id.get(item.file_id, item) for item in event.attachments or []] or None
        return materialized

    async def _sync_message_attachments_to_storage(self, event: MessageEvent) -> None:
        """Sync message attachments and update event attachments"""
        attachments: List[FileInfo] = []
        try:
            if event.attachments:
                paths_to_sync: List[str] = []
                for attachment in event.attachments:
                    if not attachment.file_path or not self._is_deliverable_artifact(attachment.file_path):
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
            if event.status in {StepStatus.COMPLETED, StepStatus.FAILED} and event.step.attachments:
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
                    elif event.function_name == "file_read":
                        # Render the completed observation. A fresh read can
                        # change its version, discard a requested line range,
                        # or replace a failed read with unrelated later data.
                        result = event.function_result
                        result = result.model_dump(mode="python") if isinstance(result, ToolResult) else result
                        result = result if isinstance(result, dict) else {}
                        data = result.get("data")
                        content = data.get("content") if isinstance(data, dict) else None
                        if result.get("success") is True and isinstance(content, str):
                            file_content = content
                        elif result.get("success") is False:
                            file_content = "(File read failed)"
                        else:
                            file_content = "(No Content)"
                        event.tool_content = FileToolContent(content=file_content)
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
        self._analysis_outcome_published = False
        self._analysis_has_verified_result = False
        self._analysis_turn_started = False
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
                analysis_uploads = []
                metadata = {}
                if isinstance(event, MessageEvent):
                    self._analysis_outcome_published = False
                    self._analysis_has_verified_result = False
                    self._analysis_turn_started = False
                    self._accepted_input_key = None
                    self._accepted_input_finished = False
                    delivery = getattr(self, "_input_delivery", None)
                    if delivery is not None and await delivery.start(self._session_id, event, task):
                        from app.domain.models.input_admission import input_key
                        self._accepted_input_key = input_key(event)
                    message = event.message or ""
                    metadata = event.metadata or {}
                    if "analysis_input_files" in metadata:
                        analysis_uploads = await self._sync_analysis_inputs_to_sandbox(event)
                    else:
                        await self._sync_message_attachments_to_sandbox(event)
                        analysis_uploads = list(event.attachments or [])
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
                if analysis_uploads:
                    self._protected_dataset_roots.add(UPLOAD_INPUT_ROOT)
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
                    for attachment in analysis_uploads
                    if attachment.file_path
                ]
                logger.info(
                    "Agent message attachments agent=%s request=%d file_ids=%d sandbox_paths=%d",
                    opaque_log_identifier(self._agent_id, namespace="agent"),
                    len(event.attachments or []),
                    len([attachment for attachment in (event.attachments or []) if attachment.file_id]),
                    len(sandbox_attachment_paths),
                )
                decision = getattr(getattr(self, "_front_controller_resolution", None), "decision", None)
                execution_decision = getattr(decision, "execution", None)
                message_obj = Message(
                    message=message,
                    resume_from=metadata.get("resume_from"),
                    client_message_id=metadata.get("client_message_id"),
                    deliverables=list(getattr(execution_decision, "deliverables", []) or []),
                    controller_requires_artifacts=getattr(execution_decision, "requires_artifacts", None),
                    attachments=sandbox_attachment_paths,
                    attachment_file_ids=[
                        attachment.file_id
                        for attachment in analysis_uploads
                        if attachment.file_id
                    ],
                    attachment_file_infos=analysis_uploads,
                    skills=metadata.get("skills", []),
                    mcp_servers=metadata.get("mcp_servers", []),
                    datasets=datasets,
                    analysis_inputs=build_analysis_inputs(datasets, analysis_uploads),
                    controller_target_files=list(
                        self._front_controller_resolution.target_files
                        if getattr(self, "_front_controller_resolution", None) is not None
                        else []
                    ),
                    mcp_access_all=bool(metadata.get("mcp_access_all", False)),
                )
                message_obj._session_events_snapshot = getattr(self, "_session_events_snapshot", None)
                message_obj._accepted_event_seq = event.seq
                self._session_events_snapshot = None

                # Generated attachments belong to one user turn.  Keeping files
                # from an earlier turn here makes later summaries re-deliver
                # stale artifacts even when nothing changed.
                self._generated_files = []
                self._pending_artifact_paths = set()
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
                async with aclosing(flow_events):
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
            from app.domain.services.input_delivery import InputLeaseLost
            if isinstance(error, InputLeaseLost):
                # The new owner/reaper publishes the durable interrupted state.
                # This expired attempt must not fabricate another terminal.
                return
            logger.info(
                "Agent task cancelled agent=%s",
                opaque_log_identifier(self._agent_id, namespace="agent"),
            )
            if isinstance(error, ModelBudgetStopped) or model_stop_reason():
                await self._put_and_add_event(task, self._terminal_failure_event(
                    ("本次分析已达到执行时间上限，已有结果保持不变。"
                             if getattr(error, "code", None) == "analysis_budget_deadline_exceeded" or model_stop_reason() == "analysis_budget_deadline_exceeded"
                             else "本轮执行已达到模型运行边界，或运行记录暂时无法保存。已有分析结果会保留，请检查模型运行记录后继续。"),
                    reason_code="model_runtime_stopped",
                ))
            elif isinstance(error, ToolAuthorizationStopped):
                await self._put_and_add_event(task, self._terminal_failure_event(
                    "本次工具调用未获得有效授权（可能已拒绝、过期、凭据未配置或权限检查未通过），本轮执行已停止。请检查调用权限和凭据配置后重新发起。",
                    reason_code="tool_authorization_stopped",
                ))
            elif isinstance(error, AnalysisJobCancelled):
                await self._put_and_add_event(task, self._terminal_failure_event(
                    "当前分析作业已取消，本轮执行已停止。你可以调整要求后继续分析。",
                    reason_code="request_cancelled",
                ))
            else:
                await self._put_and_add_event(task, self._terminal_failure_event(
                    "当前执行已中断，本轮执行已停止。系统没有自动重跑，请检查运行记录和已有结果后再继续。",
                    reason_code="execution_interrupted",
                ))
            await self._put_and_add_event(task, DoneEvent())
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except ToolCallParseError:
            # The entire malformed batch was rejected before tool dispatch.
            # Preserve any earlier committed verdict and never publish raw
            # provider arguments or parser excerpts as a user-facing error.
            message = "模型未能生成完整、有效的工具参数，本次分析已停止。已有结果保持不变。"
            logger.warning("Analysis stopped after bounded native tool argument retries")
            await self._put_and_add_event(task, self._terminal_failure_event(
                message, reason_code="tool_protocol_error",
            ))
            await self._put_and_add_event(task, ErrorEvent(error=message))
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
            
            await self._put_and_add_event(task, self._terminal_failure_event(
                "本轮执行因运行错误停止（execution_failed）。系统没有自动重跑，请检查运行记录后再继续。",
                reason_code="execution_failed",
            ))
            await self._put_and_add_event(
                task,
                ErrorEvent(error=public_error_message(f"Task error: {e}")),
            )
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)

    def _terminal_failure_event(self, message: str, *, reason_code: str) -> MessageEvent:
        # A transport/cleanup failure after a committed scientific verdict must
        # not replace that verdict. Never infer a lease cause or artifact health.
        if getattr(self, "_analysis_outcome_published", False):
            return MessageEvent(message=message)
        started = getattr(self, "_analysis_turn_started", False)
        event = terminal_analysis_message(message, reason_code=reason_code,
            stage="execution" if started else "input_preparation", analysis_started=started)
        if getattr(self, "_analysis_has_verified_result", False):
            event.metadata["analysis_outcome"]["status"] = "partial"
        return event

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
    
    async def _review_artifact_repair(self, step, message, records, checked_files, requirements,
                                      outcome, execution, *, validation_available, source_seq, semantic_missing=()):
        """Keep repair evidence host-private; only queue a new local operation.

        The plan consumes this queue after its current executor has drained.
        No recursive generator execution or replay of an unknown operation.
        """
        from app.domain.services.analysis_artifact_repair import ArtifactRepairTracker
        from app.domain.services.analysis_completion import blocking_receipt_paths, UNAVAILABLE_REASONS
        trackers = getattr(self, "_analysis_delivery_trackers", None)
        if trackers is None:
            trackers = self._analysis_delivery_trackers = {}
            self._analysis_verified_files = {}
        if not hasattr(self._flow, "_artifact_repair_requests"):
            self._flow._artifact_repair_requests = {}
        tracker = trackers.get(step.id)
        rejected_paths = frozenset()
        handler = getattr(self._flow, "enabled_subagents", {}).get(step.agent)
        supported = (getattr(self._flow, "supports_artifact_repair", False) is True
                     and getattr(handler, "handler_type", None) == "execution"
                     and (step.inputs or {}).get("dataset_intent") != "file_preview"
                     and not self._is_delivery_only_continuation(message._resume_checkpoint))
        delivery_blocked = any(issue.blocking and issue.reason_code == "delivery_failed" for issue in outcome.issues)
        # Extra failed outputs alone never extend an otherwise fulfilled task.
        if supported and (tracker is not None or outcome.missing):
            if tracker is None:
                tracker = trackers[step.id] = ArtifactRepairTracker(
                    original_goal=message.message, step_id=step.id, requirements=requirements)
            if semantic_missing:
                try:
                    tracker.reject_semantic_candidates(records, semantic_missing)
                except ValueError:
                    # Conflicting proof is not permission to overwrite a
                    # working copy; leave immutable uploads and stop repair.
                    outcome.status = "partial" if checked_files else "failed"
                    outcome.reason_code = "artifact_validation_failed"
                    logger.warning("analysis_semantic_repair rejected=invalid_evidence")
                    return False, True
            rejected_paths = tracker.semantic_rejected_paths(records)
            if rejected_paths:
                # Byte-valid but semantically rejected working copies may be
                # regenerated at their promised path; uploaded old versions
                # remain immutable and available if repair fails.
                preserved_ids = getattr(self, "_analysis_preserved_file_ids", None)
                if preserved_ids is None:
                    preserved_ids = self._analysis_preserved_file_ids = set()
                versions = getattr(self, "_analysis_rejected_versions", None)
                if versions is None:
                    versions = self._analysis_rejected_versions = {}
                pins = self._analysis_verified_files.setdefault(step.id, {})
                for item in checked_files:
                    if item.file_path in rejected_paths:
                        preserved_ids.add(item.file_id)
                        versions[item.file_id] = item
                        pins.pop(item.file_path, None)
                if not outcome.missing:
                    outcome.missing = [item.model_copy(deep=True) for item in requirements
                                       if rejected_paths.intersection(item.output_paths)]
                    outcome.status = "partial" if checked_files else "failed"
                    outcome.reason_code = "analytical_requirements_missing"
                    step.result = None
            blocking_paths = blocking_receipt_paths(requirements, records, checked_files,
                                                    validation_available=validation_available)
            protected_paths = self._analysis_verified_files.get(step.id, {})
            relevant = [item for item in records if item.get("valid") is True
                        or item.get("path") in blocking_paths or item.get("path") in protected_paths]
            relevant_available = validation_available or not any(
                item.get("reason") in UNAVAILABLE_REASONS for item in relevant)
            decision = tracker.review(relevant, outcome.missing, execution,
                                      validation_available=relevant_available)
        else:
            decision = None
        repair_reason = decision.reason if decision else "no_repair_needed"
        if delivery_blocked and repair_reason == "local_artifact_repair_allowed":
            repair_reason = "delivery_failed"
        protected_changed = repair_reason == "protected_artifact_changed"
        pins = self._analysis_verified_files.setdefault(step.id, {})
        if protected_changed:
            outcome.status = "partial" if pins or checked_files else "failed"
            outcome.reason_code = "execution_failed"
        if not protected_changed:
            receipts = getattr(self, "_analysis_verified_receipts", None)
            if receipts is None:
                receipts = self._analysis_verified_receipts = {}
            pinned_receipts = receipts.setdefault(step.id, {})
            records_by_path = {item.get("path"): item for item in records if item.get("valid") is True}
            for item in checked_files:
                if item.file_path not in rejected_paths:
                    pins.setdefault(item.file_path, item)
                    if item.file_path in records_by_path:
                        pinned_receipts.setdefault(item.file_path, dict(records_by_path[item.file_path]))
            for path in rejected_paths:
                pinned_receipts.pop(path, None)

        # Production diagnostics survive a restart, separately from chat/SSE.
        # An audit failure prevents a new autonomous repair, not an honest
        # successful delivery of already checked bytes.
        audit_ok = True
        audit = getattr(self, "_delivery_audit_store", None)
        if audit is not None or source_seq is not None:
            if audit is None:
                from app.domain.services.analysis_delivery_audit import AnalysisDeliveryAuditStore
                audit = self._delivery_audit_store = AnalysisDeliveryAuditStore()
            try:
                async with asyncio.timeout(3):
                    await audit.record(user_id=self._user_id, session_id=self._session_id,
                        input_seq=source_seq, step_id=step.id, records=records, requirements=requirements,
                        outcome=outcome, repair_reason=repair_reason)
            except Exception as error:
                audit_ok = False
                logger.warning("Analysis delivery audit unavailable error_type=%s", type(error).__name__)
        # Failed transport is not missing computation. Do not ask the model to
        # regenerate verified bytes while storage is still unavailable.
        allowed = bool(decision and decision.allowed and audit_ok and not delivery_blocked)
        if allowed:
            # A local repair may later alter a protected working copy. Freeze
            # the observations that justified the immutable uploads now, so
            # those original facts can still be reviewed without trusting
            # replacement bytes or a later model draft.
            from app.domain.services.analysis_answer_review import AnswerEvidence
            snapshots = getattr(self, "_analysis_preserved_answer_evidence", None)
            if snapshots is None:
                snapshots = self._analysis_preserved_answer_evidence = {}
            snapshots.pop(step.id, None)
            evidence = getattr(self, "_analysis_answer_evidence", None)
            if isinstance(evidence, AnswerEvidence):
                step_ids = {item.id for item in getattr(getattr(self._flow, "plan", None), "steps", [])}
                try:
                    snapshots[step.id] = AnswerEvidence.from_checkpoint_snapshot(
                        evidence.checkpoint_snapshot(step_ids=step_ids), step_ids=step_ids)
                except ValueError:
                    # Missing or oversized proof cannot authorize prose based
                    # on a subsequently changed output.
                    logger.warning("Analysis repair evidence snapshot unavailable")
            feedback = dict(decision.feedback)
            feedback["previous_analysis"] = (step.result or "")[:32000]
            self._flow._artifact_repair_requests[step.id] = feedback
        logger.info("analysis_artifact_repair allowed=%s reason=%s files=%d missing=%d",
                    allowed, repair_reason, len(records), len(outcome.missing))
        return allowed, protected_changed

    def _observe_answer_tool_event(self, event: ToolEvent) -> None:
        """Join source versions only to the current executor's private receipt.

        An identically named plugin, public result field or another step's
        ledger cannot attest that this step ran particular source bytes.
        This lookup observes host memory only; it never invokes a tool.
        """
        evidence = self._analysis_answer_evidence
        self._observe_scope_repair_tool(event, evidence.current_step_id)
        proof = None
        if event.status == ToolStatus.CALLED and event.function_name == "program_run":
            from app.domain.services.execution_evidence import ToolExecutionLedger
            from app.domain.services.program_execution import trusted_program_execution_feedback
            steps = getattr(getattr(self._flow, "plan", None), "steps", [])
            step = next((item for item in steps if item.id == evidence.current_step_id), None)
            if step is not None:
                profile = getattr(self._flow, "enabled_subagents", {}).get(step.agent)
                executor = (getattr(self._flow, "vision", None)
                            if getattr(profile, "handler_type", None) == "vision" else
                            getattr(self._flow, "_domain_agents", {}).get(
                                step.agent, getattr(self._flow, "executor", None)))
                ledger = getattr(executor, "_tool_execution_ledger", None)
                if type(ledger) is ToolExecutionLedger:
                    try:
                        tool = executor.get_tool(event.function_name)
                        if event.tool_name == getattr(getattr(tool, "toolkit", None), "name", None):
                            proof = trusted_program_execution_feedback(tool, {
                                "name": event.function_name, "id": event.tool_call_id,
                                "args": event.function_args}, None, ledger)
                    except Exception:
                        # An unavailable private link does not invalidate the
                        # observed result, but cannot attest source execution.
                        logger.warning("Analysis program source proof unavailable")
        evidence.observe(event, trusted_program_execution=proof)

    def _observe_scope_repair_tool(self, event, step_id):
        """Record only resolved native reads and affirmative pre-dispatch failures."""
        states = getattr(self, "_analysis_scope_observations", None)
        if states is None or not step_id or event.status != ToolStatus.CALLED:
            return
        state = states.setdefault(step_id, {"reads": set(), "blocked": False})
        from app.domain.services.execution_evidence import ToolExecutionLedger
        from app.domain.services.tools.file import FileToolkit
        from app.domain.models.tool_result import ToolResult
        steps = getattr(getattr(self._flow, "plan", None), "steps", [])
        step = next((item for item in steps if item.id == step_id), None)
        executor = (getattr(self._flow, "_domain_agents", {}).get(step.agent, getattr(self._flow, "executor", None))
                    if step is not None else None)
        ledger = getattr(executor, "_tool_execution_ledger", None)
        result = event.function_result
        if type(ledger) is not ToolExecutionLedger or not isinstance(result, ToolResult):
            state["blocked"] = True
            return
        if result.success is False and ledger.failure_state(event.tool_call_id) == "not_started":
            return
        try:
            tool = executor.get_tool(event.function_name)
            args = event.function_args
            native = (type(getattr(tool, "toolkit", None)) is FileToolkit
                      and getattr(getattr(tool, "_tool", None), "coroutine", None) is FileToolkit.file_read.coroutine
                      and event.function_name == "file_read" and event.tool_name == "file")
            full = (isinstance(args, dict) and args.get("start_line") is None
                    and args.get("end_line") is None and args.get("sudo", False) is False)
            content = result.data.get("content") if isinstance(result.data, dict) else None
            if native and full and result.success is True and isinstance(content, str) and content.strip():
                path = args.get("file")
                if isinstance(path, str) and path in {item.get("path") for item in self._analysis_source_fingerprints or []}:
                    state["reads"].add(path)
                    return
        except (AttributeError, TypeError, ValueError):
            pass
        state["blocked"] = True

    async def _review_scope_completion(self, step, message, files, requirements, outcome, execution,
                                       reviewed, *, source_seq):
        """Narrow same-input completion after reads; no scientific-error reexecutor."""
        from datetime import UTC, datetime
        from app.domain.services.analysis_scope_repair import ScopeRepairGuards, ScopeRepairTracker, ScopeReviewBinding
        from app.domain.services.analysis_checkpoint import fingerprints, approved_upload_paths
        from app.domain.services.model_runtime import current_analysis_budget
        from app.domain.services.analysis_scope_repair_audit import ScopeRepairAuditStore
        from copy import deepcopy
        # Freeze the complete review verdict before any runtime/storage await.
        # Mutable diagnostic mappings must not switch the eligible candidate.
        metadata = deepcopy(getattr(reviewed, "metadata", {}))
        scope = metadata.get("answer_scope_review", {})
        final_review = metadata.get("final_candidate_review", {})
        candidate = getattr(reviewed, "scope_completion_candidate", None)
        original = getattr(reviewed, "scope_completion_request", None)
        # An unavailable scientific check alone is never a reason to execute.
        if (files or requirements or message._resume_checkpoint or step.attachments
                or type(source_seq) is not int or source_seq < 1
                or (step.inputs or {}).get("dataset_intent") != "analysis"
                or scope.get("status") != "incomplete" or scope.get("completion_blocker") != "none"
                or candidate is None or original != message.message
                or metadata.get("evidence_truncated") is not False
                or metadata.get("review_schema_repair_attempted") or metadata.get("citation_repair_attempted")
                or not isinstance(final_review, dict)
                or type(final_review.get("protocol_attempts")) is not int
                or final_review.get("protocol_attempts") != 1
                or final_review.get("schema_recovered") or final_review.get("error")
                or any(issue.blocking for issue in outcome.issues)):
            return False
        def rejected(value):
            if isinstance(value, dict):
                return value.get("status") == "rejected" or any(rejected(v) for v in value.values())
            return isinstance(value, list) and any(rejected(v) for v in value)
        if any(rejected(metadata.get(key)) for key in ("answer_scientific_review", "scientific_review", "report_review")):
            return False
        resolution = getattr(self, "_front_controller_resolution", None)
        state = getattr(self, "_analysis_scope_observations", {}).get(step.id, {})
        trackers = getattr(self, "_analysis_scope_trackers", None)
        budget = current_analysis_budget()
        delivery, identity = getattr(self, "_input_delivery", None), getattr(self, "_accepted_input_key", None)
        if (trackers is None or budget is None or delivery is None or identity is None
                or not self._answer_scientific_scope(step, message)
                or getattr(resolution, "mode", None) != "sandbox"
                or resolution.decision.safety.allowed is not True
                or not state.get("reads") or state.get("blocked") is not False
                or model_stop_reason()):
            return False
        key = source_seq
        tracker = trackers.setdefault(key, ScopeRepairTracker(input_seq=source_seq, step_id=step.id, request=message.message))
        if tracker.snapshot()["consumed"]:
            return False
        try:
            await delivery._require_live(self._session_id, identity)
            snapshot = await budget.snapshot()
            if (snapshot.lineage_id != message._budget_lineage_id
                    or snapshot.model_call_limit is not None and snapshot.model_calls >= snapshot.model_call_limit
                    or snapshot.model_token_limit is not None and snapshot.charged_tokens >= snapshot.model_token_limit
                    or snapshot.hard_limit is not None and snapshot.tool_batches_used >= snapshot.hard_limit):
                return False
            expected = [dict(item) for item in self._analysis_source_fingerprints if item.get("path") in state["reads"]]
            current = await fingerprints(self._sandbox, sorted(state["reads"]), approved_upload_paths=approved_upload_paths(message))
            if not current or sorted(current, key=lambda item:item["path"]) != sorted(expected, key=lambda item:item["path"]):
                return False
            await delivery._require_live(self._session_id, identity)
            decision = tracker.review(binding=ScopeReviewBinding(input_seq=source_seq, step_id=step.id,
                    request=original, paragraphs=candidate, metadata=scope), execution=execution,
                guards=ScopeRepairGuards(analysis_authorized=True, current_input=True, original_request_complete=True,
                    candidate_complete=True, read_only_observations=True, prerequisites_met=scope.get("completion_blocker")=="none",
                    no_user_input_required=scope.get("completion_blocker")=="none", no_policy_refusal=True, runtime_live=True,
                    no_artifacts_or_requirements=True, no_review_repair=True),
                now=datetime.now(UTC), deadline_at=snapshot.deadline_at)
            if not decision.allowed:
                return False
            audit = getattr(self, "_scope_repair_audit_store", None) or ScopeRepairAuditStore()
            async with asyncio.timeout(3):
                recorded = await audit.claim(user_id=self._user_id, session_id=self._session_id, snapshot=tracker.snapshot())
            if not recorded:
                return False
            await delivery._require_live(self._session_id, identity)
            if (getattr(self, "_accepted_input_key", None) != identity or message.message != original or model_stop_reason()
                    or snapshot.deadline_at is not None and datetime.now(UTC) >= snapshot.deadline_at):
                return False
            feedback = dict(decision.feedback)
            feedback["previous_analysis"] = "\n\n".join(candidate)
            self._flow._artifact_repair_requests[step.id] = feedback
            logger.info("analysis_scope_completion allowed=True reason=scope_completion_allowed")
            return True
        except Exception as error:
            # Runtime admission failures are not permission to retry. Original
            # CancelledError and ModelBudgetStopped (BaseException) propagate.
            logger.info("analysis_scope_completion allowed=False reason=host_evidence_unavailable error_type=%s", type(error).__name__)
            return False

    def _answer_scientific_scope(self, step, message) -> bool:
        """Select factual analysis review from this admission and step only.

        Input identities do not by themselves turn a greeting or a file-copy
        operation into analysis. Conversely, dataset structure/unit explanation
        is analysis even when it produces no artifact or fitted model.
        """
        resolution = getattr(self, "_front_controller_resolution", None)
        admission_mode = getattr(resolution, "mode", None)
        inputs = step.inputs or {}
        mode = inputs.get("execution_mode")
        intent = inputs.get("dataset_intent")
        mode = mode.strip().casefold() if isinstance(mode, str) else None
        intent = intent.strip().casefold() if isinstance(intent, str) else None
        if admission_mode in {"direct", "catalog", "reject"}:
            return False
        if intent in {
            "file_preview", "preview_file", "preview", "file_inventory", "inventory", "files",
            "catalog_description", "catalog_semantics", "dataset_purpose", "purpose", "use_cases",
            "catalog_metadata", "metadata", "size", "file_count", "file_formats", "formats",
        }:
            # Copy/navigation and pure catalog metadata do not claim measured
            # scientific findings. Mixed field/unit explanations use analysis
            # or file_structure, not a test of the model's final draft.
            return False
        context = message.analysis_inputs
        has_current_inputs = bool(
            message.datasets or (context is not None and context.sources)
            or any(isinstance(value, str) and value.strip() for value in message.attachment_file_ids)
        )
        if not has_current_inputs:
            return False
        if mode == "dataset_fast_path":
            return True
        if intent in {
            "analysis", "custom_question", "question", "visualization", "visualisation",
            "visualize", "visualise", "plot", "file_structure", "archive_structure",
        }:
            return True
        # Skill/domain/multistep analysis can bypass the one-step dataset fast
        # path. Its host admission still binds the request to file contents;
        # do not let a missing planner intent disable factual verification.
        decision = getattr(resolution, "decision", None)
        execution = getattr(decision, "execution", None)
        return admission_mode == "sandbox" and getattr(execution, "required_evidence", None) == "file_content"

    async def _review_analysis_answer(self, step, message, files, requirements, outcome, *, evidence=None):
        """Ground prose independently of the minimum artifact-count verdict."""
        from app.domain.services.analysis_answer_review import AnswerEvidence, review_answer
        if evidence is None:
            evidence = getattr(self, "_analysis_answer_evidence", None) or AnswerEvidence()
        executor = getattr(self._flow, "_domain_agents", {}).get(step.agent, getattr(self._flow, "executor", None))
        reviewer = getattr(executor, "review_delivery_answer", None)
        question = json.dumps({"user_question": message.message, "current_step": {
            "id": step.id, "description": step.description,
            "target_files": (step.inputs or {}).get("target_files", []),
            "target_file": (step.inputs or {}).get("target_file"),
        }}, ensure_ascii=False)
        arguments = dict(question=question, draft=step.result or "", files=files,
                         evidence=evidence, requirements=requirements,
                         language=getattr(getattr(self._flow, "plan", None), "language", None) or "zh",
                         answer_scientific_scope=self._answer_scientific_scope(step, message))
        from app.domain.services.analysis_report_review import load_report_targets
        targets = await load_report_targets(files=files, storage=getattr(self, "_file_storage", None),
            user_id=getattr(self, "_user_id", ""), session_id=getattr(self, "_session_id", ""),
            requirements=requirements)
        if targets:
            arguments["report_targets"] = targets
        if callable(reviewer):
            reviewed = await reviewer(**arguments)
        else:
            async def unavailable(_messages):
                raise RuntimeError("answer_reviewer_unavailable")
            reviewed = await review_answer(ask=unavailable, **arguments)
        from app.domain.services.analysis_report_review import changed_report_indices
        changed_reports = changed_report_indices(targets, files)
        if changed_reports:
            from dataclasses import replace
            from copy import deepcopy
            from app.domain.services.analysis_report_review import report_review_metadata
            report = deepcopy(reviewed.metadata.get("report_review")) or report_review_metadata(
                targets, None, citations=lambda *_: [], lookup={})
            for record in report["reports"]:
                if record["report_index"] in changed_reports:
                    record.update(status="unavailable", reason="delivery_version_changed",
                        unverified_ranges=[[0, record["size"]]] if record["size"] is not None else [])
            report["status"] = "rejected" if any(item["status"] == "rejected" for item in report["reports"]) else "unavailable"
            metadata = {**reviewed.metadata, "status": "unavailable", "report_review": report}
            if any(targets[index].target_kind == "structured" for index in changed_reports):
                from app.domain.services.analysis_scientific_review import scientific_review_metadata
                scientific = scientific_review_metadata(targets, None, citations=lambda *_: [], lookup={},
                                                         evidence_complete=False)
                scientific.update(status="unavailable", reason="delivery_version_changed")
                metadata["scientific_review"] = scientific
            if reviewed.status != "unavailable":
                metadata.update(reason="scientific_validation_unavailable" if any(
                    target.target_kind == "structured" for target in targets) else "report_validation_unavailable",
                                validation_state="unavailable",
                                chat_review_status=reviewed.status)
            notice = ("成果版本已变化，先前内容核验不适用于当前附件。"
                      if arguments["language"].lower().startswith("zh") else
                      "The artifact version changed; its earlier review does not verify the current attachment.")
            reviewed = replace(reviewed, text=reviewed.text + "\n\n" + notice, status="unavailable", metadata=metadata)
        step.result = reviewed.text
        step.outputs["answer_review"] = {"version": 1, "status": reviewed.status, **reviewed.metadata,
            "dataset_ids": sorted({item.dataset_id for item in message.datasets}),
            "input_file_ids": sorted(set(message.attachment_file_ids))}
        if reviewed.status == "unavailable" and outcome.status == "succeeded":
            outcome.status = "partial" if files else "failed"
            outcome.reason_code = (
                reviewed.metadata["reason"] if reviewed.metadata.get("reason") in {
                    "report_validation_rejected", "report_validation_unavailable",
                    "scientific_validation_rejected", "scientific_validation_unavailable"}
                else "answer_objectives_missing" if reviewed.metadata.get("reason") == "answer_coverage_incomplete"
                else "answer_validation_rejected" if reviewed.metadata.get("validation_state") == "rejected"
                else "answer_validation_unavailable"
            )
        elif reviewed.missing_requirement_indices and outcome.status == "succeeded":
            # Indices bind only to the pre-execution contract, never to a
            # filename or extra analytical method invented by the final draft.
            outcome.missing = [requirements[index].model_copy(deep=True)
                               for index in reviewed.missing_requirement_indices]
            outcome.status = "partial" if files else "failed"
            outcome.reason_code = "analytical_requirements_missing"
        logger.info("analysis_answer_review status=%s reason=%s missing_objectives=%d citation_diagnostics=%s",
                    reviewed.status, reviewed.metadata.get("reason", "none"),
                    len(reviewed.missing_requirement_indices),
                    json.dumps(reviewed.metadata.get("citation_diagnostics", {}), sort_keys=True))
        return reviewed

    async def _finalize_analysis_step(self, event, message, files, *, source_seq=None):
        from app.domain.models.plan import ExecutionStatus
        from app.domain.models.event import StepStatus
        from app.domain.services.analysis_completion import (
            artifact_kind, assess_delivery, outcome_message, requirements_for_step, verified_deliveries,
        )
        from app.domain.services.analysis_checkpoint import save_checkpoint
        public_step = event.step
        current_plan = getattr(self._flow, "plan", None)
        matches = [item for item in getattr(current_plan, "steps", []) if item.id == public_step.id]
        step = matches[0] if len(matches) == 1 else public_step
        step.outputs["model_execution_success"] = bool(step.success)
        declared_paths = tuple(step.attachments or [])
        requirements = requirements_for_step(step, message)
        step.deliverables = requirements
        requested_code = list(getattr(self, "_requested_code_deliverables", []))
        for item in [*message.deliverables, *requirements]:
            if item.kind == "code" and item not in requested_code:
                requested_code.append(item)
        self._requested_code_deliverables = requested_code
        files = [item for item in files if self._is_deliverable_artifact(item.file_path)]
        execution = step.outputs.get("execution_outcome", {})
        unconfirmed = bool(execution.get("has_unconfirmed_tool_execution") or execution.get("side_effect_state") == "unknown")
        candidates = set(step.attachments or []) | {item.file_path for item in files if item.file_path}
        candidates.update(path for requirement in requirements for path in requirement.output_paths)
        candidates.update(getattr(self, "_pending_artifact_paths", set()))
        pinned = getattr(self, "_analysis_verified_files", {}).get(step.id, {})
        candidates.update(pinned)
        intent = (step.inputs or {}).get("dataset_intent")
        handler = getattr(self._flow, "enabled_subagents", {}).get(step.agent)
        review_prose = (getattr(handler, "handler_type", None) == "execution"
                        and intent not in {"file_preview", "catalog_metadata", "catalog_description"})
        if not requirements and not candidates and step.success and not unconfirmed and not review_prose:
            return files
        if requirements:
            candidates.update(path for path in await self._list_sandbox_artifacts()
                              if path not in getattr(self, "_artifact_baseline_paths", set()))
        if message._resume_checkpoint:
            candidates.update(message._resume_checkpoint.get("progress", {}).get("verified_files", []))
        def expected_kind(path):
            # A structured JSON table is a table only when the requested
            # contract explicitly says so. Extension defaults serve other cases.
            suffix = PurePosixPath(path).suffix.lower().lstrip(".")
            declared = {item.kind for item in requirements if item.kind != "any" and suffix in item.formats}
            return next(iter(declared)) if len(declared) == 1 else artifact_kind(path)

        items = [{"path": path, "kind": expected_kind(path)} for path in sorted(candidates)
                 if expected_kind(path) and self._is_deliverable_artifact(path) and path.startswith("/home/ubuntu/output/")
                 and str(PurePosixPath(path)) == path and ".." not in PurePosixPath(path).parts
                 and "\\" not in path and not any(ord(char) < 32 for char in path)]
        records, available = [], True
        if items:
            validate = getattr(self._sandbox, "validate_artifacts", None)
            # A failed batch must not hide evidence from other batches.
            for offset in range(0, len(items), 32):
                batch = items[offset:offset + 32]
                try:
                    result = await validate(batch) if callable(validate) else None
                    batch_ok = bool(result and result.success and isinstance(result.data, dict)
                                    and type(result.data.get("version")) is int
                                    and result.data.get("version") == 1)
                    batch_records = result.data.get("files", []) if batch_ok else []
                    batch_ok = (batch_ok and isinstance(batch_records, list) and len(batch_records) == len(batch)
                                and all(isinstance(item, dict) for item in batch_records)
                                and {item.get("path") for item in batch_records} == {item["path"] for item in batch})
                except Exception:
                    batch_ok = False
                if batch_ok:
                    records.extend(batch_records)
                else:
                    available = False
                    records.extend({**item, "expected_kind": item["kind"], "valid": False,
                                    "reason": "validation_unavailable"} for item in batch)
        valid = {item["path"]: item for item in records if item.get("valid") is True}
        def uploaded_matches(item):
            return (isinstance(item.file_id, str) and bool(item.file_id.strip()) and item.file_path in valid
                    and item.size == valid[item.file_path].get("size")
                    and (item.metadata or {}).get("artifact_sha256") == valid[item.file_path].get("sha256"))
        checked_files = [item for item in files if uploaded_matches(item)]
        uploaded_paths = {item.file_path for item in checked_files}
        for path in valid.keys() - uploaded_paths - pinned.keys():
            # Retry delivery, not analysis. A fresh upload must match the exact
            # validated bytes, otherwise it remains unverified and cannot count.
            info = await self._sync_file_to_storage(path)
            if info is not None and uploaded_matches(info):
                checked_files.append(info)
        checked_files = verified_deliveries(records, checked_files, validation_available=available)
        final_code = execution.get("code", "")
        stop_code = ("tool_execution_unknown" if unconfirmed else
                     final_code if final_code not in {"", "running", "completed"} else
                     execution.get("last_tool_error_code") or final_code)
        outcome = assess_delivery(requirements, records, checked_files,
                                  execution_success=bool(step.success and not unconfirmed), stop_code=stop_code,
                                  validation_available=available)
        answer_review = None
        if review_prose and outcome.status == "succeeded":
            answer_review = await self._review_analysis_answer(step, message, checked_files, requirements, outcome)
        repair_pending, protected_changed = await self._review_artifact_repair(
            step, message, records, checked_files, requirements, outcome, execution,
            validation_available=available, source_seq=source_seq,
            semantic_missing=([requirements[index] for index in answer_review.missing_requirement_indices]
                              if answer_review is not None and answer_review.status != "unavailable" else ()))
        if not repair_pending and not protected_changed and answer_review is not None:
            repair_pending = await self._review_scope_completion(step, message, checked_files, requirements,
                outcome, execution, answer_review, source_seq=source_seq)
        if answer_review is not None and answer_review.status == "corrected" and not repair_pending:
            from app.domain.services.analysis_answer_review import rejected_missing_claim_paths
            from app.domain.services.analysis_completion import issues_from_records
            rejected = rejected_missing_claim_paths(declared_paths=declared_paths, requirements=requirements,
                records=records, evidence=self._analysis_answer_evidence)
            # Full receipts were already recorded privately above. Only pure
            # rejected draft assertions disappear from public diagnostics;
            # actual attempts, required outputs and uncertain provenance stay.
            outcome.issues = issues_from_records(requirements,
                [item for item in records if item.get("path") not in rejected], checked_files,
                validation_available=available)
            step.outputs["answer_review"]["discarded_attachment_claim_count"] = len(rejected)
        if protected_changed:
            # Previously uploaded bytes are immutable. Do not substitute a
            # changed working copy or claim the repair preserved its evidence.
            checked_files = list(pinned.values()) + [item for item in checked_files if item.file_path not in pinned]
            outcome.status = "partial" if checked_files else "failed"
            outcome.reason_code = "execution_failed"
        if not repair_pending:
            # Immutable, already checked uploads remain usable even when the
            # latest validation service or tool receipt is unavailable.
            preserved = getattr(self, "_analysis_verified_files", {}).get(step.id, {})
            checked_files = list(preserved.values()) + [item for item in checked_files if item.file_path not in preserved]
            if outcome.status != "succeeded":
                existing_ids = {item.file_id for item in checked_files}
                checked_files.extend(item for key, item in getattr(self, "_analysis_rejected_versions", {}).items()
                                     if key not in existing_ids)
            if checked_files and outcome.status == "failed":
                # A later validation outage must not erase durable, previously
                # validated results from the user's completion status.
                outcome.status = "partial"
            if preserved and answer_review is None and outcome.status != "succeeded":
                # Required files already delivered as immutable uploads remain
                # fulfilled even if the working copy cannot be checked now.
                # This reconciles only missing file slots, never semantic review
                # or the failure reason, and cannot turn an outage into success.
                pinned_receipts = getattr(self, "_analysis_verified_receipts", {}).get(step.id, {})
                retained_records = [pinned_receipts[path] for path in preserved if path in pinned_receipts]
                retained_paths = {item["path"] for item in retained_records}
                reconciled = assess_delivery(requirements,
                    retained_records + [item for item in records if item.get("path") not in retained_paths],
                    checked_files, execution_success=False, stop_code=outcome.reason_code,
                    validation_available=available)
                outcome.missing = reconciled.missing
                outcome.issues = reconciled.issues
        step.outcome = outcome
        if not repair_pending:
            # Future plans, summaries and checkpoints must not retain rejected
            # model attachment claims after exact-byte delivery reconciliation.
            step.attachments = [item.file_path for item in checked_files]
        step.success = outcome.status == "succeeded"
        step.status = ExecutionStatus.COMPLETED if step.success else ExecutionStatus.FAILED
        event.status = StepStatus.COMPLETED if step.success else StepStatus.FAILED
        if not step.success:
            had_unverified_draft = bool(step.result)
            partial_draft = step.result or ""
            grounded_answer = answer_review is not None
            if not repair_pending and not grounded_answer:
                # Clear before saving a continuation: its plan/progress must
                # not label an unverified draft as evidence for a later turn.
                # Local repair already received the draft privately above.
                step.result = None
            # An uncertain side-effecting call must not be offered as an
            # automatically replayable script. Only explicit safe checkpoints.
            safe = not execution.get("has_unconfirmed_tool_execution", False)
            safe = safe and execution.get("side_effect_state") != "unknown"
            proof = execution.get("execution_evidence", {})
            if proof:
                # A confirmed exit is not permission to replay an arbitrary
                # shell/plugin step. Only upload-only continuation can bypass
                # computation without a genuine replay-safety guarantee.
                safe = safe and (proof.get("replay_safe") is True or
                    (outcome.reason_code == "delivery_failed" and step.outputs.get("model_execution_success") is True))
            from app.domain.services.model_runtime import current_analysis_budget
            budget = current_analysis_budget()
            if safe and budget is not None:
                try:
                    snapshot = await budget.snapshot()
                    from datetime import UTC, datetime
                    deadline = snapshot.deadline_at
                    if deadline is not None:
                        deadline = deadline.replace(tzinfo=UTC) if deadline.tzinfo is None else deadline
                    # Production requests are metered, not quota-limited. A
                    # missing ceiling must not disable a safe checkpoint.
                    safe = (deadline is None or datetime.now(UTC) < deadline) and (
                        outcome.reason_code == "delivery_failed" or all(
                            limit is None or used < limit for used, limit in (
                                (snapshot.tool_batches_used, snapshot.hard_limit),
                                (snapshot.model_calls, snapshot.model_call_limit),
                                (snapshot.charged_tokens, snapshot.model_token_limit),
                            )
                        )
                    )
                except Exception:
                    safe = False
            if (safe and not repair_pending and not protected_changed and answer_review is None
                    and message.datasets and source_seq is not None):
                try:
                    token = await save_checkpoint(
                        self._session_repository, self._sandbox, self._session_id, self._user_id,
                        message, getattr(self._flow, "plan", None),
                        source_fingerprints=getattr(self, "_analysis_source_fingerprints", None),
                        records=records, delivered=checked_files, reason_code=outcome.reason_code,
                        source_seq=source_seq,
                        answer_evidence=getattr(self, "_analysis_answer_evidence", None))
                    if token:
                        outcome.can_resume, outcome.resume_from = True, token
                except Exception as error:
                    logger.warning("Analysis checkpoint unavailable error_type=%s", type(error).__name__)
            evidence = (getattr(self, "_analysis_preserved_answer_evidence", {}).get(step.id)
                        if protected_changed else getattr(self, "_analysis_answer_evidence", None))
            has_terminal_observations = evidence is not None and any(
                item.get("kind") == "tool_result" and item.get("state") in {"succeeded", "failed"}
                and not item.get("write_only") for item in evidence.render_sources())
            if (review_prose and not repair_pending and not unconfirmed
                    and answer_review is None and has_terminal_observations):
                # Missing files must not erase independently observed statistics
                # or the reason an input cannot support the requested analysis.
                # Review the draft read-only after all repair decisions and safe
                # checkpoint capture; keep the host's original failure verdict.
                step.result = partial_draft
                answer_review = await self._review_analysis_answer(
                    step, message, checked_files, requirements, outcome.model_copy(deep=True), evidence=evidence)
                grounded_answer = True
                step.outputs["answer_review"]["partial_delivery_review"] = True
                if protected_changed:
                    step.outputs["answer_review"]["preserved_evidence_review"] = True
                step.result = outcome_message(outcome, delivered_files=checked_files) + "\n\n" + answer_review.text
            if not step.result:
                step.result = outcome_message(outcome, delivered_files=checked_files)
            if protected_changed and not repair_pending:
                step.result += "\n\n补齐过程中已有文件发生变化，已保留此前核验通过的版本。"
            if had_unverified_draft and not repair_pending and not grounded_answer:
                # A disclaimer cannot turn an unexecuted draft into evidence.
                # Keep the original draft in the private execution memory and
                # repair feedback, not in a final answer beside failed receipts.
                # Already verified uploads and other successful steps survive.
                step.result += (
                    "\n\n本步骤的分析说明尚未通过完整核验，未作为已确认结论发布。"
                )
            step.error = outcome.reason_code
        logger.info("analysis_outcome status=%s reason=%s missing=%d delivered=%d resumable=%s",
                    outcome.status, outcome.reason_code, len(outcome.missing), len(checked_files), outcome.can_resume)
        if public_step is not step:
            # Public StepEvents can be redacted copies. Reflect execution state
            # in both places without writing redacted source inputs into the plan.
            for field in ("status", "success", "result", "error", "outcome", "deliverables", "outputs", "attachments"):
                setattr(public_step, field, getattr(step, field))
        return checked_files

    @staticmethod
    def _is_delivery_only_continuation(checkpoint) -> bool:
        """Only replay upload/validation when execution already finished.

        A successful upload must never turn an unfinished computation into a
        successful analysis. Untouched later plan steps therefore disqualify
        this path, as does any uncertain tool side effect.
        """
        if not checkpoint or checkpoint.get("reason_code") != "delivery_failed":
            return False
        steps = checkpoint.get("plan", {}).get("steps", [])
        return bool(steps) and all(
            step.get("success") or (
                step.get("outputs", {}).get("model_execution_success") is True
                and not step.get("outputs", {}).get("execution_outcome", {}).get("has_unconfirmed_tool_execution")
            ) for step in steps)

    async def _resume_delivery_only(self, checkpoint):
        from app.domain.models.plan import Plan, ExecutionStatus
        from app.domain.models.event import PlanEvent, PlanStatus
        self._flow.plan = Plan.model_validate(checkpoint["plan"])
        self._flow.status = AgentStatus.EXECUTING
        for step in self._flow.plan.steps:
            if step.success:
                continue
            step.outcome = None
            step.result = None
            step.attachments = []
            step.status = ExecutionStatus.RUNNING
            yield StepEvent(status=StepStatus.STARTED, step=step)
            step.attachments = list(checkpoint.get("progress", {}).get("verified_files", []))
            step.success = True
            step.status = ExecutionStatus.COMPLETED
            step.result = "已完成已有成果的验证与交付。"
            yield StepEvent(status=StepStatus.COMPLETED, step=step)
            yield PlanEvent(status=PlanStatus.UPDATED, plan=self._flow.plan)
            if not step.success:
                break
        self._flow.plan.status = (ExecutionStatus.COMPLETED if all(step.success for step in self._flow.plan.steps)
                                  else ExecutionStatus.FAILED)
        yield PlanEvent(status=PlanStatus.COMPLETED, plan=self._flow.plan)
        self._flow.status = AgentStatus.IDLE
        yield DoneEvent()

    async def _run_flow(self, message: Message, *, task_id: str | None = None,
                        trigger_event_seq: int | None = None) -> AsyncGenerator[BaseEvent, None]:
        from contextlib import ExitStack, aclosing
        # The stack lives outside the generator producing events so model,
        # validation, upload and checkpoint work share the same request identity
        # and accounting, without a cumulative execution quota.
        with ExitStack() as runtime_stack:
            async with aclosing(self._run_authorized_flow(message, task_id=task_id,
                    trigger_event_seq=trigger_event_seq, runtime_stack=runtime_stack)) as events:
                async for event in events:
                    yield event

    async def _open_analysis_runtime(self, message, trigger_event_seq, runtime_stack):
        from app.domain.services.analysis_budget import AnalysisBudgetService, BudgetUnavailableError
        from app.domain.services.analysis_checkpoint import configuration_digest
        from app.domain.services.analysis_recovery import AnalysisRecoveryContext, analysis_recovery_scope
        from app.domain.services.execution_identity import private_identity_hmac
        # Isolated legacy runners have no durable input. Production admission
        # always supplies its server-assigned event sequence.
        if type(trigger_event_seq) is not int or trigger_event_seq < 1:
            return
        session = await self._session_repository.find_by_id_and_user_id(self._session_id, self._user_id)
        checkpoint = message._resume_checkpoint or {}
        lineage_id = checkpoint.get("budget_lineage_id")
        if checkpoint and not lineage_id and not self._is_delivery_only_continuation(checkpoint):
            raise BudgetUnavailableError("legacy_checkpoint_budget_missing")
        if checkpoint and not lineage_id:
            return  # Legacy upload-only recovery cannot run analysis tools.
        scope_digest = private_identity_hmac({"purpose": "analysis-budget-scope/v1",
            "goal": message.message, "configuration": configuration_digest(session),
            "sandbox_id": self._sandbox.id, "datasets": [item.dataset_id for item in message.datasets],
            "targets": message.controller_target_files, "sources": self._analysis_source_fingerprints,
            "attachments": message.attachment_file_ids, "skills": message.skills,
            "mcp_servers": message.mcp_servers, "mcp_access_all": message.mcp_access_all})

        delivery = getattr(self, "_input_delivery", None)
        identity = getattr(self, "_accepted_input_key", None)

        async def require_live():
            if getattr(self, "_accepted_input_key", None) != identity:
                from app.domain.services.input_delivery import InputLeaseLost
                raise InputLeaseLost()
            if delivery is not None and identity is not None:
                await delivery._require_live(self._session_id, identity)

        service = getattr(self, "_analysis_budget_service", None) or AnalysisBudgetService()
        handle = await service.open(user_id=self._user_id, session_id=self._session_id,
            origin_input_id=str(checkpoint.get("budget_origin_seq", trigger_event_seq)), scope_digest=scope_digest,
            lineage_id=lineage_id, require_live=require_live)
        recovery = AnalysisRecoveryContext(review=lambda *args, **kwargs: self._review_analysis_budget(
            message, recovery, handle, *args, **kwargs))
        from app.domain.services.analysis_checkpoint import source_paths
        recovery.progress.read_scope_paths = frozenset(source_paths(message) + list(message.attachments))
        # These lists are private host-authored identities protected by the
        # checkpoint HMAC, never supplied by the continuation request.
        recovery.progress.evidence.update(checkpoint.get("budget_read_evidence", []))
        recovery.artifact_evidence.update(checkpoint.get("budget_artifact_evidence", []))
        message._budget_lineage_id = handle.lineage_id
        message._budget_origin_seq = checkpoint.get("budget_origin_seq", trigger_event_seq)
        runtime_stack.enter_context(analysis_budget_scope(handle))
        runtime_stack.enter_context(analysis_recovery_scope(recovery))

    async def _review_analysis_budget(self, message, recovery, handle, next_calls, *, review_needed):
        from app.domain.services.analysis_budget import BudgetEvidence
        from app.domain.services.analysis_checkpoint import fingerprints, source_paths
        from app.domain.services.analysis_completion import artifact_kind
        from app.domain.services.execution_identity import private_identity_hmac
        from app.domain.services.tools.tool_contract import validate_tool_arguments
        sources_stable = not (message.datasets or message.attachments or message.attachment_file_ids)
        required_work = True
        bounded = isinstance(next_calls, list) and 0 < len(next_calls) <= 4
        if bounded:
            executor = getattr(self._flow, "executor", None)
            for call in next_calls:
                try:
                    tool = executor.get_tool(call.get("name")) if executor else None
                    if tool is None or executor._blocked_runtime_install_reason(call) or recovery.progress.before_call(call, record=False):
                        bounded = False
                        break
                    validate_tool_arguments(tool, call)
                except Exception:
                    bounded = False
                    break
        if review_needed:
            # Each extension rechecks sources and real output bytes, under one
            # bounded deadline. Scripts are deliberately not delivery progress.
            try:
                async with asyncio.timeout(10):
                    if source_paths(message):
                        current_sources = await fingerprints(self._sandbox, source_paths(message),
                            approved_upload_paths=message.analysis_inputs.upload_paths if message.analysis_inputs else [])
                        sources_stable = bool(current_sources and current_sources == self._analysis_source_fingerprints)
                    if (message.attachments or message.attachment_file_ids) and not message.analysis_inputs:
                        # The current fingerprint protocol covers mounted
                        # datasets/output, not arbitrary attachment paths.
                        sources_stable = False
                    candidates = [path for path in await self._list_sandbox_artifacts()
                                  if path not in getattr(self, "_artifact_baseline_paths", set())
                                  and artifact_kind(path) not in {None, "code"}][:32]
                    valid = []
                    if candidates:
                        result = await self._sandbox.validate_artifacts([
                            {"path": path, "kind": artifact_kind(path)} for path in candidates])
                        if not result.success or not isinstance(result.data, dict) or result.data.get("version") != 1:
                            raise ValueError("artifact_review_unavailable")
                        records = result.data.get("files", [])
                        if len(records) != len(candidates) or {item["path"] for item in records} != set(candidates):
                            raise ValueError("artifact_review_incomplete")
                        valid = [item for item in records if item.get("valid") is True
                                 and type(item.get("size")) is int and item["size"] > 0
                                 and isinstance(item.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])]
                        for item in valid:
                            if len(recovery.artifact_evidence) < 128:
                                recovery.artifact_evidence.add(private_identity_hmac({"purpose": "analysis-verified-artifact/v1",
                                    "path": item["path"], "sha256": item["sha256"], "kind": item["kind"]}))
                    running_steps = [step for step in getattr(getattr(self._flow, "plan", None), "steps", [])
                                     if getattr(step.status, "value", step.status) == "running"]
                    requirements = (list(running_steps[0].deliverables) if len(running_steps) == 1
                                    else list(message.deliverables))
                    if requirements:
                        from app.domain.services.analysis_completion import assess_delivery
                        # Provisional records are used only for the required-
                        # output comparison; they are never uploaded/persisted.
                        inspected = [FileInfo(file_id=f"review-{index}", file_path=item["path"],
                            filename=PurePosixPath(item["path"]).name, size=item["size"],
                            metadata={"artifact_sha256": item["sha256"]}) for index, item in enumerate(valid)]
                        required_work = bool(assess_delivery(requirements, valid, inspected,
                            execution_success=True, stop_code="completed", validation_available=True).missing)
            except Exception:
                sources_stable = False
                bounded = False
        progress = sorted(recovery.progress.evidence | recovery.artifact_evidence)
        return BudgetEvidence(scope_digest=handle.scope_digest, confirmed_progress_units=len(progress),
            progress_digest=private_identity_hmac({"purpose": "analysis-grant-progress/v1", "evidence": progress}),
            has_unknown_execution=recovery.executions.summary()["pending_execution"],
            no_progress_loop=recovery.progress.stalled,
            next_action_bounded=bool(bounded and sources_stable and required_work), estimated_next_batches=1)

    async def _run_authorized_flow(
        self,
        message: Message,
        *,
        task_id: str | None = None,
        trigger_event_seq: int | None = None,
        runtime_stack=None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """Process a single message through the agent's flow and yield events"""
        self._requested_code_deliverables = [item for item in message.deliverables if item.kind == "code"]
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

        from app.domain.services.analysis_checkpoint import fingerprints, prepare_continuation, source_paths
        checkpoint = (await prepare_continuation(
            self._session_repository, self._sandbox, self._session_id, self._user_id, message)
            if message.resume_from else None)
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
        self._analysis_source_fingerprints = await fingerprints(self._sandbox, source_paths(message),
            approved_upload_paths=message.analysis_inputs.upload_paths if message.analysis_inputs else []) if source_paths(message) else None
        try:
            await self._open_analysis_runtime(message, trigger_event_seq, runtime_stack)
        except Exception as error:
            from app.domain.services.analysis_budget import BudgetUnavailableError
            if not isinstance(error, BudgetUnavailableError):
                raise
            logger.warning("Analysis runtime admission unavailable error_type=%s", type(error).__name__)
            yield terminal_analysis_message(
                "本次分析尚未开始：无法确认任务运行记录（runtime_admission_unavailable）。请检查运行记录后再继续。",
                reason_code="runtime_admission_unavailable", stage="input_preparation", analysis_started=False)
            yield DoneEvent()
            return
        delivery = getattr(self, "_input_delivery", None)
        identity = getattr(self, "_accepted_input_key", None)
        if delivery is not None and identity is not None:
            await delivery.mark_analysis_started(self._session_id, identity)
        self._analysis_turn_started = True
        if checkpoint:
            self._generated_files = [FileInfo.model_validate(item) for item in checkpoint.get("delivered", [])]
        self._analysis_delivery_trackers = {}
        # These survive executor context resets/compaction and are keyed by
        # the original accepted input. Durable audit also prevents restart use.
        self._analysis_scope_trackers = {}
        self._analysis_scope_observations = {}
        self._analysis_verified_files = {}
        self._analysis_preserved_answer_evidence = {}
        self._analysis_verified_receipts = {}
        self._analysis_preserved_file_ids = set()
        self._analysis_rejected_versions = {}
        from app.domain.services.analysis_answer_review import AnswerEvidence
        self._analysis_answer_evidence = AnswerEvidence()
        self._analysis_answer_evidence.observe_context({
            "datasets": [{"dataset_id": dataset.dataset_id, "name": dataset.name,
                          "description": dataset.description,
                          "sandbox_path": dataset.sandbox_path,
                          "files": [{"path": str(PurePosixPath(dataset.sandbox_path) / item.path),
                                     "size": item.size} for item in dataset.files[:64]]}
                         for dataset in message.datasets[:4]],
            "input_files": [{"path": path} for path in message.attachments[:64]],
        })
        if (checkpoint and self._is_delivery_only_continuation(checkpoint)
                and "answer_evidence" in checkpoint):
            # prepare_continuation has authenticated the complete private
            # checkpoint and rechecked original source/output bytes. Restore
            # the same execution's observations only for upload recovery;
            # arbitrary history and new analyses cannot gain this authority.
            self._analysis_answer_evidence = AnswerEvidence.from_checkpoint_snapshot(
                checkpoint["answer_evidence"],
                step_ids={step["id"] for step in checkpoint["plan"]["steps"]})
        from app.domain.services.execution_history import reviewed_history_steps
        history = getattr(message, "_session_events_snapshot", None)
        for previous_step in reviewed_history_steps(
            history, dataset_ids={item.dataset_id for item in message.datasets},
            input_file_ids=set(message.attachment_file_ids),
        ):
            self._analysis_answer_evidence.observe_reviewed_result(previous_step)
        self._flow._artifact_repair_requests = {}
        last_analysis_outcome = None
        artifact_discovery_ran = False
        delivered_file_keys: set[str] = set()
        completed_step_count = 0
        early_artifact_delivery_count = 0
        skip_next_step_results: set[str] = set()
        withheld_step_outcomes = {}
        # Completion advice only needs the current turn.  Keeping this compact
        # also avoids loading and serializing the full session at Done time.
        turn_events: List[BaseEvent] = [
            MessageEvent(role="user", message=message.message)
        ]

        flow_events = (self._resume_delivery_only(checkpoint)
                       if self._is_delivery_only_continuation(checkpoint) else self._flow.run(message))
        async for event in flow_events:
            delivery = getattr(self, "_input_delivery", None)
            identity = getattr(self, "_accepted_input_key", None)
            if delivery is not None and identity is not None:
                # Check before processing step attachments or delayed tool
                # results, not only after artifact uploads have already run.
                await delivery._require_live(self._session_id, identity)
            pre_events: List[BaseEvent] = []
            post_events: List[BaseEvent] = []
            suppress_event = False
            if isinstance(event, ToolEvent):
                self._observe_answer_tool_event(event)
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
                from app.domain.services.analysis_completion import requirements_for_step
                for item in requirements_for_step(event.step, message):
                    if item.kind == "code" and item not in self._requested_code_deliverables:
                        self._requested_code_deliverables.append(item)
                if event.status == StepStatus.STARTED:
                    self._analysis_answer_evidence.begin_step(event.step.id)
                explicit_files = await self._sync_step_attachments_to_storage(event)
                artifact_policy = str(
                    (event.step.inputs or {}).get("artifact_policy") or "optional"
                )
                force_artifact_discovery = artifact_policy in {
                    "required",
                    "capability",
                }
                if event.status in {StepStatus.COMPLETED, StepStatus.FAILED} and (
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

                if event.status in {StepStatus.COMPLETED, StepStatus.FAILED}:
                    original_step_result = (event.step.result or "").strip()
                    terminal_files = self._unique_files(explicit_files + discovered_files + self._generated_files)
                    terminal_files = await self._finalize_analysis_step(
                        event, message, terminal_files, source_seq=trigger_event_seq)
                    # Once a contract has been checked, later summary/Done
                    # branches may only reuse the validated delivered files.
                    if event.step.outcome:
                        self._generated_files = terminal_files
                    if event.step.id in self._flow._artifact_repair_requests:
                        skip_next_step_results = {original_step_result, (event.step.result or "").strip()} - {""}
                        yield MessageEvent(message="", metadata={"analysis_progress": {"stage": "completing_results"}})
                        # The matching terminal/prose pair is internal until
                        # required outputs are repaired or further work stalls.
                        continue
                    completed_step_count += 1
                    last_analysis_outcome = event.step.outcome
                    if last_analysis_outcome and last_analysis_outcome.status != "succeeded":
                        withheld_step_outcomes[event.step.id] = last_analysis_outcome
                    else:
                        withheld_step_outcomes.pop(event.step.id, None)
                    new_files = [
                        file_info
                        for file_info in terminal_files
                        if self._file_delivery_key(file_info) not in delivered_file_keys
                    ]
                    if new_files or last_analysis_outcome:
                        result_message = (event.step.result or "").strip()
                        delivery_message = result_message or f"已生成 {len(new_files)} 个结果文件。"
                        delivery_event = MessageEvent(
                            role="assistant",
                            message=delivery_message,
                            attachments=new_files,
                            metadata={
                                "artifact_delivery": True,
                                "step_id": event.step.id,
                                **({"analysis_outcome": last_analysis_outcome.model_dump(exclude_none=True)} if last_analysis_outcome else {}),
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
                            skip_next_step_results = {original_step_result, result_message} - {""}
            elif isinstance(event, MessageEvent):
                if (event.metadata or {}).get("analysis_progress"):
                    # Progress is a transient running message, not a terminal
                    # summary and not a request to discover or attach files.
                    event.attachments = []
                    event.metadata.pop("analysis_outcome", None)
                    yield event
                    turn_events.append(event)
                    continue
                is_summary = self._should_attach_generated_files_to_message()
                summary_discovered_files: List[FileInfo] = []
                normalized_message = (event.message or "").strip()
                if is_summary and last_analysis_outcome and last_analysis_outcome.status != "succeeded":
                    event.metadata = {**(event.metadata or {}), "analysis_outcome": last_analysis_outcome.model_dump(exclude_none=True)}
                if is_summary and withheld_step_outcomes:
                    # A later summarizer has no new execution evidence and must
                    # not resurrect a withheld draft, including when a later
                    # step succeeded after an earlier one failed.
                    event.message = "部分步骤尚未完成核验；已确认的分析与文件请以上方各步骤的交付结果为准。"
                elif is_summary and last_analysis_outcome is not None:
                    # No new execution evidence exists in a model-only summary.
                    # Reuse reviewed step text, never re-author scientific facts.
                    checked_steps = getattr(getattr(self._flow, "plan", None), "steps", [])
                    event.message = "\n\n".join(item.result for item in checked_steps if item.outcome and item.result)
                if (
                    skip_next_step_results
                    and not is_summary
                    and not event.attachments
                    and normalized_message in skip_next_step_results
                ):
                    suppress_event = True
                    skip_next_step_results.clear()
                elif skip_next_step_results and not is_summary:
                    skip_next_step_results.clear()

                if not suppress_event and is_summary and artifact_discovery_dirty:
                    summary_discovered_files = await self._sync_discovered_artifacts_to_storage()
                    artifact_discovery_dirty = False
                    artifact_discovery_ran = True
                if not suppress_event:
                    if last_analysis_outcome is not None:
                        # The step boundary is authoritative. A later model
                        # summary cannot introduce an unvalidated attachment.
                        requested_paths = {item.file_path for item in event.attachments or []}
                        event.attachments = [item for item in self._generated_files if item.file_path in requested_paths]
                    else:
                        await self._sync_message_attachments_to_storage(event)
                    staged_files = [
                        file_info
                        for file_info in self._unique_files(
                            list(event.attachments or []) + summary_discovered_files
                        )
                        if self._file_delivery_key(file_info) not in delivered_file_keys
                    ]
                    event.attachments = await self._validate_staged_artifact_files(staged_files)
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
                if self._flow._artifact_repair_requests:
                    # execute_step has already yielded its terminal StepEvent;
                    # confirmed, repairable file failure is not a terminal SSE
                    # error while a private repair is scheduled.
                    continue
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
                    partial_files = await self._validate_staged_artifact_files(partial_files)
                    if partial_files:
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info)
                            for file_info in partial_files
                        )
                        pre_events.append(
                            MessageEvent(
                                role="assistant",
                                message=(
                                    f"任务未能完整完成。已保存的阶段性文件：{len(partial_files)} 个。"
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
                    late_files = await self._validate_staged_artifact_files(late_files)
                    if late_files:
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info) for file_info in late_files
                        )
                        pre_events.append(
                            MessageEvent(
                                role="assistant",
                                message=f"已保存的阶段性文件：{len(late_files)} 个。",
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
                    late_files = await self._validate_staged_artifact_files(late_files)
                    if late_files:
                        delivered_file_keys.update(
                            self._file_delivery_key(file_info) for file_info in late_files
                        )
                        pre_events.append(
                            MessageEvent(
                                role="assistant",
                                message=f"已保存的阶段性文件：{len(late_files)} 个。",
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
