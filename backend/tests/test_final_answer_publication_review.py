"""Publication changes are re-reviewed without running analytical tools again."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import file, paragraph, tool
from test_text_scientific_review_integration import checked, grounded


def final_check(*, science="verified", scope="complete", count=1):
    value = checked(science=science, scope=scope)
    for item in value["answer_scientific_checks"]:
        item["paragraph_indices"] = list(range(count))
    value["answer_scope_check"]["paragraph_indices"] = list(range(count))
    return {key: value[key] for key in ("answer_scientific_checks", "answer_scope_check")}


def correction(text="The observed mean is 3 mg."):
    return {"answer_complete": True, "paragraph_corrections": [
        {"index": 0, "paragraph": {"text": text, "kind": "analysis",
                                   "evidence": ["tool_0001_result:excerpt_0001"]}}],
        "requirement_corrections": []}


async def run(*answers, files=(), question="Describe the observed data"):
    evidence = review.AnswerEvidence()
    evidence.begin_step("current")
    evidence.observe(tool())
    original = copy.deepcopy(evidence.__dict__)
    ask = AsyncMock(side_effect=[value if isinstance(value, BaseException) else json.dumps(
        grounded(value) if index == 0 else value) for index, value in enumerate(answers)])
    result = await review.review_answer(ask=ask, question=question, draft="Observed mean is 3 mg.",
        files=files, evidence=evidence, language="en", answer_scientific_scope=True)
    assert evidence.__dict__ == original
    return result, ask


def invalid_quote():
    value = checked()
    value["paragraphs"][0]["evidence"][0]["quote"] = "A quote that is not in the source"
    return value


@pytest.mark.asyncio
async def test_corrected_text_is_verified_against_frozen_final_candidate():
    result, ask = await run(invalid_quote(), correction(), final_check())
    assert ask.await_count == 3 and result.status == "corrected"
    assert result.metadata["answer_scientific_review"]["status"] == "verified"
    assert result.metadata["answer_scope_review"]["status"] == "verified"
    assert result.metadata["final_candidate_review"]["status"] == "verified"
    initial = json.loads(ask.await_args_list[0].args[0][-1].content)
    final = json.loads(ask.await_args_list[-1].args[0][-1].content)
    assert final["frozen_paragraphs"] == [result.text] == ["The observed mean is 3 mg."]
    assert final["sources"] == initial["sources"]
    assert final["request"] == initial["question"]
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_safe_path_redaction_is_reviewed_as_the_actual_public_text():
    value = checked(text="Observed mean is 3 mg in /home/ubuntu/output/observed.png.")
    result, ask = await run(value, final_check(), files=[file()])
    assert result.status == "verified" and ask.await_count == 2
    assert "/home/" not in result.text and "observed.png" in result.text
    payload = json.loads(ask.await_args_list[-1].args[0][-1].content)
    assert payload["frozen_paragraphs"] == [result.text]


@pytest.mark.asyncio
async def test_current_request_attribution_does_not_permanently_invalidate_science():
    question = "Describe the observed data"
    value = checked()
    value["paragraphs"].append(paragraph(question, kind="context", source="current_request", quote=question))
    for item in value["answer_scientific_checks"]:
        item["paragraph_indices"] = [0, 1]
    value["answer_scope_check"]["paragraph_indices"] = [0, 1]
    result, ask = await run(value, final_check(count=2), question=question)
    assert result.status == "verified" and ask.await_count == 2
    assert "Current user request: " + question in result.text
    assert json.loads(ask.await_args_list[-1].args[0][-1].content)["frozen_paragraphs"] == result.text.split("\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("science,scope,reason", [
    ("rejected", "complete", "scientific_validation_rejected"),
    ("unclear", "complete", "scientific_validation_unavailable"),
    ("verified", "incomplete", "answer_coverage_incomplete"),
])
async def test_final_reviewer_can_reject_or_withhold_without_more_retries(science, scope, reason):
    result, ask = await run(invalid_quote(), correction(), final_check(science=science, scope=scope))
    assert ask.await_count == 3 and result.status == "unavailable"
    assert result.metadata["reason"] == reason
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_final_protocol_error_has_one_same_payload_recovery():
    malformed = final_check()
    malformed["answer_scientific_checks"][0]["paragraph_indices"] = [99]
    result, ask = await run(invalid_quote(), correction(), malformed, final_check())
    assert ask.await_count == 4 and result.status == "corrected"
    third, fourth = [call.args[0] for call in ask.await_args_list[-2:]]
    assert third[-1] is fourth[-1]
    assert "review_answer_science_indices" in fourth[0].content
    assert result.metadata["final_candidate_review"]["schema_recovered"] is True


@pytest.mark.asyncio
async def test_final_reviewer_cannot_replace_text_or_inherit_previous_green_on_failure():
    malicious = {**final_check(), "paragraphs": ["SECRET replacement unsupported conclusion"]}
    result, ask = await run(invalid_quote(), correction(), malicious, malicious)
    assert ask.await_count == 4 and result.status == "unavailable"
    assert result.metadata["validation_state"] == "unavailable"
    assert result.metadata["answer_scientific_review"]["reason"] == "final_answer_review_unavailable"
    assert "SECRET" not in result.text + json.dumps(result.metadata)
    assert result.metadata["final_candidate_review"]["error"] == "final_review_schema_root"


@pytest.mark.asyncio
async def test_final_review_transport_failure_is_not_scientific_rejection():
    result, ask = await run(invalid_quote(), correction(), TimeoutError("SECRET provider body"))
    assert ask.await_count == 3 and result.status == "unavailable"
    assert result.metadata["validation_state"] == "unavailable"
    assert result.metadata["final_candidate_review"]["error"] == "transport_timeout"
    assert "SECRET" not in result.text + json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_final_review_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await run(invalid_quote(), correction(), asyncio.CancelledError())


@pytest.mark.asyncio
async def test_final_review_tool_request_is_rejected_without_schema_retry():
    evidence = review.AnswerEvidence()
    evidence.begin_step("current")
    evidence.observe(tool())
    asked_tool = AIMessage(content="", tool_calls=[{
        "id": "forbidden", "name": "shell_run", "args": {"command": "SECRET unauthorized command"}}])
    ask = AsyncMock(side_effect=[json.dumps(grounded(invalid_quote())), json.dumps(correction()), asked_tool])
    result = await review.review_answer(ask=ask, question="Describe the observed data", draft="Observed mean is 3 mg.",
        files=[], evidence=evidence, language="en", answer_scientific_scope=True)
    assert ask.await_count == 3 and result.status == "unavailable"
    assert result.metadata["final_candidate_review"]["error"] == "review_requested_tools"
    assert "SECRET" not in result.text + json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_unchanged_candidate_still_requires_independent_review():
    result, ask = await run(checked(), final_check())
    assert result.status == "verified" and ask.await_count == 2
    assert result.metadata["final_candidate_review"]["status"] == "verified"
