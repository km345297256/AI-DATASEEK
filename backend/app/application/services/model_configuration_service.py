from typing import Any, Dict

from app.infrastructure.models.documents import ModelConfigurationDocument


def legacy_model_settings(source: Any) -> Dict[str, Any]:
    if isinstance(source, dict):
        nested = source.get("model_config") or source.get("model_settings")
        if isinstance(nested, dict):
            return {key: value for key, value in nested.items() if value is not None}
        return {
            key: source.get(key)
            for key in ("model_provider", "model_name", "api_base", "api_key", "temperature", "max_tokens")
            if source.get(key) is not None
        }
    nested = getattr(source, "model_settings", None)
    if isinstance(nested, dict) and nested:
        return {key: value for key, value in nested.items() if value is not None}
    return {
        key: getattr(source, key)
        for key in ("model_provider", "model_name", "api_base", "api_key", "temperature", "max_tokens")
        if getattr(source, key, None) is not None
    }


async def resolve_model_settings(model_config_id: str | None, fallback: Any) -> Dict[str, Any]:
    if model_config_id:
        doc = await ModelConfigurationDocument.find_one(
            ModelConfigurationDocument.model_config_id == model_config_id
        )
        if doc and doc.enabled:
            return doc.to_domain().runtime_settings()
    return legacy_model_settings(fallback)


async def resolve_agent_profile(profile: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    profile_data = profile.model_dump()
    profile_settings = await resolve_model_settings(profile.model_config_id, profile)

    resolved_subagents: list[Dict[str, Any]] = []
    for subagent in profile.subagents:
        data = subagent.model_dump()
        data["model_config"] = await resolve_model_settings(subagent.model_config_id, subagent)
        data.pop("model_settings", None)
        resolved_subagents.append(data)
    profile_data["subagents"] = resolved_subagents
    return profile_settings, profile_data
