"""Validated, credential-free configuration for model-facing tool selection."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preset_id: str = Field(default="general", max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    # Missing fields on existing persisted profiles retain the legacy catalog.
    selection_mode: Literal["all", "on_demand"] = "all"
    code_mode_enabled: bool = False
    domain_subagents_enabled: bool = False

    @field_validator("preset_id")
    @classmethod
    def known_preset(cls, value: str) -> str:
        from app.domain.services.domain_presets import get_domain_preset

        get_domain_preset(value)
        return value
