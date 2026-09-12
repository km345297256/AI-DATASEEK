"""Safe absence permits a new completion operation, never a blind replay."""
from copy import deepcopy

import pytest

from app.domain.models.analysis_outcome import ArtifactIssue, DeliverableRequirement
from app.domain.services.analysis_artifact_repair import ArtifactRepairTracker
from app.domain.services.analysis_completion import assess_delivery


def confirmed():
    return {"code": "completed", "has_unconfirmed_tool_execution": False,
            "side_effect_state": "confirmed_terminal", "execution_evidence": {
                "execution_confirmed": True, "pending_execution": False,
                "unresolved_call_count": 0, "tracked_operation_count": 1,
            }}


def missing(name="any-name.csv", **changes):
    return {"path": "/home/ubuntu/output/" + name, "kind": "table", "expected_kind": "table",
            "valid": False, "reason": "missing_artifact", "sha256": None, "size": None, **changes}


def tracker():
    return ArtifactRepairTracker(original_goal="Create the requested measured table and preserve original observations",
                                 step_id="step", requirements=[{"kind": "table", "formats": ["csv"]}])


def test_proven_missing_allows_only_new_local_completion_with_original_contract():
    decision = tracker().review([missing()], [{"kind": "table", "formats": ["csv"]}], confirmed())
    assert decision.allowed
    assert decision.feedback["failed_files"][0]["reason"] == "missing_artifact"
    assert decision.feedback["requirements"][0]["formats"] == ["csv"]
    assert decision.feedback["constraints"]["new_local_operations_only"] is True
    assert decision.feedback["constraints"]["replay_original_step"] is False
    assert decision.feedback["constraints"]["preserve_dataset_read_only"] is True


@pytest.mark.parametrize("reason", ["unavailable_or_unsafe_path", "changed_during_read", "not_regular_file"])
def test_old_unsafe_and_changed_reasons_are_not_promoted_to_missing(reason):
    assert tracker().review([missing(reason=reason)], [{"kind": "table"}], confirmed()).reason == "validation_not_locally_repairable"


@pytest.mark.parametrize("change", ["unknown", "pending", "unresolved", "unconfirmed", "unavailable"])
def test_missing_does_not_bypass_execution_and_validation_proof(change):
    execution = confirmed()
    if change == "unknown":
        execution["side_effect_state"] = "unknown"
    elif change == "pending":
        execution["execution_evidence"]["pending_execution"] = True
    elif change == "unresolved":
        execution["execution_evidence"]["unresolved_call_count"] = 1
    elif change == "unconfirmed":
        execution["has_unconfirmed_tool_execution"] = True
    assert not tracker().review([missing()], [{"kind": "table"}], execution,
                                validation_available=change != "unavailable").allowed


@pytest.mark.parametrize("changes", [{"sha256": "a" * 64}, {"size": 0}, {"size": 10}])
def test_missing_receipts_cannot_also_claim_observed_bytes(changes):
    assert tracker().review([missing(**changes)], [{"kind": "table"}], confirmed()).reason == "invalid_validation_evidence"


@pytest.mark.parametrize("new_name", ["any-name.csv", "different-name.csv"])
def test_unchanged_missing_count_or_renaming_is_not_progress(new_name):
    controller = tracker()
    assert controller.review([missing()], [{"kind": "table"}], confirmed()).allowed
    assert controller.review([missing(new_name)], [{"kind": "table"}], confirmed()).reason == "artifact_repair_no_progress"


def test_missing_does_not_allow_recreating_a_previously_verified_file():
    controller = tracker()
    good = missing("finished.csv", valid=True, reason="validated", sha256="a" * 64, size=20)
    assert controller.review([good, missing()], [{"kind": "table"}], confirmed()).allowed
    now_missing = deepcopy(good)
    now_missing.update(valid=False, reason="missing_artifact", sha256=None, size=None)
    assert controller.review([now_missing, missing()], [{"kind": "table"}], confirmed()).reason == "protected_artifact_changed"


def test_public_missing_issue_preserves_type_and_count_not_private_path():
    result = assess_delivery(requirements=[DeliverableRequirement(kind="table", min_count=2)],
                             records=[missing("arbitrary-名称.csv")], delivered=[], execution_success=True)
    assert result.status == "failed" and result.missing[0].min_count == 2
    assert result.issues[0] == ArtifactIssue(artifact_name="arbitrary-名称.csv", kind="table",
                                           reason_code="missing_artifact", blocking=True)
    assert "/home/" not in result.model_dump_json()
