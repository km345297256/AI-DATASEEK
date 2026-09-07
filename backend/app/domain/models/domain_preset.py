"""Host-authored, declarative tool-selection presets (not permission grants)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ToolSelectionMode = Literal["all", "on_demand"]


class DomainPreset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=600)
    plugin_ids: tuple[str, ...] = Field(max_length=20)
    initial_tools: tuple[str, ...] = Field(min_length=1, max_length=8)
    instructions: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def validate_unique_references(self) -> "DomainPreset":
        for values in (self.plugin_ids, self.initial_tools):
            if len(values) != len(set(values)):
                raise ValueError("preset references must be unique")
        if self.id != "general" and not self.plugin_ids:
            raise ValueError("domain presets must declare their plugin scope")
        return self
