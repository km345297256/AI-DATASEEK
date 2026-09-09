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
            if file_path in skipped or file_path in unavailable:
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
                    self._accepted_input_key = None
                    self._accepted_input_finished = False
                    delivery = getattr(self, "_input_delivery", None)
                    if delivery is not None and await delivery.start(self._session_id, event, task):
                        from app.domain.models.input_admission import input_key
                        self._accepted_input_key = input_key(event)
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
                decision = getattr(getattr(self, "_front_controller_resolution", None), "decision", None)
                execution_decision = getattr(decision, "execution", None)
                message_obj = Message(
                    message=message,
                    resume_from=metadata.get("resume_from"),
                    client_message_id=metadata.get("client_message_id"),
                    deliverables=list(getattr(execution_decision, "deliverables", []) or []),
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
                await self._put_and_add_event(task, MessageEvent(
                    message=("本次分析已达到执行时间上限，已有结果保持不变。"
                             if getattr(error, "code", None) == "analysis_budget_deadline_exceeded" or model_stop_reason() == "analysis_budget_deadline_exceeded"
                             else "本轮执行已达到模型运行边界，或运行记录暂时无法保存。已有分析结果会保留，请检查模型运行记录后继续。"),
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
    
    async def _review_artifact_repair(self, step, message, records, checked_files, requirements,
                                      outcome, execution, *, validation_available, source_seq):
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
            for item in checked_files:
                pins.setdefault(item.file_path, item)

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
            feedback = dict(decision.feedback)
            feedback["previous_analysis"] = (step.result or "")[:32000]
            self._flow._artifact_repair_requests[step.id] = feedback
        logger.info("analysis_artifact_repair allowed=%s reason=%s files=%d missing=%d",
                    allowed, repair_reason, len(records), len(outcome.missing))
        return allowed, protected_changed

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
        requirements = requirements_for_step(step, message)
        step.deliverables = requirements
        execution = step.outputs.get("execution_outcome", {})
        unconfirmed = bool(execution.get("has_unconfirmed_tool_execution") or execution.get("side_effect_state") == "unknown")
        candidates = set(step.attachments or []) | {item.file_path for item in files if item.file_path}
        candidates.update(getattr(self, "_pending_artifact_paths", set()))
        pinned = getattr(self, "_analysis_verified_files", {}).get(step.id, {})
        candidates.update(pinned)
        if not requirements and not candidates and step.success and not unconfirmed:
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
                 if expected_kind(path) and path.startswith("/home/ubuntu/output/")
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
        repair_pending, protected_changed = await self._review_artifact_repair(
            step, message, records, checked_files, requirements, outcome, execution,
            validation_available=available, source_seq=source_seq)
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
        step.outcome = outcome
        step.success = outcome.status == "succeeded"
        step.status = ExecutionStatus.COMPLETED if step.success else ExecutionStatus.FAILED
        event.status = StepStatus.COMPLETED if step.success else StepStatus.FAILED
        if not step.success:
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
            if safe and not repair_pending and not protected_changed and message.datasets and source_seq is not None:
                try:
                    token = await save_checkpoint(
                        self._session_repository, self._sandbox, self._session_id, self._user_id,
                        message, getattr(self._flow, "plan", None),
                        source_fingerprints=getattr(self, "_analysis_source_fingerprints", None),
                        records=records, delivered=checked_files, reason_code=outcome.reason_code,
                        source_seq=source_seq)
                    if token:
                        outcome.can_resume, outcome.resume_from = True, token
                except Exception as error:
                    logger.warning("Analysis checkpoint unavailable error_type=%s", type(error).__name__)
            if not step.result:
                step.result = outcome_message(outcome, delivered_files=checked_files)
            elif not repair_pending and not step.result.startswith("分析说明（完成状态以成果检查结果为准）："):
                # Preserve evidence/explanation, but distinguish model-authored
                # prose from the authoritative verified completion state.
                step.result = "分析说明（完成状态以成果检查结果为准）：\n\n" + step.result
            step.error = outcome.reason_code
        logger.info("analysis_outcome status=%s reason=%s missing=%d delivered=%d resumable=%s",
                    outcome.status, outcome.reason_code, len(outcome.missing), len(checked_files), outcome.can_resume)
        if public_step is not step:
            # Public StepEvents can be redacted copies. Reflect execution state
            # in both places without writing redacted source inputs into the plan.
            for field in ("status", "success", "result", "error", "outcome", "deliverables", "outputs"):
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
                    if message.datasets:
                        current_sources = await fingerprints(self._sandbox, source_paths(message))
                        sources_stable = bool(current_sources and current_sources == self._analysis_source_fingerprints)
                    if message.attachments or message.attachment_file_ids:
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
        self._analysis_source_fingerprints = await fingerprints(self._sandbox, source_paths(message)) if message.datasets else None
        try:
            await self._open_analysis_runtime(message, trigger_event_seq, runtime_stack)
        except Exception as error:
            from app.domain.services.analysis_budget import BudgetUnavailableError
            if not isinstance(error, BudgetUnavailableError):
                raise
            logger.warning("Analysis runtime admission unavailable error_type=%s", type(error).__name__)
            yield MessageEvent(message="本次执行暂未启动：无法确认任务运行记录。已有结果保持不变，请检查任务运行记录后再继续。")
            yield DoneEvent()
            return
        if checkpoint:
            self._generated_files = [FileInfo.model_validate(item) for item in checkpoint.get("delivered", [])]
        self._analysis_delivery_trackers = {}
        self._analysis_verified_files = {}
        self._flow._artifact_repair_requests = {}
        last_analysis_outcome = None
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
                    terminal_files = self._unique_files(explicit_files + discovered_files + self._generated_files)
                    terminal_files = await self._finalize_analysis_step(
                        event, message, terminal_files, source_seq=trigger_event_seq)
                    # Once a contract has been checked, later summary/Done
                    # branches may only reuse the validated delivered files.
                    if event.step.outcome:
                        self._generated_files = terminal_files
                    if event.step.id in self._flow._artifact_repair_requests:
                        skip_next_step_result = (event.step.result or "").strip() or None
                        yield MessageEvent(message="", metadata={"analysis_progress": {"stage": "completing_results"}})
                        # The matching terminal/prose pair is internal until
                        # required outputs are repaired or further work stalls.
                        continue
                    completed_step_count += 1
                    last_analysis_outcome = event.step.outcome
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
                            skip_next_step_result = result_message
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
