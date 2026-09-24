"""Historical explanations reuse authenticated findings, not execution authority."""
import copy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import PlanEvent, PlanStatus
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.analysis_answer_review import AnswerEvidence, _citations, review_answer
from app.domain.services.analysis_scientific_review import SCIENTIFIC_DIMENSIONS
from app.domain.services.analysis_text_scientific_review import (
    ANSWER_HISTORICAL_EVIDENCE_RULES,
    FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES,
    answer_scientific_review_metadata,
    freeze_answer_history_sources,
)
from app.domain.services.execution_history import reviewed_history_steps


HISTORY = "Earlier measured mean was 3 mg; the result was descriptive, not causal."
PARAGRAPHS = ["The earlier result reported a descriptive mean of 3 mg, not a causal effect."]


def step(**updates):
    return Step(id="previous", success=True, status=ExecutionStatus.COMPLETED,
        outcome=AnalysisOutcome(status="succeeded", reason_code="completed"), result=HISTORY,
        outputs={"answer_review": {"version": 1, "status": "verified",
            "dataset_ids": ["dataset-a"], "input_file_ids": ["upload-a"]}}).model_copy(update=updates, deep=True)


def admitted(previous=None, *, current_step_id="current"):
    evidence = AnswerEvidence()
    evidence.observe_reviewed_result(previous or step())
    evidence.begin_step(current_step_id)
    sources = evidence.render_sources()
    return {item["source_id"]: item for item in sources}, freeze_answer_history_sources(
        sources, current_step_id=current_step_id)


def checks(scope="historical_explanation"):
    return [{"dimension": dimension, "status": "verified", "paragraph_indices": [0],
             "evidence_scope": scope,
             "evidence": [{"source_id": "prior_review_0001", "quote": "Earlier measured mean was 3 mg"}]}
            for dimension in SCIENTIFIC_DIMENSIONS]


def run(*, items=None, lookup=None, bindings=None, **kwargs):
    original_lookup, original_bindings = admitted()
    return answer_scientific_review_metadata(checks() if items is None else items,
        request="Explain the earlier result in more detail.", paragraphs=PARAGRAPHS,
        citations=_citations, lookup=original_lookup if lookup is None else lookup,
        historical_sources=original_bindings if bindings is None else bindings,
        current_step_id="current", **kwargs)


def test_authenticated_reviewed_history_can_support_all_dimensions_without_new_tools():
    lookup, bindings = admitted()
    before = copy.deepcopy((lookup, bindings))
    result = run(lookup=lookup, bindings=bindings)
    assert result["status"] == "verified" and result["checked_dimensions"] == 4
    assert all(row["evidence_source_count"] == 1 for row in result["dimensions"])
    assert (lookup, bindings) == before
    for secret in (HISTORY, "prior_review_0001", "previous", bindings["prior_review_0001"]):
        assert secret not in json.dumps(result)


def test_host_history_disambiguates_reused_step_ids_across_plans():
    lookup, bindings = admitted(step(id="current"))
    assert run(lookup=lookup, bindings=bindings)["status"] == "verified"


@pytest.mark.parametrize("scope", ["current_observation", None])
def test_history_cannot_certify_current_observations_or_new_execution(scope):
    items = checks(scope)
    if scope is None:
        for item in items:
            item.pop("evidence_scope")
    result = run(items=items)
    assert result["status"] == "unavailable"
    assert result["dimensions"][0]["reason"] == "answer_scientific_source_not_independent"


def test_bare_prior_review_kind_without_private_host_snapshot_is_not_authority():
    assert run(bindings={})["status"] == "unavailable"


@pytest.mark.parametrize("changes", [
    {"text": HISTORY + " Now says something else."}, {"step_id": "different"},
    {"state": "failed"}, {"state": "pending"}, {"kind": "tool_result"},
    {"source_id": "another"}, {"truncated": True}, {"truncated": None},
    {"code_only": True}, {"self_content_only": True}, {"current_request": True},
])
def test_mutating_history_after_freezing_invalidates_the_scientific_citation(changes):
    lookup, bindings = admitted()
    lookup["prior_review_0001"].update(changes)
    assert run(lookup=lookup, bindings=bindings)["status"] == "unavailable"


def test_history_digest_is_not_just_a_filename_or_prose_digest():
    lookup, bindings = admitted()
    assert bindings["prior_review_0001"] != hashlib.sha256(HISTORY.encode()).hexdigest()
    assert run(lookup=lookup, bindings={"prior_review_0001": "0" * 64})["status"] == "unavailable"


@pytest.mark.parametrize("changes", [
    {"success": False}, {"status": ExecutionStatus.RUNNING},
    {"outcome": AnalysisOutcome(status="partial", reason_code="answer_validation_unavailable")},
    {"outputs": {"answer_review": {"version": 2, "status": "verified"}}},
    {"outputs": {"answer_review": {"version": 1, "status": "unavailable"}}},
    {"outputs": {}},
])
def test_unreviewed_failed_running_or_legacy_step_cannot_enter_history_scope(changes):
    lookup, bindings = admitted(step(**changes))
    assert not lookup and not bindings
    assert run(lookup=lookup, bindings=bindings)["status"] == "unavailable"


def test_exact_dataset_and_upload_scope_must_match_before_history_admission():
    events = [PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(id="previous-plan", steps=[step()]))]
    assert reviewed_history_steps(events, dataset_ids={"dataset-a"}, input_file_ids={"upload-a"})
    assert not reviewed_history_steps(events, dataset_ids={"dataset-b"}, input_file_ids={"upload-a"})
    assert not reviewed_history_steps(events, dataset_ids={"dataset-a"}, input_file_ids={"upload-b"})
    assert not reviewed_history_steps(events, dataset_ids=set(), input_file_ids=set())


def test_revoked_latest_plan_result_cannot_reenter_history():
    events = [PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(id="previous-plan", steps=[step()])),
              PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(id="previous-plan", steps=[step(success=False)]))]
    assert not reviewed_history_steps(events, dataset_ids={"dataset-a"}, input_file_ids={"upload-a"})


def test_raw_earlier_tool_result_never_acquires_completed_history_authority():
    lookup, bindings = admitted()
    lookup["prior_review_0001"].update(kind="tool_result", state="succeeded", function="shell_exec",
                                      write_only=False)
    assert not freeze_answer_history_sources(list(lookup.values()), current_step_id="current")
    assert run(lookup=lookup, bindings=bindings)["status"] == "unavailable"


def test_current_observations_must_not_be_mixed_into_a_historical_explanation_check():
    lookup, bindings = admitted()
    lookup["tool_0001_result"] = {"source_id": "tool_0001_result", "kind": "tool_result",
        "state": "succeeded", "function": "shell_exec", "step_id": "current", "truncated": False,
        "write_only": False, "text": "New measured mean is 6 mg."}
    items = checks()
    for item in items:
        item["evidence"].append({"source_id": "tool_0001_result", "quote": "New measured mean is 6 mg."})
    assert run(items=items, lookup=lookup, bindings=bindings)["status"] == "unavailable"


@pytest.mark.parametrize("flag", ["method_complete", "request_complete", "draft_complete"])
def test_historical_explanation_does_not_bypass_host_completeness(flag):
    assert run(**{flag: False})["status"] == "unavailable"


def test_historical_explanation_does_not_override_a_substantive_error():
    items = checks()
    items[2].update(status="rejected", evidence=[])
    result = run(items=items)
    assert result["status"] == "rejected"
    assert result["dimensions"][2]["reason"] == "answer_scientific_claim_rejected"


def test_empty_historical_citations_can_reject_but_never_verify():
    items = checks()
    for item in items:
        item.update(evidence=[])
    assert run(items=items, bindings={})["status"] == "unavailable"
    for item in items:
        item.update(status="rejected")
    assert run(items=items, bindings={})["status"] == "rejected"


@pytest.mark.parametrize("scope", ["", "new_experiment", None, True, {}])
def test_invalid_evidence_scope_is_a_schema_failure_not_a_scientific_rejection(scope):
    result = run(items=checks(scope))
    assert result["status"] == "unavailable"
    assert result["reason"] == "answer_scientific_check_invalid"


def test_duplicate_source_ids_are_not_authenticated_by_a_snapshot():
    lookup, _ = admitted()
    assert freeze_answer_history_sources(list(lookup.values()) * 2, current_step_id="current") == {}


def test_final_and_initial_review_share_historical_semantics():
    assert ANSWER_HISTORICAL_EVIDENCE_RULES in FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES
    assert "historical_explanation" in FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES
    assert "ONLY answer_scientific_checks and answer_scope_check" in FINAL_ANSWER_SCIENTIFIC_REVIEW_RULES
    assert "current file availability" in ANSWER_HISTORICAL_EVIDENCE_RULES
    assert "same session and exactly matching input scope" in ANSWER_HISTORICAL_EVIDENCE_RULES


@pytest.mark.asyncio
@pytest.mark.parametrize("publication_change", [False, True])
async def test_followup_review_freezes_authorized_history_for_initial_and_final_candidate(publication_change):
    question = "Explain the earlier result in more detail."
    evidence = AnswerEvidence()
    evidence.observe_reviewed_result(step())
    evidence.begin_step("current")
    paragraphs = [{"text": PARAGRAPHS[0], "kind": "analysis", "evidence": checks()[0]["evidence"]}]
    if publication_change:
        paragraphs.insert(0, {"text": question, "kind": "context",
            "evidence": [{"source_id": "current_request", "quote": question}]})
    scientific_checks = checks()
    for check in scientific_checks:
        check["paragraph_indices"] = list(range(len(paragraphs)))
    final_checks = {"answer_scientific_checks": scientific_checks,
        "answer_scope_check": {"status": "complete", "paragraph_indices": list(range(len(paragraphs))),
                               "completion_blocker": "none"}}
    responses = [AIMessage(content=json.dumps({"paragraphs": paragraphs,
        "unsupported_claims": False, "requirement_checks": []})),
        AIMessage(content=json.dumps(final_checks))]
    ask = AsyncMock(side_effect=responses)
    reviewed = await review_answer(ask=ask, question=question, draft=PARAGRAPHS[0],
        files=[], evidence=evidence, answer_scientific_scope=True, language="en")
    assert reviewed.status == "verified"
    assert reviewed.metadata["answer_scientific_review"]["status"] == "verified"
    assert reviewed.metadata["answer_scope_review"]["status"] == "verified"
    assert ask.await_count == 2
    initial = json.loads(ask.await_args_list[0].args[0][1].content)
    assert initial["publication_review"]["phase"] == "grounded_answer"
    for call in ask.await_args_list:
        payload = json.loads(call.args[0][1].content)
        assert payload["historical_source_ids"] == ["prior_review_0001"]
        assert any(source["kind"] == "prior_review" for source in payload["sources"])
        assert not any(source["kind"] == "tool_result" for source in payload["sources"])
    final = json.loads(ask.await_args_list[1].args[0][1].content)
    expected = (["Current user request: " + question] if publication_change else []) + PARAGRAPHS
    assert final["frozen_paragraphs"] == expected
    assert final["candidate_paragraphs"] == [{"index": index, "text": text}
        for index, text in enumerate(expected)]
    assert final["paragraph_indices"] == list(range(len(expected)))
    assert reviewed.metadata["answer_scientific_review"]["candidate_text_sha256"] == hashlib.sha256(
        reviewed.text.encode()).hexdigest()
