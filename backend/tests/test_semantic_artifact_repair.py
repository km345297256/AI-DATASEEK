"""Semantic rejection unlocks only authorized working copies, never uploads."""
from copy import deepcopy

import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.services.analysis_artifact_repair import ArtifactRepairTracker


PATH = "/home/ubuntu/output/requested.png"


def requirement(**changes):
    return DeliverableRequirement(kind="image", output_paths=[PATH],
                                  objective="Requested distribution comparison", **changes)


def receipt(path=PATH, digest="a", size=30, **changes):
    return {"path": path, "kind": "image", "valid": True, "sha256": digest * 64,
            "size": size, **changes}


def confirmed():
    return {"code": "completed", "has_unconfirmed_tool_execution": False,
            "side_effect_state": "confirmed_terminal", "execution_evidence": {
                "execution_confirmed": True, "pending_execution": False,
                "unresolved_call_count": 0, "replay_safe": False}}


def tracker():
    return ArtifactRepairTracker(original_goal="Compare the measurements", step_id="step",
                                 requirements=[requirement()])


def test_only_named_semantically_rejected_working_bytes_can_be_replaced():
    controller = tracker()
    wrong = receipt()
    good = receipt("/home/ubuntu/output/valid.png", digest="b")
    assert controller.reject_semantic_candidates([wrong, good], [requirement()]) == {PATH}
    decision = controller.review([wrong, good], [requirement()], confirmed())
    assert decision.allowed
    assert decision.feedback["protected_files"] == [{key: good[key] for key in ("path", "sha256", "size")}]
    assert decision.feedback["replaceable_working_files"] == [{key: wrong[key] for key in ("path", "sha256", "size")}]
    assert decision.feedback["constraints"]["overwrite_protected_files"] is False
    assert decision.feedback["constraints"]["replay_original_step"] is False
    corrected = receipt(digest="c")
    assert controller.semantic_rejected_paths([corrected, good]) == set()
    assert controller.review([corrected, good], [], confirmed()).reason == "no_repair_needed"
    assert controller.review([receipt(digest="d"), good], [], confirmed()).reason == "protected_artifact_changed"


def test_previously_protected_exact_version_can_be_rejected_without_unprotecting_other_outputs():
    controller = tracker()
    original, other = receipt(), receipt("/home/ubuntu/output/other.png", digest="b")
    assert controller.review([original, other], [], confirmed()).reason == "no_repair_needed"
    assert controller.reject_semantic_candidates([original, other], [requirement()]) == {PATH}
    assert controller.review([original, other], [requirement()], confirmed()).allowed
    assert controller.review([receipt(digest="c"), receipt(other["path"], digest="d")], [], confirmed()).reason == "protected_artifact_changed"


def test_rejected_unchanged_bytes_never_reenter_protection_or_count_as_repaired():
    controller = tracker()
    current = receipt()
    controller.reject_semantic_candidates([current], [requirement()])
    decision = controller.review([current], [requirement()], confirmed())
    assert decision.allowed and decision.feedback["protected_files"] == []
    assert controller.review([current], [], confirmed()).reason == "semantic_artifact_unresolved"
    assert controller.semantic_rejected_paths([current]) == {PATH}
    assert controller.review([current], [requirement()], confirmed()).reason == "artifact_repair_no_progress"


def test_new_hash_alone_does_not_extend_semantically_unsuccessful_repairs():
    controller = tracker()
    original, changed = receipt(), receipt(digest="b")
    controller.reject_semantic_candidates([original], [requirement()])
    first = controller.review([original], [requirement()], confirmed())
    assert first.allowed
    controller.reject_semantic_candidates([changed], [requirement()])
    assert controller.semantic_rejected_paths([changed]) == {PATH}
    second = controller.review([changed], [requirement()], confirmed())
    assert not second.allowed and second.reason == "artifact_repair_no_progress"
    assert controller.semantic_rejected_paths([original]) == {PATH}
    assert first.feedback["semantic_rejection_fingerprint"] != second.fingerprint


def test_a_different_protected_version_cannot_be_unlocked_by_semantic_rejection():
    controller = tracker()
    original, changed = receipt(), receipt(digest="b")
    controller.review([original], [], confirmed())
    with pytest.raises(ValueError, match="protected_artifact_changed"):
        controller.reject_semantic_candidates([changed], [requirement()])
    assert not controller.semantic_rejected_paths([changed])
    assert controller.review([changed], [requirement()], confirmed()).reason == "protected_artifact_changed"


def test_rejection_does_not_modify_receipts_or_create_permission_outside_original_contract():
    controller = tracker()
    current = receipt()
    before = deepcopy(current)
    unknown = DeliverableRequirement(kind="image", output_paths=["/home/ubuntu/output/unrequested.png"],
                                    objective="Requested distribution comparison")
    with pytest.raises(ValueError, match="semantic_rejection_outside_contract"):
        controller.reject_semantic_candidates([current], [unknown])
    assert current == before and not controller.semantic_rejected_paths([current])
    assert controller.reject_semantic_candidates([current], [DeliverableRequirement(kind="image")]) == set()


@pytest.mark.parametrize("change", [{"sha256": None}, {"size": 0}, {"valid": "true"},
                                   {"path": "/Users/private/chart.png"}])
def test_semantic_rejection_requires_exact_validated_sandbox_bytes(change):
    controller = tracker()
    with pytest.raises(ValueError):
        controller.reject_semantic_candidates([receipt(**change)], [requirement()])
    assert not controller.semantic_rejected_paths([receipt()])


def test_missing_or_invalid_receipts_never_unlock_a_working_copy():
    controller = tracker()
    failed = receipt(valid=False, reason="invalid_content")
    assert controller.reject_semantic_candidates([failed], [requirement()]) == set()
    assert controller.reject_semantic_candidates([], [requirement()]) == set()


def test_feedback_mutation_cannot_clear_monotonic_rejection_history():
    controller = tracker()
    current = receipt()
    controller.reject_semantic_candidates([current], [requirement()])
    first = controller.review([current], [requirement()], confirmed())
    first.feedback["semantic_rejections"][0]["sha256"] = "b" * 64
    first.feedback["replaceable_working_files"].clear()
    assert controller.semantic_rejected_paths([current]) == {PATH}
    assert controller.semantic_rejected_paths([receipt(digest="b")]) == set()


def test_rejection_never_waives_confirmed_execution_or_content_validation():
    controller = tracker()
    current = receipt()
    controller.reject_semantic_candidates([current], [requirement()])
    assert controller.review([current], [requirement()], {}).reason == "execution_not_confirmed"
    assert controller.review([current], [requirement()], confirmed(), validation_available=False).reason == "validation_unavailable"
