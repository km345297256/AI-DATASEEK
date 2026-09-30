"""User-evaluated publication must never impersonate scientific verification."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.plan import ExecutionStatus, Step
from app.domain.models.result_assessment import ResultAssessmentMarker, has_user_evaluated_result


MARKER = {"version": 1, "mode": "user_evaluated", "scientific_review": "not_performed",
          "dataset_ids": ["synthetic"], "input_file_ids": ["minio:synthetic"]}


def completed_step():
    return Step(success=True, status=ExecutionStatus.COMPLETED, result="Mean = 7.",
                outcome=AnalysisOutcome(status="succeeded", reason_code="completed"),
                outputs={"result_assessment": deepcopy(MARKER)})


def test_result_assessment_roundtrip_is_separate_from_answer_review():
    marker = ResultAssessmentMarker.model_validate(MARKER)
    assert ResultAssessmentMarker.model_validate_json(marker.model_dump_json()) == marker
    step = completed_step()
    restored = Step.model_validate_json(step.model_dump_json())
    assert has_user_evaluated_result(restored)
    assert "answer_review" not in restored.outputs
    assert restored.outputs["result_assessment"]["scientific_review"] == "not_performed"


@pytest.mark.parametrize("update", [
    {"version": True}, {"version": "1"}, {"mode": "verified"},
    {"scientific_review": "passed"}, {"dataset_ids": [""]},
    {"dataset_ids": [5]}, {"input_file_ids": None}, {"extra_authority": True},
])
def test_invalid_or_scientifically_approved_marker_is_rejected(update):
    marker = {**deepcopy(MARKER), **update}
    with pytest.raises(ValidationError):
        ResultAssessmentMarker.model_validate(marker)
    step = completed_step()
    step.outputs["result_assessment"] = marker
    assert not has_user_evaluated_result(step)


@pytest.mark.parametrize("change", ["missing_marker", "failed", "partial", "pending", "empty", "unknown"])
def test_marker_cannot_promote_failed_or_unconfirmed_execution(change):
    step = completed_step()
    if change == "missing_marker":
        step.outputs.clear()
    elif change == "failed":
        step.success = False
    elif change == "partial":
        step.outcome.status = "partial"
    elif change == "pending":
        step.status = ExecutionStatus.PENDING
    elif change == "empty":
        step.result = " "
    else:
        step.outputs["execution_outcome"] = {"has_unconfirmed_tool_execution": True}
    assert not has_user_evaluated_result(step)
