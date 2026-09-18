"""Typed deliverables and outcomes; file paths never belong in the public outcome."""
from pathlib import PurePosixPath
from typing import Literal, get_args

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator
from typing_extensions import TypedDict

DELIVERABLE_LABELS = {"image": "图表", "table": "数据表", "report": "报告", "code": "代码", "any": "结果文件"}

ArtifactReasonCode = Literal[
    "missing_artifact",
    "invalid_content", "unavailable_or_unsafe_path", "unsupported_kind", "unsupported_format",
    "kind_mismatch", "format_mismatch", "not_regular_file", "file_size_limit", "batch_size_limit",
    "empty_file", "changed_during_read", "empty_or_binary_text", "image_size_limit", "table_size_limit",
    "inconsistent_table_width", "empty_table", "archive_size_limit", "encrypted_workbook",
    "unsafe_workbook_xml", "worksheet_count_limit", "invalid_json_constant", "duplicate_json_key",
    "invalid_json_table", "invalid_notebook", "validation_deadline", "validator_unavailable",
    "validation_unavailable", "validation_receipt_invalid", "delivery_failed", "invalid_csv_syntax",
    "invalid_text_encoding", "invalid_json_syntax", "invalid_code_syntax",
]
ARTIFACT_ISSUE_REASONS = frozenset(get_args(ArtifactReasonCode))


def safe_artifact_name(value: object) -> str:
    """A bounded display basename, never a private path or parser excerpt."""
    if not isinstance(value, str):
        return "未命名文件"
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if not unicodedata.category(char).startswith("C"))
    name = name.replace(":", "_").strip()[:160]
    return name if name and name not in {".", ".."} else "未命名文件"


class ArtifactIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always", frozen=True)
    artifact_name: str = Field(min_length=1, max_length=160)
    kind: Literal["image", "table", "report", "code", "any"]
    reason_code: ArtifactReasonCode
    blocking: bool = Field(strict=True)

    @field_validator("artifact_name")
    @classmethod
    def validate_display_name(cls, value):
        if value != safe_artifact_name(value):
            raise ValueError("Artifact name must be a safe display basename")
        return value

    @field_serializer("artifact_name")
    def serialize_display_name(self, value):
        return safe_artifact_name(value)

    @field_serializer("reason_code")
    def serialize_reason(self, value):
        return value if isinstance(value, str) and value in ARTIFACT_ISSUE_REASONS else "invalid_content"

    @field_serializer("kind")
    def serialize_kind(self, value):
        return value if isinstance(value, str) and value in DELIVERABLE_LABELS else "any"

    @field_serializer("blocking")
    def serialize_blocking(self, value):
        return value if type(value) is bool else False


class DeliverableRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
    kind: Literal["image", "table", "report", "code", "any"]
    min_count: int = Field(default=1, strict=True, ge=1, le=16)
    formats: list[str] = Field(default_factory=list, max_length=8)
    label: str = Field(default="", max_length=200)
    # Private execution contract, established before execution. Never derive
    # these obligations from the executor's final attachment/prose claims.
    output_paths: list[str] = Field(default_factory=list, max_length=16)
    objective: str = Field(default="", max_length=500)

    @field_validator("output_paths")
    @classmethod
    def safe_output_paths(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            if (len(value) > 4096 or not value.startswith("/home/ubuntu/output/")
                    or str(PurePosixPath(value)) != value
                    or ".." in PurePosixPath(value).parts or "\\" in value
                    or any(unicodedata.category(char).startswith("C") for char in value)):
                raise ValueError("Deliverable paths must be canonical sandbox output file paths")
            if value not in normalized:
                normalized.append(value)
        return normalized

    @field_validator("objective")
    @classmethod
    def safe_objective(cls, value: str) -> str:
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Deliverable objectives must be plain text")
        # Objectives are semantic descriptions also visible in plan events.
        # Put exact file identities in the root-restricted output_paths field,
        # never in a free-text field that could disclose a real host path.
        semantic = re.sub(r"\bhttps?://[^\s]+", "", value)
        if (re.search(r"(?<!\s)/|/(?!\s)", semantic)
                or re.search(r"[A-Za-z]:[\\/]|\\\\", semantic)):
            raise ValueError("Deliverable objectives must not contain filesystem paths")
        return value.strip()

    @field_validator("formats")
    @classmethod
    def safe_formats(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            suffix = value.removeprefix(".").lower()
            if not re.fullmatch(r"[a-z0-9]{1,16}", suffix):
                raise ValueError("Deliverable formats must be file extensions")
            if suffix not in normalized:
                normalized.append(suffix)
        return normalized

    @model_validator(mode="after")
    def public_label(self):
        # Labels are display decoration, never model-provided filenames/paths.
        self.label = DELIVERABLE_LABELS[self.kind]
        # Every explicit identity is required, not a pool from which one image
        # may be chosen. Additional anonymous slots preserve the count floor.
        self.min_count = max(self.min_count, len(self.output_paths))
        return self

    @field_serializer("label")
    def serialize_public_label(self, value):
        return DELIVERABLE_LABELS[self.kind]

    @field_serializer("formats")
    def serialize_public_formats(self, values):
        return self.safe_formats(values)


class _PublicDeliverableRequirement(TypedDict):
    kind: Literal["image", "table", "report", "code", "any"]
    min_count: int
    formats: list[str]
    label: str


class AnalysisOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded", "partial", "failed"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    missing: list[DeliverableRequirement] = Field(default_factory=list, max_length=16)
    issues: list[ArtifactIssue] = Field(default_factory=list, max_length=64)
    can_resume: bool = Field(default=False, strict=True)
    resume_from: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_serializer("missing")
    def serialize_public_missing(self, values) -> list[_PublicDeliverableRequirement]:
        # A requirement is persisted in private plans/checkpoints, but the
        # public outcome contains only types/counts. Project explicitly even
        # when an internal model_copy bypassed nested model validation.
        public = []
        for value in values[:16] if isinstance(values, list) else []:
            try:
                item = DeliverableRequirement.model_validate(value)
            except (ValueError, TypeError):
                continue
            public.append({"kind": item.kind, "min_count": item.min_count,
                           "formats": item.formats, "label": DELIVERABLE_LABELS[item.kind]})
        return public

    @field_serializer("issues")
    def serialize_public_issues(self, values):
        # Internal model_copy/update calls can bypass Pydantic validation. Do
        # not let such a copy forward an arbitrary receipt dictionary to SSE.
        public = []
        for value in values[:64] if isinstance(values, list) else []:
            item = value.model_dump() if isinstance(value, ArtifactIssue) else value
            if not isinstance(item, dict):
                continue
            kind, reason = item.get("kind"), item.get("reason_code")
            public.append({
                "artifact_name": safe_artifact_name(item.get("artifact_name")),
                "kind": kind if isinstance(kind, str) and kind in DELIVERABLE_LABELS else "any",
                "reason_code": reason if isinstance(reason, str) and reason in ARTIFACT_ISSUE_REASONS else "invalid_content",
                "blocking": item.get("blocking") if type(item.get("blocking")) is bool else False,
            })
        return public
