"""Public run records reconstructed from the durable, ordered event journal."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.domain.models.skill_manifest import PACKAGE_ID, SkillToolDependency


class RunSkillRelease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    package_id: str = Field(pattern=f"^{PACKAGE_ID}$")
    version: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value):
        return SkillToolDependency.exact_version(value)

    @classmethod
    def from_skill(cls, skill):
        manifest = skill.manifest
        return cls(package_id=manifest.package_id,
                   version=SkillToolDependency.exact_version(manifest.version), sha256=skill.package_sha256)


class DataRunStep(BaseModel):
    step_id: str
    ordinal: int
    status: Literal["pending", "running", "succeeded", "failed", "unknown"]
    outcome_status: Literal["succeeded", "partial", "failed"] | None = None


class DataRun(BaseModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_event_seq: int
    revision: int
    status: Literal["running", "waiting", "succeeded", "partial", "failed", "unknown"] = "running"
    started_at: int
    ended_at: int | None = None
    plan_revision: int | None = None
    steps: list[DataRunStep] = Field(default_factory=list)
    skills: list[RunSkillRelease] = Field(default_factory=list)
    legacy_skill_count: int = 0
    environment_fingerprint: str | None = None
    catalog_revision: str | None = None
    history_complete: bool = True
    # No automatic replay is authorized by the presence of a run record.
    recovery: Literal["inspect_only"] = "inspect_only"


class DataRunPage(BaseModel):
    runs: list[DataRun]
    next_before_seq: int | None = None
