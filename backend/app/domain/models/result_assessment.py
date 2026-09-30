"""Host publication policy, deliberately distinct from scientific approval.

This marker records how a result was published; it is not an execution receipt,
permission grant or a claim that the answer is scientifically correct.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

from app.domain.models.plan import ExecutionStatus, Step


class ResultAssessmentMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    mode: Literal["user_evaluated"]
    scientific_review: Literal["not_performed"]
    dataset_ids: list[StrictStr]
    input_file_ids: list[StrictStr]

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("invalid_result_assessment_version")
        return value

    @field_validator("dataset_ids", "input_file_ids")
    @classmethod
    def nonempty_identities(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("invalid_result_assessment_scope")
        return values


def has_user_evaluated_result(step: Step) -> bool:
    """Allow completed prose as scoped context, never as reviewed evidence."""
    try:
        ResultAssessmentMarker.model_validate(step.outputs.get("result_assessment"))
    except ValidationError:
        return False
    execution = step.outputs.get("execution_outcome", {})
    if not isinstance(execution, dict):
        return False
    if execution.get("has_unconfirmed_tool_execution") or execution.get("side_effect_state") == "unknown":
        return False
    return bool(
        step.outcome is not None and step.outcome.status == "succeeded"
        and step.success and step.status == ExecutionStatus.COMPLETED
        and isinstance(step.result, str) and step.result.strip()
    )
