"""Unified, data-only visualization capability contract; no executable entry points."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from app.domain.models.visualization_adapters_generated import (
    ADAPTER_CONTRACTS,
    VISUALIZATION_CONTRACT_VERSION,
    VisualizationAdapter,
    VisualizationInputMode,
    VisualizationKind,
    VisualizationOperation,
    VisualizationReader,
)


class VisualizationCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operations: list[VisualizationOperation] = Field(min_length=1, max_length=5)
    input_mode: VisualizationInputMode
    shared: StrictBool

    @field_validator("operations")
    @classmethod
    def unique_operations(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate visualization operations")
        return values


class VisualizationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    max_input_bytes: StrictInt = Field(ge=1, le=512 * 1024 * 1024)
    max_output_bytes: StrictInt = Field(ge=1, le=16 * 1024 * 1024)


class VisualizationPlugin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    contract_version: Literal[2]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=1000)
    extensions: list[str] = Field(max_length=128)
    filenames: list[str] = Field(max_length=128)
    view_kind: VisualizationKind
    adapter: VisualizationAdapter
    reader: VisualizationReader
    capabilities: VisualizationCapabilities
    default_enabled: StrictBool
    priority: StrictInt = Field(ge=-1000, le=1000)
    permissions: list[Literal["file:read"]] = Field(min_length=1, max_length=1)
    limits: VisualizationLimits

    @field_validator("contract_version", mode="before")
    @classmethod
    def strict_contract_version(cls, value):
        if type(value) is not int or value != VISUALIZATION_CONTRACT_VERSION:
            raise ValueError("Unsupported visualization contract")
        return value

    @model_validator(mode="after")
    def validate_contract(self):
        import re
        if not self.name.strip() or not (self.extensions or self.filenames):
            raise ValueError("Visualization name and file matchers are required")
        for items, pattern in (
            (self.extensions, r"[a-z0-9][a-z0-9.-]{0,31}"),
            (self.filenames, r"(?:[a-z0-9][a-z0-9._-]{0,127}|\.zattrs)"),
        ):
            if len(set(items)) != len(items) or any(not re.fullmatch(pattern, item) for item in items):
                raise ValueError("Invalid visualization file matcher")
        spec = ADAPTER_CONTRACTS[self.adapter]
        if self.reader not in spec["readers"] or self.view_kind != spec["view_kind"]:
            raise ValueError("Visualization adapter and reader contract mismatch")
        capabilities = spec["capabilities"]
        if (set(self.capabilities.operations) != set(capabilities["operations"])
                or self.capabilities.input_mode != capabilities["input_mode"]
                or self.capabilities.shared != capabilities["shared"]):
            raise ValueError("Visualization adapter and capabilities contract mismatch")
        return self

    def matches_filename(self, filename: str) -> bool:
        """Only use an authorized file's display name, never a user-provided path."""
        name = filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return name in self.filenames or any(name.endswith("." + extension) for extension in self.extensions)


class VisualizationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    engine: Literal["cordis"]
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    plugins: list[VisualizationPlugin] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [plugin.id for plugin in self.plugins]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate visualization IDs")
        return self


class VisualizationPluginState(VisualizationPlugin):
    enabled: StrictBool


class VisualizationCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    engine: Literal["cordis"] = "cordis"
    revision: str
    plugins: list[VisualizationPluginState]


class VisualizationStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: StrictBool
