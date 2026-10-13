"""Version 1 of the Data Skill package contract (not an execution grant)."""

import json
import re
from typing import Any, Literal

from jsonschema import Draft7Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator


SEMVER = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
PACKAGE_ID = r"[a-z][a-z0-9-]{0,63}/[a-z][a-z0-9-]{0,63}"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SkillToolDependency(ContractModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
    # Exact plugin version. Resolution against the execution catalog is a
    # separate admission step; declaring a dependency does not install it.
    version: str

    @field_validator("version")
    @classmethod
    def exact_version(cls, value: str) -> str:
        if not re.fullmatch(SEMVER, value, flags=re.ASCII):
            raise ValueError("Tool dependency requires an exact SemVer version")
        return value


class SkillResourceRequirements(ContractModel):
    network: bool = False
    max_download_bytes: int | None = Field(default=None, strict=True, gt=0)
    memory_mb: int | None = Field(default=None, strict=True, gt=0)
    timeout_seconds: int | None = Field(default=None, strict=True, gt=0)


class SkillManifest(ContractModel):
    schema_version: Literal[1]
    package_id: str = Field(pattern=f"^{PACKAGE_ID}$")
    version: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    authors: list[str] = Field(min_length=1, max_length=20)
    license: str = Field(min_length=1, max_length=200)
    category: Literal["discovery", "acquisition", "quality", "transformation", "analysis", "reporting"]
    domains: list[str] = Field(default_factory=list, max_length=30)
    triggers: list[str] = Field(default_factory=list, max_length=50)
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    prerequisites: list[str] = Field(default_factory=list, max_length=50)
    semantic_constraints: list[str] = Field(default_factory=list, max_length=50)
    tools: list[SkillToolDependency] = Field(default_factory=list, max_length=50)
    resources: SkillResourceRequirements = Field(default_factory=SkillResourceRequirements)
    validation: list[str] = Field(min_length=1, max_length=50)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported skill schema version")
        return value

    @field_validator("version")
    @classmethod
    def release_version(cls, value: str) -> str:
        return SkillToolDependency.exact_version(value)

    @field_validator("authors", "domains", "triggers", "prerequisites", "semantic_constraints", "validation")
    @classmethod
    def nonempty_items(cls, items: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 2000 for item in items):
            raise ValueError("Skill metadata entries must be nonempty and bounded")
        return items

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, tools: list[SkillToolDependency]) -> list[SkillToolDependency]:
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("Duplicate tool dependency")
        return tools

    @field_validator("inputs", "outputs")
    @classmethod
    def object_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(schema, allow_nan=False)
            if len(encoded) > 64_000 or schema.get("type") != "object":
                raise ValueError("Skill input/output must be a bounded object schema")
            if schema.get("$schema") not in (None, "http://json-schema.org/draft-07/schema#"):
                raise ValueError("Skill schemas must use JSON Schema draft-07")
            Draft7Validator.check_schema(schema)
            pending = [schema]
            # V1 deliberately inlines definitions. Remote and recursive refs
            # are unsupported rather than implicitly fetching external schemas.
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    if "$ref" in value or "$id" in value:
                        raise ValueError("Skill schemas must inline definitions in version 1")
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
        except (SchemaError, TypeError, RecursionError) as exc:
            raise ValueError("Invalid skill input/output schema") from exc
        return schema

    @property
    def selection_name(self) -> str:
        # No slash: compatible with existing /skills/{name} routes. Namespace
        # and exact version prevent silently selecting a newer release.
        return f"{self.package_id.replace('/', ':')}@{self.version}"
