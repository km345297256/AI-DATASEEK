from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models.event import MAX_EVENT_SEQUENCE


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,255}$")
_SAFE_RUNTIME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SECRET_LIKE_IDENTIFIER = re.compile(
    r"(?:^|[-_.:/])(?:api[-_]?key|authorization|bearer|password|secret|token)(?:$|[-_.:/])|^sk-",
    re.IGNORECASE,
)


def stable_sha256(value: object) -> str:
    """Hash a JSON-compatible, already-sanitized value deterministically."""
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_public_identifier(value: object) -> str:
    """Return a bounded non-secret identifier or a fixed redaction marker.

    Provider and model identifiers are useful for replay, but these values can
    ultimately originate in an administrator-created profile. Reject URL user
    information, absolute paths, control characters and arbitrary free text
    rather than persisting a hash derived from potentially secret input.
    """
    text = str(value or "").strip()
    if (
        not text
        or text.startswith(("/", "~", ".", "file:"))
        or re.match(r"^[A-Za-z]:/", text)
        or "://" in text
        or "@" in text
        or _SECRET_LIKE_IDENTIFIER.search(text)
        or not _SAFE_IDENTIFIER.fullmatch(text)
    ):
        return "redacted"
    return text


class CordisCatalogIdentity(BaseModel):
    """The exact immutable plugin generation used by one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ready", "unavailable", "legacy"]
    engine: Literal["cordis", "legacy-filesystem"]
    version: str = Field(min_length=1, max_length=64)
    revision: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    manifest_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    execution_bundle_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    plugin_count: int = Field(default=0, ge=0)
    tool_count: int = Field(default=0, ge=0)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if safe_public_identifier(value) != value:
            raise ValueError("catalog version is not safe to persist")
        return value

    @model_validator(mode="after")
    def validate_generation(self) -> "CordisCatalogIdentity":
        if self.status == "ready":
            if self.engine != "cordis" or not all((
                self.revision,
                self.manifest_digest,
                self.execution_bundle_digest,
            )):
                raise ValueError("ready Cordis identity requires all generation digests")
        elif any((self.revision, self.manifest_digest, self.execution_bundle_digest)):
            raise ValueError("non-ready catalog identity cannot advertise generation digests")
        if self.status == "legacy" and self.engine != "legacy-filesystem":
            raise ValueError("legacy catalog identity must use the legacy engine")
        if self.status == "unavailable" and self.engine != "cordis":
            raise ValueError("unavailable catalog identity must use the Cordis engine")
        return self


class SandboxExecutionIdentity(BaseModel):
    """Safe identity for the isolated runtime, never its host connection data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime: str = Field(min_length=1, max_length=64)
    image_reference: str | None = Field(default=None, max_length=256)
    image_digest: str | None = Field(
        default=None,
        pattern=r"^(?:sha256:)?[0-9a-f]{64}$",
    )
    dataset_mount_count: int = Field(default=0, ge=0)
    dataset_mounts_digest: str = Field(pattern=_SHA256_PATTERN)
    configuration_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: str) -> str:
        if not _SAFE_RUNTIME.fullmatch(value):
            raise ValueError("sandbox runtime must be a safe symbolic identifier")
        return value

    @field_validator("image_reference")
    @classmethod
    def validate_image_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if safe_public_identifier(value) != value:
            raise ValueError("sandbox image reference is not safe to persist")
        return value


class ModelExecutionIdentity(BaseModel):
    """Provider/model labels plus non-reversible prompt/configuration hashes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    prompt_version: str | None = Field(default=None, max_length=64)
    prompt_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    # HMAC rather than a plain hash: custom prompts can be private and have low
    # entropy. The original prompt never crosses the snapshot boundary.
    custom_prompt_hmac_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
        exclude_if=lambda value: value is None,
    )
    configuration_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("provider", "model")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if value != "redacted" and safe_public_identifier(value) != value:
            raise ValueError("model identity is not safe to persist")
        return value

    @field_validator("prompt_version")
    @classmethod
    def validate_prompt_version(cls, value: str | None) -> str | None:
        if value is not None and safe_public_identifier(value) != value:
            raise ValueError("prompt version is not safe to persist")
        return value


class ToolSelectionExecutionIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    preset_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    selection_mode: Literal["all", "on_demand"]
    initial_tool_count: int = Field(ge=0)
    available_tool_count: int = Field(ge=0)
    selection_digest: str = Field(pattern=_SHA256_PATTERN)
    code_mode_enabled: bool = False
    domain_subagents_enabled: bool = False
    domain_agent_count: int = Field(default=0, ge=0, le=4)


class ToolsetExecutionIdentity(BaseModel):
    """The effective post-MCP registry and policy installed for one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(min_length=1, max_length=128)
    policy_digest: str = Field(pattern=_SHA256_PATTERN)
    tool_names_digest: str = Field(pattern=_SHA256_PATTERN)
    tool_count: int = Field(ge=0)
    mcp_tool_count: int = Field(default=0, ge=0)
    requested_mcp_count: int = Field(default=0, ge=0)
    requested_skill_count: int = Field(default=0, ge=0)
    active_skill_count: int = Field(default=0, ge=0)
    selection: ToolSelectionExecutionIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    # These values are server-keyed HMACs. Names, descriptions, schemas,
    # instructions and other private source material are never persisted.
    mcp_tools_hmac_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
        exclude_if=lambda value: value is None,
    )
    requested_mcp_hmac_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
        exclude_if=lambda value: value is None,
    )
    active_skill_hmac_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
        exclude_if=lambda value: value is None,
    )

    @field_validator("policy_version")
    @classmethod
    def validate_policy_version(cls, value: str) -> str:
        if safe_public_identifier(value) != value:
            raise ValueError("tool policy version is not safe to persist")
        return value

    @model_validator(mode="after")
    def validate_private_identities(self) -> "ToolsetExecutionIdentity":
        if self.mcp_tool_count and self.mcp_tools_hmac_sha256 is None:
            raise ValueError("MCP tools require an opaque identity")
        if self.requested_mcp_count and self.requested_mcp_hmac_sha256 is None:
            raise ValueError("requested MCP servers require an opaque identity")
        if self.active_skill_count and self.active_skill_hmac_sha256 is None:
            raise ValueError("active skills require an opaque identity")
        return self


class ExecutionEnvironmentSnapshot(BaseModel):
    """Immutable per-task replay identity stored outside browser event history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    execution_mode: Literal["agent", "lightweight"]
    catalog: CordisCatalogIdentity | None = None
    sandbox: SandboxExecutionIdentity | None = None
    models: tuple[ModelExecutionIdentity, ...] = ()
    toolset: ToolsetExecutionIdentity | None = None
    fingerprint: str = Field(pattern=_SHA256_PATTERN)
    # Sequence of the user MessageEvent that created this task. It maps a
    # snapshot to a segment in a multi-task recording and is intentionally not
    # part of the environment fingerprint.
    trigger_event_seq: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_EVENT_SEQUENCE,
        exclude_if=lambda value: value is None,
    )
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("task_id", "session_id")
    @classmethod
    def validate_context_identifier(cls, value: str) -> str:
        if safe_public_identifier(value) != value:
            raise ValueError("snapshot context identifier is not safe to persist")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ExecutionEnvironmentSnapshot":
        roles = [model.role for model in self.models]
        if len(roles) != len(set(roles)):
            raise ValueError("execution snapshot contains duplicate model roles")
        if self.execution_mode == "agent" and self.sandbox is None:
            raise ValueError("agent execution snapshot requires a sandbox identity")
        if self.execution_mode == "lightweight" and self.sandbox is not None:
            raise ValueError("lightweight execution snapshot cannot contain a sandbox identity")
        expected = self.calculate_fingerprint(
            schema_version=self.schema_version,
            execution_mode=self.execution_mode,
            catalog=self.catalog,
            sandbox=self.sandbox,
            models=self.models,
            toolset=self.toolset,
        )
        if self.fingerprint != expected:
            raise ValueError("execution snapshot fingerprint does not match its contents")
        return self

    @staticmethod
    def calculate_fingerprint(
        *,
        schema_version: int,
        execution_mode: str,
        catalog: CordisCatalogIdentity | None,
        sandbox: SandboxExecutionIdentity | None,
        models: tuple[ModelExecutionIdentity, ...],
        toolset: ToolsetExecutionIdentity | None,
    ) -> str:
        model_payloads = []
        for model in models:
            payload = model.model_dump(mode="json")
            if model.custom_prompt_hmac_sha256 is None:
                payload.pop("custom_prompt_hmac_sha256", None)
            model_payloads.append(payload)
        toolset_payload = toolset.model_dump(mode="json") if toolset else None
        if toolset is not None and toolset_payload is not None:
            for field_name in (
                "mcp_tools_hmac_sha256",
                "requested_mcp_hmac_sha256",
                "active_skill_hmac_sha256",
            ):
                if getattr(toolset, field_name) is None:
                    toolset_payload.pop(field_name, None)
        return stable_sha256({
            "schema_version": schema_version,
            "execution_mode": execution_mode,
            "catalog": catalog.model_dump(mode="json") if catalog else None,
            "sandbox": sandbox.model_dump(mode="json") if sandbox else None,
            "models": model_payloads,
            "toolset": toolset_payload,
        })

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        session_id: str,
        execution_mode: Literal["agent", "lightweight"],
        catalog: CordisCatalogIdentity | None,
        sandbox: SandboxExecutionIdentity | None,
        models: tuple[ModelExecutionIdentity, ...],
        toolset: ToolsetExecutionIdentity | None = None,
        trigger_event_seq: int | None = None,
        captured_at: datetime | None = None,
    ) -> "ExecutionEnvironmentSnapshot":
        fingerprint = cls.calculate_fingerprint(
            schema_version=1,
            execution_mode=execution_mode,
            catalog=catalog,
            sandbox=sandbox,
            models=models,
            toolset=toolset,
        )
        return cls(
            task_id=task_id,
            session_id=session_id,
            execution_mode=execution_mode,
            catalog=catalog,
            sandbox=sandbox,
            models=models,
            toolset=toolset,
            fingerprint=fingerprint,
            trigger_event_seq=trigger_event_seq,
            captured_at=captured_at or datetime.now(UTC),
        )
