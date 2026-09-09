import io
import logging
from typing import Any

from app.application.services.dataset_request_resolver import FrontControllerResolution
from app.domain.external.task import Task, TaskRunner
from app.domain.external.file import FileStorage
from app.domain.models.file import FileInfo
from app.domain.models.execution_environment import ExecutionEnvironmentSnapshot
from app.domain.models.audit import AuditRiskLevel, AuditStatus
from app.domain.models.event import AgentEvent, DoneEvent, ErrorEvent, MessageEvent
from app.domain.models.session import SessionStatus
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.audit_service import AuditService
from app.domain.services.completion_advice_service import get_completion_advice_service
from app.domain.services.tools.pipeline import opaque_log_identifier
from app.domain.services.execution_environment import (
    create_lightweight_execution_snapshot,
)
from app.domain.utils.public_error import public_error_message
from pydantic import TypeAdapter

logger = logging.getLogger(__name__)


class LightweightTaskRunner(TaskRunner):
    """Persist and stream a model-resolved answer without allocating a sandbox."""

    def __init__(
        self,
        *,
        session_id: str,
        user_id: str,
        resolution: FrontControllerResolution,
        session_repository: SessionRepository,
        file_storage: FileStorage,
        llm_overrides: dict[str, Any] | None = None,
        input_delivery=None,
    ):
        self._session_id = session_id
        self._user_id = user_id
        self._resolution = resolution
        self._session_repository = session_repository
        self._input_delivery = input_delivery
        self._accepted_input_key: str | None = None
        self._accepted_input_finished = False
        self._file_storage = file_storage
        self._llm_overrides = dict(llm_overrides or {})
        self._execution_snapshot: ExecutionEnvironmentSnapshot | None = None
        self._audit_service = AuditService()
        self._completion_advice = get_completion_advice_service()

    async def _record_execution_snapshot(
        self,
        task_id: str,
        trigger_event_seq: int | None = None,
    ) -> ExecutionEnvironmentSnapshot:
        snapshot = create_lightweight_execution_snapshot(
            task_id=task_id,
            session_id=self._session_id,
            resolution=self._resolution,
            llm_overrides=getattr(self, "_llm_overrides", None),
            trigger_event_seq=trigger_event_seq,
        )
        existing = getattr(self, "_execution_snapshot", None)
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
                    "Failed to persist lightweight execution snapshot session=%s task=%s error_type=%s",
                    opaque_log_identifier(self._session_id, namespace="session"),
                    opaque_log_identifier(task_id, namespace="task"),
                    type(exc).__name__,
                )
                raise RuntimeError(
                    "The task execution environment could not be recorded"
                ) from exc
        else:
            logger.warning(
                "Session repository has no execution snapshot store session=%s",
                opaque_log_identifier(self._session_id, namespace="session"),
            )
        self._execution_snapshot = snapshot
        return snapshot

    def _execution_task_id(self, task: Task) -> str:
        """Read the required production task id without trusting test doubles."""
        task_id = getattr(task, "id", None)
        if isinstance(task_id, str) and task_id.strip():
            return task_id
        if callable(getattr(self._session_repository, "add_execution_snapshot", None)):
            raise RuntimeError("Persisted lightweight task is missing its task id")
        # A few isolated legacy tests use a deliberately incomplete Task fake
        # and repository. They cannot persist a snapshot; keep them compatible
        # without allowing a production repository to bypass provenance.
        logger.warning(
            "Legacy untracked lightweight task session=%s",
            opaque_log_identifier(self._session_id, namespace="session"),
        )
        return "legacy-untracked-task"

    async def _publish(self, task: Task, event: AgentEvent) -> None:
        delivery = getattr(self, "_input_delivery", None)
        identity = getattr(self, "_accepted_input_key", None)
        if delivery is not None and identity is not None:
            if getattr(self, "_accepted_input_finished", False):
                from app.domain.services.input_delivery import InputLeaseLost
                raise InputLeaseLost()
            await delivery.prepare_event(self._session_id, identity, event)
        event.bind_producer_event_id()
        reserve_sequence = getattr(
            self._session_repository,
            "reserve_event_sequence",
            None,
        )
        if callable(reserve_sequence):
            await reserve_sequence(self._session_id, event)
        await self._session_repository.add_event(self._session_id, event)
        try:
            event_id = await task.output_stream.put(event.model_dump_json())
            event.id = event_id
            record_alias = getattr(self._session_repository, "record_event_transport_alias", None)
            if callable(record_alias):
                await record_alias(self._session_id, event)
        except Exception as error:
            logger.warning("Durable event live publication unavailable error_type=%s", type(error).__name__)
        if delivery is not None and identity is not None:
            await delivery.complete(self._session_id, identity, event)
            if isinstance(event, (DoneEvent, ErrorEvent)):
                self._accepted_input_finished = True

    async def run(self, task: Task) -> None:
        try:
            pop_input_or_close = getattr(task, "pop_input_or_close", None)
            event_id, event_str = (
                await pop_input_or_close()
                if callable(pop_input_or_close)
                else await task.input_stream.pop()
            )
            if event_str is None:
                return
            user_event = TypeAdapter(AgentEvent).validate_json(event_str)
            if not isinstance(user_event, MessageEvent):
                raise RuntimeError("Lightweight task requires a user message")
            delivery = getattr(self, "_input_delivery", None)
            if delivery is not None and await delivery.start(self._session_id, user_event, task):
                from app.domain.models.input_admission import input_key
                self._accepted_input_key = input_key(user_event)
            await self._record_execution_snapshot(
                self._execution_task_id(task),
                trigger_event_seq=user_event.seq,
            )
            review = self._resolution.decision.safety
            await self._record_safety_audit(review)
            if not review.allowed:
                technical_failure = any(
                    category in {"front_controller_unavailable", "front_controller_decision_missing"}
                    for category in review.categories
                )
                answer = (
                    "请求未执行。\n\n"
                    f"判定原因：{review.reason or '请求命中了系统安全策略。'}\n\n"
                    f"修改建议：{review.suggestion or '请移除可能违规或越权的内容后重试。'}"
                )
                metadata = {
                    "front_controller_error" if technical_failure else "safety_review": review.model_dump(),
                    "front_controller": self._resolution.controller_metadata,
                }
            else:
                answer = self._resolution.answer
                metadata = {
                    "execution_mode": "lightweight",
                    "front_controller": self._resolution.controller_metadata,
                }
            attachments = await self._upload_catalog_artifacts()
            assistant_event = MessageEvent(
                role="assistant",
                message=answer,
                metadata=metadata,
                attachments=attachments or None,
            )
            await self._publish(task, assistant_event)
            await self._session_repository.update_latest_message(self._session_id, answer, assistant_event.timestamp)
            await self._session_repository.increment_unread_message_count(self._session_id)
            advice = self._completion_advice.default_advice()
            await self._publish(task, DoneEvent(advice=self._completion_advice.to_payload(advice)))
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except Exception as exc:
            logger.error(
                "Lightweight task failed session=%s error_type=%s",
                opaque_log_identifier(self._session_id, namespace="session"),
                type(exc).__name__,
            )
            await self._publish(task, ErrorEvent(error=public_error_message(exc)))
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)

    async def _upload_catalog_artifacts(self) -> list[FileInfo]:
        artifacts = getattr(self._resolution, "artifacts", [])
        if not artifacts:
            return []
        uploaded = []
        for artifact in artifacts:
            content = artifact.content.encode("utf-8")
            file_info = await self._file_storage.upload_file(
                io.BytesIO(content),
                artifact.filename,
                self._user_id,
                content_type=artifact.content_type,
                metadata={
                    "session_id": self._session_id,
                    "source": "catalog_artifact",
                    "artifact_size": len(content),
                },
            )
            await self._session_repository.add_file(self._session_id, file_info)
            uploaded.append(file_info)
        return uploaded

    async def _record_safety_audit(self, review) -> None:
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
                    "front_controller": self._resolution.controller_metadata,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist lightweight safety audit session=%s error_type=%s",
                opaque_log_identifier(self._session_id, namespace="session"),
                type(exc).__name__,
            )

    async def on_done(self, task: Task) -> None:
        return None

    async def destroy(self) -> None:
        return None
