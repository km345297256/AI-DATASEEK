from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from typing import Optional

from app.core.config import Settings


def create_chat_model(settings: Settings, overrides: Optional[dict] = None) -> BaseChatModel:
    """Create the configured chat model.

    DeepSeek has a first-party LangChain integration with its own environment
    variables and request cleanup, so instantiate it directly instead of routing
    through the generic provider resolver.

    overrides: optional dict with keys matching Settings fields (model_name,
               model_provider, api_key, api_base, temperature, max_tokens,
               client_max_retries)
               that take precedence over settings values.
    """
    ov = overrides or {}
    provider = (ov.get("model_provider") or settings.model_provider or "openai").lower().strip()
    kwargs = {
        "model": ov.get("model_name") or settings.model_name,
        "temperature": ov.get("temperature") if ov.get("temperature") is not None else settings.temperature,
        "max_tokens": ov.get("max_tokens") if ov.get("max_tokens") is not None else settings.max_tokens,
        "max_retries": (
            ov.get("client_max_retries")
            if ov.get("client_max_retries") is not None
            else settings.llm_client_max_retries
        ),
    }

    api_base = ov.get("api_base") or settings.api_base
    if api_base:
        kwargs["base_url"] = api_base
    if settings.extra_headers:
        kwargs["default_headers"] = settings.extra_headers

    api_key = ov.get("api_key") or settings.api_key

    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        if api_key:
            kwargs["api_key"] = api_key
        return ChatDeepSeek(**kwargs,
                            extra_body={"thinking": {"type": "disabled"}},)

    if api_key and provider in {"openai", "azure_openai", "xai", "perplexity"}:
        kwargs["api_key"] = api_key

    return init_chat_model(
        **kwargs,
        extra_body={"thinking": {"type": "disabled"}},
        model_provider=provider,
    )
