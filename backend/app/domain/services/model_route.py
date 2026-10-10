"""Resolve credential inheritance without sending one route's secrets elsewhere."""
from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.core.config import Settings


class ModelRouteConfigurationError(ValueError):
    """Safe configuration diagnostic; never includes endpoints or credentials."""


_KEY_PROVIDERS = frozenset({"openai", "azure_openai", "xai", "perplexity", "deepseek", "anthropic"})
_DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "ollama": "http://localhost:11434",
}


def _endpoint_identity(value: str | None, provider: str) -> tuple | None:
    value = value or _DEFAULT_ENDPOINTS.get(provider)
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "\\" in value
                or any(character.isspace() for character in value)):
            raise ValueError
        # Keep paths in the identity: another tenant/API under the same origin
        # must not acquire this endpoint's credentials through implicit fallback.
        port = parsed.port
        if port == 0:
            raise ValueError
        return (parsed.scheme, parsed.hostname.lower(),
                port if port is not None else (443 if parsed.scheme == "https" else 80),
                parsed.path.rstrip("/"))
    except (TypeError, ValueError):
        raise ModelRouteConfigurationError("Model endpoint is invalid; check its configuration.") from None


def _loopback(identity: tuple | None) -> bool:
    if identity is None:
        return False
    host = identity[1]
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def merge_model_route_overrides(settings: Settings, base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict:
    """Merge one trusted config layer without moving its parent's credentials.

    A child selecting another provider must not inherit the parent's endpoint;
    a child selecting another endpoint must supply its own key. Missing values
    preserve legacy inheritance, while an explicit empty key remains meaningful.
    """
    updates = {key: value for key, value in updates.items() if value is not None}
    merged = {**base, **updates}
    default_provider = (settings.model_provider or "openai").lower().strip()
    previous_provider = (base.get("model_provider") or default_provider).lower().strip()
    provider = (updates.get("model_provider") or previous_provider).lower().strip()
    previous_base = base.get("api_base") or (settings.api_base if previous_provider == default_provider else None)
    if provider != previous_provider and "api_base" not in updates:
        merged.pop("api_base", None)
    current_base = merged.get("api_base") or (settings.api_base if provider == default_provider else None)
    changed = provider != previous_provider or _endpoint_identity(previous_base, previous_provider) != _endpoint_identity(current_base, provider)
    if changed:
        for field_name in ("api_key", "extra_headers"):
            if field_name not in updates:
                merged.pop(field_name, None)
    return merged


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    api_base: str | None = field(repr=False)
    api_key: str | None = field(repr=False)
    extra_headers: dict[str, Any] | None = field(repr=False)


def resolve_model_route(settings: Settings, overrides: Mapping[str, Any] | None = None) -> ModelRoute:
    overrides = overrides or {}
    default_provider = (settings.model_provider or "openai").lower().strip()
    provider = (overrides.get("model_provider") or default_provider).lower().strip()
    explicit_base = overrides.get("api_base")
    if explicit_base is not None and (not isinstance(explicit_base, str) or not explicit_base.strip()):
        raise ModelRouteConfigurationError("Model endpoint is invalid; check its configuration.")
    api_base = explicit_base if explicit_base is not None else settings.api_base if provider == default_provider else None
    target = _endpoint_identity(api_base, provider)
    default = _endpoint_identity(settings.api_base, default_provider)
    same_route = provider == default_provider and target == default

    explicit_key = "api_key" in overrides and overrides["api_key"] is not None
    key = overrides["api_key"] if explicit_key else settings.api_key if same_route else None
    if key is not None and not isinstance(key, str):
        raise ModelRouteConfigurationError("Model credentials are invalid; check the selected configuration.")
    if explicit_key and not key.strip():
        # An intentional empty credential is allowed only for a loopback model.
        # Pass the empty string to the SDK so its ambient API key cannot win.
        if not _loopback(target):
            raise ModelRouteConfigurationError("An empty model credential is allowed only for a local loopback endpoint.")
        key = ""
    elif provider in _KEY_PROVIDERS and not same_route and not (key and key.strip()):
        raise ModelRouteConfigurationError("The selected model route requires its own credentials; default credentials were not reused.")

    # Even innocently named deployment headers can contain credentials. No
    # global headers cross a route boundary or an explicit no-auth selection.
    headers = dict(settings.extra_headers) if same_route and settings.extra_headers and not (explicit_key and key == "") else None
    return ModelRoute(provider, api_base, key, headers)
