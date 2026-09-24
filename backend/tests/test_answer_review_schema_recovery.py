"""Shape recovery precedes acceptance and cannot create execution permission."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import historical_step, paragraph, response, tool


GOOD = "The observed mean is 3 mg."
PRIVATE = "REJECTED_RESPONSE_SENTINEL /Users/private/secret-key"
REQUIREMENT = DeliverableRequirement(kind="table", objective="Compute the mean")


def observations(*, historical=False, failed_only=False):
    evidence = review.AnswerEvidence()
    evidence.begin_step("current")
    if historical:
        evidence.observe_reviewed_result(historical_step())
    else:
        evidence.observe(tool(success=not failed_only,
                              data={"stdout": "mean not computed"} if failed_only else None))
    return evidence


def shape_response(mutation):
    value = json.loads(response(paragraph(GOOD)).content)
    if mutation == "root_fields": value = {"unexpected": PRIVATE}
    elif mutation == "root_type": value["unsupported_claims"] = "true"
    elif mutation == "empty_paragraphs": value["paragraphs"] = []
    elif mutation == "paragraph_fields": value["paragraphs"].append({"text": PRIVATE})
    elif mutation == "paragraph_scalar": value["paragraphs"].append(PRIVATE)
    elif mutation == "paragraph_kind": value["paragraphs"].append({"text": PRIVATE, "kind": [], "evidence": []})
    elif mutation == "paragraph_text": value["paragraphs"].append({"text": None, "kind": "analysis", "evidence": []})
    elif mutation == "paragraph_empty": value["paragraphs"].append({"text": " ", "kind": "analysis", "evidence": []})
    else: raise AssertionError(mutation)
    return AIMessage(content=json.dumps(value))


def covered_response(*paragraphs, checks=None, complete=True):
    value = json.loads(response(*paragraphs, checks=checks).content)
    value["answer_complete"] = complete
    return AIMessage(content=json.dumps(value))


async def run(*responses, evidence=None, requirements=()):
    evidence = observations() if evidence is None else evidence
    before = copy.deepcopy(evidence.__dict__)
    ask = AsyncMock(side_effect=list(responses))
    result = await review.review_answer(ask=ask, question="Explain the observed data", draft="Original draft",
        files=[], evidence=evidence, requirements=requirements, language="en")
    assert evidence.__dict__ == before
    assert PRIVATE not in result.text + json.dumps(result.metadata)
    return result, ask


def assert_frozen_shape_recovery(ask):
    first, second = (call.args[0] for call in ask.await_args_list[:2])
    assert first[1] is second[1] and first[1].content == second[1].content
    assert first[0].content in second[0].content
    assert "invalid structural schema" in second[0].content
    assert "json" in second[0].content.lower()
    assert all(message.type in {"system", "human"} for call in ask.await_args_list for message in call.args[0])
    assert all(PRIVATE not in message.content for call in ask.await_args_list for message in call.args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["root_fields", "root_type", "empty_paragraphs", "paragraph_fields",
                                      "paragraph_scalar", "paragraph_kind", "paragraph_text", "paragraph_empty"])
async def test_shape_recovery_reuses_frozen_scope_without_echoing_rejected_response(mutation):
    result, ask = await run(shape_response(mutation), covered_response(paragraph(GOOD)))
    assert result.status == "corrected" and result.text == GOOD and ask.await_count == 2
    assert result.metadata["review_schema_repair_attempted"] is True
    assert result.metadata["review_schema_repair_status"] == "corrected"
    assert result.metadata["schema_recovery_withheld_negative_count"] == 0
    assert "citation_repair_attempted" not in result.metadata
    assert_frozen_shape_recovery(ask)


@pytest.mark.asyncio
async def test_real_prior_review_is_preserved_for_explanation_schema_recovery_without_fresh_tools():
    supported = paragraph("The earlier result reported a mean of 3 mg.", source="prior_review_0001", quote="Earlier measured mean was 3 mg.")
    result, ask = await run(shape_response("root_fields"), covered_response(supported), evidence=observations(historical=True))
    assert result.status == "corrected" and result.text == supported["text"]
    assert result.metadata["file_count"] == 0 and result.missing_requirement_indices == ()
    assert_frozen_shape_recovery(ask)
    payload = json.loads(ask.await_args_list[1].args[0][1].content)
    assert any(source["kind"] == "prior_review" for source in payload["sources"])
    assert not any(source["kind"] == "tool_result" for source in payload["sources"])


@pytest.mark.asyncio
async def test_entire_shape_is_checked_before_any_paragraph_acceptance(monkeypatch):
    original = review._paragraph_text
    inspected = []
    def observe(paragraph_value, *args):
        inspected.append(paragraph_value.get("text"))
        return original(paragraph_value, *args)
    monkeypatch.setattr(review, "_paragraph_text", observe)
    initial = shape_response("paragraph_fields")
    replacement = "The replacement answer uses the same observed mean of 3 mg."
    result, ask = await run(initial, covered_response(paragraph(replacement)))
    assert result.status == "corrected" and result.text == replacement
    assert GOOD not in inspected and PRIVATE not in inspected
    assert "citation_repair_attempted" not in result.metadata and ask.await_count == 2


@pytest.mark.asyncio
async def test_repeated_wrong_shape_stops_after_one_protocol_recovery_without_publication():
    result, ask = await run(shape_response("paragraph_fields"), shape_response("paragraph_scalar"), response(paragraph(GOOD)))
    assert result.status == "unavailable" and GOOD not in result.text
    assert result.metadata["review_schema_repair_status"] == "unavailable"
    assert result.metadata["review_schema_error"] == "review_schema_root_fields"
    assert result.metadata["review_schema_diagnostics"] == {
        "missing_fields": ["answer_complete"], "extra_field_count": 0}
    assert result.missing_requirement_indices == () and ask.await_count == 2
    assert_frozen_shape_recovery(ask)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_shape_failure", [False, True])
async def test_cancellation_propagates_without_another_protocol_attempt(after_shape_failure):
    ask = AsyncMock(side_effect=([shape_response("root_fields")] if after_shape_failure else []) + [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await review.review_answer(ask=ask, question="Explain", draft="", files=[], evidence=observations())
    assert ask.await_count == 1 + int(after_shape_failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_shape_failure", [False, True])
@pytest.mark.parametrize("kind", ["array", "scalar", "duplicate_key", "nonfinite", "tool_call"])
async def test_parser_or_tool_boundary_never_starts_another_shape_recovery(kind, after_shape_failure):
    content = {"array": "[]", "scalar": "true", "duplicate_key": '{"a":0,"a":1}',
               "nonfinite": '{"a":NaN}', "tool_call": "{}"}[kind]
    rejected = AIMessage(content=content, tool_calls=[{"id": "forbidden", "name": "shell_run", "args": {"command": PRIVATE}}] if kind == "tool_call" else [])
    replies = ([shape_response("root_fields")] if after_shape_failure else []) + [rejected, response(paragraph(GOOD))]
    result, ask = await run(*replies)
    assert result.status == "unavailable" and ask.await_count == 1 + int(after_shape_failure)
    assert result.missing_requirement_indices == ()
    if kind == "tool_call": assert result.metadata["reason"] == "review_requested_tools"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape_problem", ["missing", "duplicate", "bad_index", "bad_status"])
async def test_requirement_shape_recovery_can_confirm_only_existing_observed_positive_proof(shape_problem):
    check = {"index": 0, "status": "met", "evidence": [{"source_id": "tool_0001_result", "quote": "mean=3"}]}
    invalid_checks = {"missing": [], "duplicate": [check, check], "bad_index": [{**check, "index": True}],
                      "bad_status": [{**check, "status": []}]}[shape_problem]
    result, ask = await run(response(paragraph(GOOD), checks=invalid_checks), covered_response(paragraph(GOOD), checks=[check]), requirements=[REQUIREMENT])
    assert result.status == "corrected" and result.text == GOOD and ask.await_count == 2
    assert result.metadata["review_schema_error"] == {
        "missing": "review_requirement_missing", "duplicate": "review_requirement_duplicate",
        "bad_index": "review_requirement_index", "bad_status": "review_requirement_status",
    }[shape_problem]
    assert result.missing_requirement_indices == ()


@pytest.mark.parametrize("check, code", [
    ("PRIVATE_REQUIREMENT", "review_requirement_object"),
    ({"private_unknown_key": "PRIVATE_REQUIREMENT"}, "review_requirement_fields"),
    ({"index": False, "status": "met", "evidence": []}, "review_requirement_index"),
    ({"index": 0, "status": [], "evidence": []}, "review_requirement_status"),
    ({"index": 0, "status": "unclear", "evidence": "PRIVATE_REQUIREMENT"}, "review_requirement_evidence_type"),
])
def test_requirement_shape_diagnostics_are_fixed_and_do_not_echo_invalid_values(check, code):
    value = json.loads(response(paragraph(GOOD), checks=[check]).content)
    with pytest.raises(review.ReviewSchemaError) as caught:
        review._validate_review_shape(value, [{"index": 0, "objective": "Compute the mean"}])
    assert caught.value.code == code and "PRIVATE_REQUIREMENT" not in str(caught.value)


@pytest.mark.asyncio
async def test_schema_recovery_cannot_turn_observed_failure_into_new_negative_execution_permission():
    check = {"index": 0, "status": "confirmed_not_performed", "evidence": [{"source_id": "tool_0001_result", "quote": "mean not computed"}]}
    supported = paragraph("The observed tool could not compute the mean.", kind="limitation", quote="mean not computed")
    result, ask = await run(shape_response("root_fields"), covered_response(supported, checks=[check]),
                            evidence=observations(failed_only=True), requirements=[REQUIREMENT])
    assert result.status == "unavailable" and result.metadata["reason"] == "requirements_unverified"
    assert result.metadata["schema_recovery_withheld_negative_count"] == 1
    assert result.missing_requirement_indices == () and ask.await_count == 2


@pytest.mark.asyncio
async def test_schema_recovery_then_scoped_check_correction_still_cannot_authorize_replay():
    negative = {"index": 0, "status": "confirmed_not_performed", "evidence": [{"source_id": "tool_0001_result", "quote": "mean=3"}]}
    forbidden = AIMessage(content=json.dumps({"paragraph_corrections": [], "requirement_corrections": [{"index": 0,
        "check": {"index": 0, "status": "confirmed_not_performed", "evidence": ["tool_0001_result:excerpt_0001"]}}]}))
    result, ask = await run(shape_response("root_fields"), covered_response(paragraph(GOOD), checks=[negative]), forbidden, requirements=[REQUIREMENT])
    assert result.status == "unavailable" and GOOD in result.text and ask.await_count == 3
    assert result.metadata["schema_recovery_withheld_negative_count"] == 1
    assert result.missing_requirement_indices == ()
    last = json.loads(ask.await_args_list[-1].args[0][1].content)
    assert last["failed_requirement_checks"] == [{"index": 0, "status": "unclear", "evidence": []}]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_shape_recovery", [False, True])
async def test_valid_shape_with_semantic_failure_uses_only_existing_scoped_locked_recovery(prior_shape_recovery):
    semantic = (covered_response if prior_shape_recovery else response)(paragraph(GOOD), paragraph("UNSUPPORTED", quote="bad citation"))
    repair = AIMessage(content=json.dumps({"answer_complete": True, "paragraph_corrections": [{"index": 1,
        "paragraph": {"text": "The measured mean is 3 mg.", "kind": "analysis", "evidence": ["tool_0001_result:excerpt_0001"]}}], "requirement_corrections": []}))
    result, ask = await run(*([shape_response("root_fields")] if prior_shape_recovery else []), semantic, repair)
    assert result.status == "corrected" and GOOD in result.text and "UNSUPPORTED" not in result.text
    assert ask.await_count == 2 + int(prior_shape_recovery)
    locked = json.loads(ask.await_args_list[-1].args[0][1].content)
    assert [item["index"] for item in locked["failed_paragraphs"]] == [1]
    assert [item["index"] for item in locked["accepted_paragraphs"]] == [0]
    assert "draft" not in locked


@pytest.mark.asyncio
async def test_bad_scoped_repair_schema_cannot_restart_whole_answer_after_acceptance():
    semantic = response(paragraph(GOOD), paragraph("UNSUPPORTED", quote="bad citation"))
    result, ask = await run(semantic, shape_response("root_fields"), response(paragraph("UNAUTHORIZED_REWRITE")))
    assert result.status == "unavailable" and GOOD in result.text and ask.await_count == 3
    assert ask.await_args_list[1].args[0][1].content == ask.await_args_list[2].args[0][1].content
    assert result.metadata["citation_schema_repair_status"] == "unavailable"
    assert "UNAUTHORIZED_REWRITE" not in result.text
    assert "review_schema_repair_attempted" not in result.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError(PRIVATE), TimeoutError(PRIVATE)])
async def test_schema_recovery_provider_failure_remains_private_and_cannot_publish_original_unaccepted_text(failure):
    result, ask = await run(shape_response("paragraph_fields"), failure)
    assert result.status == "unavailable" and GOOD not in result.text and ask.await_count == 2
    assert result.missing_requirement_indices == ()
    assert_frozen_shape_recovery(ask)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [None, 1, "true"])
async def test_schema_recovery_requires_a_strict_boolean_coverage_judgement(complete):
    result, ask = await run(shape_response("root_fields"), covered_response(paragraph(GOOD), complete=complete))
    assert result.status == "unavailable" and GOOD not in result.text and ask.await_count == 2
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_schema_recovery_without_coverage_cannot_count_as_a_valid_protocol_response():
    result, ask = await run(shape_response("root_fields"), response(paragraph(GOOD)))
    assert result.status == "unavailable" and GOOD not in result.text and ask.await_count == 2
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_explicit_incomplete_schema_recovery_preserves_verified_text_but_cannot_finish_task():
    result, ask = await run(shape_response("root_fields"), covered_response(paragraph(GOOD), complete=False))
    assert result.status == "unavailable" and GOOD in result.text and ask.await_count == 2
    assert result.metadata["reason"] == "answer_coverage_unverified"
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_catalog_only_schema_recovery_does_not_turn_true_coverage_into_analytical_completion():
    evidence = observations()
    evidence.observe_context({"name": "Selected archive"})
    context = paragraph("The selected archive is registered.", kind="context", source="catalog_0001", quote="Selected archive")
    result, ask = await run(shape_response("root_fields"), covered_response(context), evidence=evidence)
    assert result.status == "unavailable" and context["text"] in result.text and ask.await_count == 2
    assert result.metadata["reason"] == "answer_coverage_unverified"
    assert result.missing_requirement_indices == ()
