"""Independent adversarial boundaries for read-only answer recovery.

All model responses are synthetic untrusted JSON. No model, tool or analysis
execution is used, and accepted evidence must stay unchanged.
"""
import copy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import historical_step, paragraph, response, tool


GOOD = "The already accepted mean is 3 mg."
HISTORY_FIRST = "Earlier measured mean was 3 mg. The earlier method was descriptive."
HISTORY_SECOND = "The earlier result did not establish a causal relationship."
OMITTED = object()


def evidence(*, history=None, historical_only=False):
    result = review.AnswerEvidence()
    result.begin_step("current")
    if history is not None:
        step = historical_step()
        step.result = history
        result.observe_reviewed_result(step)
    if not historical_only:
        result.observe(tool())
    return result


def corrected(text, *, kind="analysis", source="tool_0001_result"):
    return {"text": text, "kind": kind, "evidence": [source + ":excerpt_0001"]}


def correction(paragraphs=(), checks=(), *, complete=True):
    payload = {"paragraph_corrections": [{"index": index, "paragraph": value} for index, value in paragraphs],
               "requirement_corrections": [{"index": index, "check": value} for index, value in checks]}
    if complete is not OMITTED:
        payload["answer_complete"] = complete
    return AIMessage(content=json.dumps(payload))


async def run(initial, recovery, *, observed=None, requirements=(), language="en"):
    observed = observed if observed is not None else evidence()
    before = copy.deepcopy(observed.__dict__)
    ask = AsyncMock(side_effect=[initial, recovery])
    result = await review.review_answer(ask=ask, question="Explain the observed findings and their limitations",
        draft="Untrusted draft", files=[], evidence=observed, requirements=requirements, language=language)
    assert observed.__dict__ == before
    assert ask.await_count <= 2
    return result, ask


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_initial", [False, True])
async def test_single_oversized_paragraph_is_isolated_without_discarding_accepted_text(oversized_initial):
    oversized = "OVERSIZED " + "x" * review.MAX_ANSWER_CHARS
    bad = paragraph(oversized) if oversized_initial else paragraph("Rejected paragraph", quote="wrong citation")
    result, ask = await run(response(paragraph(GOOD), bad), correction([(1, corrected(oversized))]))
    assert result.status == "unavailable" and GOOD in result.text
    assert "OVERSIZED" not in result.text
    assert result.metadata["unresolved_citation_count"] == 1
    assert result.metadata["withheld_paragraph_count"] == 1
    assert result.missing_requirement_indices == () and ask.await_count == 2
    assert "answer_too_long" in result.metadata["citation_diagnostics"]["correction"]


@pytest.mark.asyncio
async def test_total_length_overflow_isolated_in_initial_response_can_be_shortened():
    first = "FIRST " + "a" * (review.MAX_ANSWER_CHARS // 2)
    second = "SECOND " + "b" * (review.MAX_ANSWER_CHARS // 2)
    result, ask = await run(response(paragraph(GOOD), paragraph(first), paragraph(second)),
                            correction([(2, corrected("Short supported explanation."))]))
    assert result.status == "corrected"
    assert GOOD in result.text and first in result.text and "Short supported explanation." in result.text
    assert second not in result.text
    payload = json.loads(ask.await_args_list[1].args[0][-1].content)
    assert [entry["index"] for entry in payload["failed_paragraphs"]] == [2]
    assert [entry["index"] for entry in payload["accepted_paragraphs"]] == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse_response_order", [False, True])
async def test_combined_corrections_cannot_overflow_or_erase_locked_paragraphs(reverse_response_order):
    first = "KEPT_CORRECTION " + "a" * (review.MAX_ANSWER_CHARS // 2)
    second = "WITHHELD_CORRECTION " + "b" * (review.MAX_ANSWER_CHARS // 2)
    fixes = [(1, corrected(first)), (2, corrected(second))]
    if reverse_response_order:
        fixes.reverse()
    initial = response(paragraph(GOOD), paragraph("Bad one", quote="not present"),
                       paragraph("Bad two", quote="not present"))
    result, _ = await run(initial, correction(fixes))
    assert result.status == "unavailable" and GOOD in result.text and first in result.text
    assert second not in result.text and result.metadata["withheld_paragraph_count"] == 1
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [review.MAX_ANSWER_CHARS - 2, review.MAX_ANSWER_CHARS - 1, review.MAX_ANSWER_CHARS])
async def test_exact_single_paragraph_limit_does_not_invent_a_separator_or_need_correction(length):
    text = "x" * length
    result, ask = await run(response(paragraph(text)), correction())
    assert result.status == "verified" and result.text == text
    assert ask.await_count == 1


@pytest.mark.asyncio
async def test_exact_aggregate_limit_counts_only_real_inter_paragraph_separators():
    text = "x" * (review.MAX_ANSWER_CHARS - len(GOOD) - 2)
    result, ask = await run(response(paragraph(GOOD), paragraph(text)), correction())
    assert result.status == "verified" and result.text == GOOD + "\n\n" + text
    assert ask.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("has_accepted_paragraph", [False, True])
async def test_corrected_text_may_use_the_exact_remaining_answer_length(has_accepted_paragraph):
    original = [paragraph(GOOD)] if has_accepted_paragraph else []
    original.append(paragraph("Bad citation", quote="not present"))
    remaining = review.MAX_ANSWER_CHARS - (len(GOOD) + 2 if has_accepted_paragraph else 0)
    text = "x" * remaining
    result, ask = await run(response(*original), correction([(len(original) - 1, corrected(text))]))
    assert result.status == "corrected"
    assert result.text == (GOOD + "\n\n" if has_accepted_paragraph else "") + text
    assert ask.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [OMITTED, False])
async def test_nonnull_replacement_cannot_claim_completion_without_explicit_coverage(complete):
    result, ask = await run(response(paragraph(GOOD), paragraph("Rejected", quote="wrong citation")),
        correction([(1, corrected("Supported but not necessarily complete."))], complete=complete))
    assert result.status == "unavailable" and result.metadata["reason"] == "answer_coverage_unverified"
    assert GOOD in result.text and "Supported but not necessarily complete." in result.text
    assert result.missing_requirement_indices == () and ask.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("language,prefix", [("zh", "此前已核验的结果说明："), ("en", "The previously verified result stated: ")])
@pytest.mark.parametrize("surrounding_whitespace", [False, True])
async def test_cross_kind_history_recovery_reproduces_a_complete_paragraph_with_host_attribution(language, prefix, surrounding_whitespace):
    observed = evidence(history=HISTORY_FIRST + "\n\n" + HISTORY_SECOND, historical_only=True)
    good = paragraph("The earlier mean was 3 mg.", source="prior_review_0001", quote=HISTORY_FIRST)
    bad = paragraph("A historical qualification", kind="limitation", source="prior_review_0001", quote=HISTORY_SECOND)
    text = "  " + HISTORY_SECOND + "\n" if surrounding_whitespace else HISTORY_SECOND
    result, ask = await run(response(good, bad), correction([(1, corrected(text, source="prior_review_0001"))]),
                            observed=observed, language=language)
    assert result.status == "corrected" and prefix + HISTORY_SECOND in result.text
    assert good["text"] in result.text and result.missing_requirement_indices == ()
    assert ask.await_count == 2 and result.metadata["file_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_text", [
    "I just reran the analysis and newly measured a mean of 999 mg.",
    "The previous result was descriptive.",
    "Earlier measured mean was 3 mg.",
    HISTORY_FIRST + "\n\n" + HISTORY_SECOND,
])
async def test_cross_kind_history_recovery_rejects_new_claims_paraphrases_fragments_and_merged_paragraphs(candidate_text):
    observed = evidence(history=HISTORY_FIRST + "\n\n" + HISTORY_SECOND, historical_only=True)
    good = paragraph("The earlier mean was 3 mg.", source="prior_review_0001", quote=HISTORY_FIRST)
    bad = paragraph("A historical qualification", kind="limitation", source="prior_review_0001", quote=HISTORY_SECOND)
    result, _ = await run(response(good, bad), correction([(1, corrected(candidate_text, source="prior_review_0001"))]), observed=observed)
    assert result.status == "unavailable" and good["text"] in result.text
    assert candidate_text not in result.text
    assert result.missing_requirement_indices == () and result.metadata["withheld_paragraph_count"] == 1


@pytest.mark.asyncio
async def test_truncated_historical_source_cannot_be_used_for_the_cross_kind_exception():
    observed = evidence(history=HISTORY_FIRST + "\n\n" + "x" * review.MAX_SOURCE_CHARS, historical_only=True)
    good = paragraph("The earlier mean was 3 mg.", source="prior_review_0001", quote=HISTORY_FIRST)
    bad = paragraph("A historical qualification", kind="limitation", source="prior_review_0001", quote=HISTORY_FIRST)
    result, _ = await run(response(good, bad), correction([(1, corrected(HISTORY_FIRST, source="prior_review_0001"))]), observed=observed)
    assert result.status == "unavailable" and good["text"] in result.text
    assert result.metadata["evidence_truncated"] is True
    assert "previously verified result stated" not in result.text
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("original_status", ["met", "unclear"])
async def test_positive_or_unclear_check_recovery_cannot_create_negative_execution_evidence(original_status):
    check = {"index": 0, "status": original_status,
             "evidence": [] if original_status == "unclear" else [{"source_id": "tool_0001_result", "quote": "bad citation"}]}
    replacement = {"index": 0, "status": "confirmed_not_performed", "evidence": ["tool_0001_result:excerpt_0001"]}
    result, ask = await run(response(paragraph(GOOD), checks=[check]), correction(checks=[(0, replacement)]),
                            requirements=[DeliverableRequirement(kind="table", objective="Compute the mean")])
    assert result.status == "unavailable" and GOOD in result.text
    assert result.missing_requirement_indices == (), "A citation repair must never authorize analytical replay"
    assert result.metadata["citation_diagnostics"]["unresolved_requirement_indices"] == [0]
    assert ask.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("original_status", ["met", "unclear"])
async def test_positive_proof_can_still_recover_an_existing_objective_without_reexecution(original_status):
    check = {"index": 0, "status": original_status,
             "evidence": [] if original_status == "unclear" else [{"source_id": "tool_0001_result", "quote": "bad citation"}]}
    replacement = {"index": 0, "status": "met", "evidence": ["tool_0001_result:excerpt_0001"]}
    result, ask = await run(response(paragraph(GOOD), checks=[check]), correction(checks=[(0, replacement)]),
                            requirements=[DeliverableRequirement(kind="table", objective="Compute the mean")])
    assert result.status == "corrected" and result.text == GOOD
    assert result.missing_requirement_indices == () and ask.await_count == 2
