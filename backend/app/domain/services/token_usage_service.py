import logging
from typing import Any, Optional

from langchain.messages import AIMessage

from app.domain.models.usage import TokenUsageRecord
from app.infrastructure.models.documents import TokenUsageDocument

logger = logging.getLogger(__name__)


class TokenUsageService:
    async def record_from_message(
        self,
        message: AIMessage,
        *,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        model_provider: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> Optional[TokenUsageRecord]:
        usage = self.extract_usage(message)
        if not usage:
            return None

        record = TokenUsageRecord(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            task_id=task_id,
            model_provider=model_provider,
            model_name=model_name,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
        )
        try:
            doc = TokenUsageDocument.from_domain(record)
            await doc.insert()
            return doc.to_domain()
        except Exception as exc:
            logger.warning("Failed to record token usage: %s", exc)
            return None

    def extract_usage(self, message: AIMessage) -> Optional[dict[str, int]]:
        usage_metadata = getattr(message, "usage_metadata", None) or {}
        response_metadata = getattr(message, "response_metadata", None) or {}
        token_usage = response_metadata.get("token_usage") if isinstance(response_metadata, dict) else None
        usage = usage_metadata or token_usage or {}
        if not isinstance(usage, dict):
            return None

        prompt_tokens = self._first_int(usage, ["input_tokens", "prompt_tokens"])
        completion_tokens = self._first_int(usage, ["output_tokens", "completion_tokens"])
        total_tokens = self._first_int(usage, ["total_tokens"])
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens
        if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
            return None
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _first_int(self, data: dict[str, Any], keys: list[str]) -> int:
        for key in keys:
            value = data.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
        return 0


def get_token_usage_service() -> TokenUsageService:
    return TokenUsageService()
