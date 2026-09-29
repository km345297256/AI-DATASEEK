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
            **self.extract_cache_usage(message),
        )
        try:
            doc = TokenUsageDocument.from_domain(record)
            await doc.insert()
            return doc.to_domain()
        except Exception as exc:
            logger.warning(
                "Failed to record token usage error_type=%s",
                type(exc).__name__,
            )
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

    def extract_cache_usage(self, message: AIMessage) -> dict[str, int | None]:
        """Read explicit cache counters without changing legacy usage totals.

        LangChain cache_read/cache_creation, OpenAI cached_tokens and DeepSeek
        prompt_cache_hit_tokens are aliases, not separate quantities to add.
        DeepSeek prompt_cache_miss_tokens is NOT a cache-write measurement.
        Standard LangChain details may omit DeepSeek extensions; inspect the
        wire usage too, but reject conflicting or malformed reported aliases.
        Only fixed field paths and strict non-negative integers survive. Raw
        metadata, service-tier guesses and cache-hit inference are not stored.
        """
        response = getattr(message, "response_metadata", None)
        response = response if isinstance(response, dict) else {}
        sources = [getattr(message, "usage_metadata", None),
                   response.get("token_usage"), response.get("usage")]
        read_paths = (("input_token_details", "cache_read"),
                      ("prompt_tokens_details", "cached_tokens"),
                      ("input_tokens_details", "cached_tokens"),
                      ("prompt_cache_hit_tokens",), ("cache_read_input_tokens",))
        write_paths = (("input_token_details", "cache_creation"),
                       ("cache_creation_input_tokens",))

        def counter(paths: tuple[tuple[str, ...], ...]) -> int | None:
            observed: set[int] = set()
            for source in sources:
                if not isinstance(source, dict):
                    continue
                for path in paths:
                    value = source
                    for key in path:
                        if value is None:
                            break  # explicitly unreported detail
                        if not isinstance(value, dict):
                            return None
                        value = value.get(key)
                    if value is None:
                        continue
                    if type(value) is not int or value < 0:
                        return None
                    observed.add(value)
            return next(iter(observed)) if len(observed) == 1 else None

        return {"cache_read_tokens": counter(read_paths),
                "cache_write_tokens": counter(write_paths)}

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
