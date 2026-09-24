"""Frozen-evidence citation recovery separates protocol failure from rejection."""
import asyncio
import json
from unittest.mock import AsyncMock

from langchain.messages import AIMessage
import pytest

from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import check, paragraph, requirement, response, tool


def correction(*entries, checks=(), complete=True):
    return AIMessage(content=json.dumps({"answer_complete": complete,
        "paragraph_corrections": list(entries), "requirement_corrections": list(checks)}))


def corrected(index=1, *, kind="analysis", source="tool_0001_result"):
    return {"index": index, "paragraph": {"text": "The measured mean equals 3 mg.", "kind": kind,
        "evidence": [source + ":excerpt_0001"]}}


async def run_review(first, *repairs, requirements=(), language="zh"):
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    ask = AsyncMock(side_effect=[first, *repairs])
    result = await review.review_answer(ask=ask, question="Explain the observed mean", draft="PRIVATE_DRAFT",
        files=[], evidence=evidence, requirements=requirements, language=language)
    return result, ask


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed,code", [
    (AIMessage(content='{"PRIVATE_PROVIDER_BODY":"/Users/private/data.csv"}'), "correction_schema_root"),
    (correction(corrected(index=0)), "correction_schema_indices"),
    (correction(corrected(), corrected()), "correction_schema_indices"),
])
async def test_one_protocol_retry_uses_identical_evidence_and_exact_host_indices(malformed, code):
    result, ask = await run_review(response(paragraph(), paragraph("BAD_DRAFT", quote="not observed")),
        malformed, correction(corrected()))
    assert result.status == "corrected" and ask.await_count == 3
    assert result.text.startswith("Observed mean is 3 mg.")
    assert "The measured mean equals 3 mg." in result.text
    assert result.metadata["citation_schema_error"] == code
    assert result.metadata["citation_schema_repair_status"] == "corrected"
    assert result.metadata["citation_validation_state"] == "verified"
    second, third = [call.args[0] for call in ask.await_args_list[1:]]
    assert second[-1].content == third[-1].content
    assert "original integer indices, each once: [1]" in third[0].content
    assert "integer indices, each once: []" in third[0].content
    assert "PRIVATE_PROVIDER_BODY" not in third[0].content
    assert "/Users/private/" not in third[0].content
    assert "PRIVATE_" not in json.dumps(result.metadata) + result.text
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    TimeoutError(), RuntimeError("PRIVATE_PROVIDER_ERROR"), AIMessage(content="not json PRIVATE_BODY"),
    correction({"index": 1, "paragraph": {"text": "INVALID_SCHEMA"}}),
    correction(corrected(source="missing_source")),
    correction(corrected(kind="context")),
])
async def test_technical_failure_preserves_valid_paragraph_without_false_rejection(failure):
    result, ask = await run_review(response(paragraph(), paragraph("MUST_NOT_PUBLISH", quote="not observed")), failure)
    assert ask.await_count == 2  # No semantic, malformed-JSON, or transport retry here.
    assert result.status == "unavailable"
    assert result.metadata["validation_state"] == "unavailable"
    assert result.metadata["citation_validation_state"] == "unavailable"
    assert result.metadata["citation_diagnostics"]["unresolved_failure_classes"] == {"technical": 1}
    assert result.text.startswith("Observed mean is 3 mg.")
    assert "审核过程未完成，不代表相关计算或结论已被判错" in result.text
    assert "MUST_NOT_PUBLISH" not in result.text and "PRIVATE_" not in result.text + json.dumps(result.metadata)
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_repeated_protocol_failure_is_bounded_and_does_not_rewrite_accepted_text():
    wrong = correction(corrected(index=0))
    result, ask = await run_review(response(paragraph(), paragraph("BAD", quote="absent")), wrong, wrong)
    assert ask.await_count == 3
    assert result.status == "unavailable" and result.metadata["validation_state"] == "unavailable"
    assert result.metadata["citation_schema_repair_status"] == "unavailable"
    assert result.metadata["citation_diagnostics"]["correction"] == {"correction_schema_indices": 1}
    assert "The measured mean equals" not in result.text
    assert result.text.startswith("Observed mean is 3 mg.")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), AIMessage(content="malformed")])
async def test_empty_publication_fallback_keeps_unavailable_state(failure):
    result, ask = await run_review(response(paragraph("UNVERIFIED", quote="absent")), failure)
    assert ask.await_count == 2 and result.status == "unavailable"
    assert result.metadata["validation_state"] == "unavailable"
    assert "暂无法完成证据核验" in result.text
    assert "不表示已有计算结果或文件被判错误" in result.text
    assert "UNVERIFIED" not in result.text
    assert result.metadata["citation_diagnostics"]["paragraph_publication"]["published"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("language", ["zh", "en"])
async def test_explicit_withdrawal_of_every_paragraph_is_missing_answer_not_rejection(complete, language):
    result, ask = await run_review(response(paragraph("UNVERIFIED_ONLY", quote="absent")),
        correction({"index": 0, "paragraph": None}, complete=complete), language=language)
    assert ask.await_count == 2
    assert result.status == "unavailable" and result.metadata["validation_state"] == "unavailable"
    assert result.metadata["reason"] == "answer_coverage_unverified"
    assert result.metadata["answer_coverage_unverified"] is True
    assert result.metadata["unresolved_citation_count"] == 0
    assert result.metadata["withheld_paragraph_count"] == 1
    assert "UNVERIFIED_ONLY" not in result.text
    assert ("尚未得到完整回答" if language == "zh" else "has not been fully answered") in result.text
    assert result.metadata["citation_diagnostics"]["paragraph_publication"]["published"] == []
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_well_shaped_wrong_evidence_kind_still_rejects_claim_without_retry():
    result, ask = await run_review(response(paragraph(), paragraph("BAD", quote="absent")),
        correction(corrected(source="verified_files")))
    assert ask.await_count == 2
    assert result.status == "unavailable" and result.metadata["validation_state"] == "rejected"
    assert result.metadata["citation_diagnostics"]["unresolved_failure_classes"] == {"evidence": 1}
    assert "The measured mean equals" not in result.text


@pytest.mark.asyncio
async def test_mixed_unresolved_claims_do_not_hide_incomplete_review():
    result, ask = await run_review(response(paragraph(), paragraph("BAD1", quote="absent"),
        paragraph("BAD2", quote="absent")), correction(corrected(source="verified_files"),
            corrected(index=2, source="missing_source")))
    assert ask.await_count == 2 and result.metadata["validation_state"] == "unavailable"
    assert result.metadata["citation_diagnostics"]["unresolved_failure_classes"] == {"evidence": 1, "technical": 1}
    assert "The measured mean equals" not in result.text


@pytest.mark.asyncio
async def test_requirement_index_repair_cannot_create_replay_permission():
    first = response(paragraph(), checks=[check("met", quote="absent")])
    wrong_index = correction(checks=[{"index": 1, "check": check("met")}])
    forbidden = correction(checks=[{"index": 0, "check": {
        "index": 0, "status": "confirmed_not_performed", "evidence": ["tool_0001_result:excerpt_0001"]}}])
    result, ask = await run_review(first, wrong_index, forbidden, requirements=[requirement()])
    assert ask.await_count == 3
    assert "integer indices, each once: [0]" in ask.await_args_list[2].args[0][0].content
    assert result.metadata["validation_state"] == "unavailable"
    assert result.missing_requirement_indices == ()
    assert result.text.startswith("Observed mean is 3 mg.")


@pytest.mark.asyncio
async def test_protocol_retry_cancellation_propagates_without_publication():
    with pytest.raises(asyncio.CancelledError):
        await run_review(response(paragraph("BAD", quote="absent")), AIMessage(content="{}"), asyncio.CancelledError())
