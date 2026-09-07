from app.core.config import get_settings
from app.domain.models.tool_runtime import ToolRuntimeConfig


def default_tool_runtime() -> ToolRuntimeConfig:
    settings = get_settings()
    return ToolRuntimeConfig(
        preset_id=settings.tool_preset_id,
        selection_mode=settings.tool_selection_mode,
        code_mode_enabled=settings.code_mode_enabled,
        domain_subagents_enabled=settings.domain_subagents_enabled,
    )


def resolve_tool_runtime(profile_config: dict | None) -> ToolRuntimeConfig:
    if profile_config:
        # Never turn trials on retroactively for a saved legacy profile.
        return ToolRuntimeConfig.model_validate(profile_config.get("tool_runtime") or {})
    return default_tool_runtime()
