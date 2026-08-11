import logging
from typing import Any

from app.application.services.dataset_request_resolver import FrontControllerResolution
from app.domain.external.task import Task, TaskRunner
from app.domain.models.audit import AuditRiskLevel, AuditStatus
from app.domain.models.event import AgentEvent, DoneEvent, ErrorEvent, MessageEvent
from app.domain.models.session import SessionStatus
from app.domain.repositories.session_repository import SessionRepository
from app.domain.services.audit_service import AuditService
from app.domain.services.completion_advice_service import get_completion_advice_service
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
    ):
        self._session_id = session_id
        self._user_id = user_id
        self._resolution = resolution
        self._session_repository = session_repository
        self._audit_service = AuditService()
        self._completion_advice = get_completion_advice_service()

    async def _publish(self, task: Task, event: AgentEvent) -> None:
        event_id = await task.output_stream.put(event.model_dump_json())
        event.id = event_id
        await self._session_repository.add_event(self._session_id, event)

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
            assistant_event = MessageEvent(role="assistant", message=answer, metadata=metadata)
            await self._publish(task, assistant_event)
            await self._session_repository.update_latest_message(self._session_id, answer, assistant_event.timestamp)
            await self._session_repository.increment_unread_message_count(self._session_id)
            advice = self._completion_advice.default_advice()
            await self._publish(task, DoneEvent(advice=self._completion_advice.to_payload(advice)))
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)
        except Exception as exc:
            logger.exception("Lightweight task failed for session %s", self._session_id)
            await self._publish(task, ErrorEvent(error=public_error_message(exc)))
            await self._session_repository.update_status(self._session_id, SessionStatus.COMPLETED)

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
            logger.warning("Failed to persist lightweight safety audit: %s", exc)

    async def on_done(self, task: Task) -> None:
        return None

    async def destroy(self) -> None:
        return None
