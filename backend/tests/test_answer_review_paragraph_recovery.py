"""Semantic review failures are isolated without inventing completion or work."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import file, historical_step, paragraph, response, tool


GOOD = "The observed mean is 3 mg."
HISTORY = "The earlier analysis measured a mean of 3 mg."
PRIVATE = "/Users/private/unverified-secret.csv"
ABSENT = object()


def evidence_set(*, historical_only=False):
    evidence = review.AnswerEvidence()
    evidence.begin_step("current")
    evidence.observe_context({"name": "Observatory", "description": "Registered observation archive"})
    evidence.observe_reviewed_result(historical_step())
    if not historical_only:
        evidence.observe(tool())
        evidence.observe(tool(call="failed-inspection", success=False, data={"message": "Optional inspection failed"}))
    return evidence


def candidate(text, kind, source):
    sources = [source] if isinstance(source, str) else source
    return {"text": text, "kind": kind, "evidence": [item + ":excerpt_0001" for item in sources]}


def repair(value, *, index=1, complete=True):
    payload = {"paragraph_corrections": [{"index": index, "paragraph": value}], "requirement_corrections": []}
    if complete is not ABSENT:
        payload["answer_complete"] = complete
    return AIMessage(content=json.dumps(payload))


async def call_review(bad, corrected, *, good=None, evidence=None, files=None, requirements=(), checks=()):
    evidence = evidence_set() if evidence is None else evidence
    before = copy.deepcopy(evidence.__dict__)
    ask = AsyncMock(side_effect=[response(good or paragraph(GOOD), bad, checks=list(checks)), corrected, corrected])
    result = await review.review_answer(ask=ask, question="Describe the observed result", draft="UNVERIFIED_DRAFT",
        files=[file()] if files is None else files, evidence=evidence, requirements=requirements)
    assert evidence.__dict__ == before
    return result, ask


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,wrong_source,wrong_quote,text,correct_source", [
    ("analysis", "verified_files", "observed.png", "The observed mean is 3 mg.", "tool_0001_result"),
    ("delivery", "tool_0001_result", "mean=3", "Saved `observed.png`.", "verified_files"),
    ("limitation", "catalog_0001", "Observatory", "An optional inspection failed.", "tool_0002_result"),
    ("context", "tool_0001_result", "mean=3", "The selected catalog is named Observatory.", "catalog_0001"),
])
async def test_wrong_source_kind_is_repaired_once_without_rewriting_accepted_paragraph(
        kind, wrong_source, wrong_quote, text, correct_source):
    bad = paragraph("UNSUPPORTED_OPTIONAL_CLAIM", kind=kind, source=wrong_source, quote=wrong_quote)
    result, ask = await call_review(bad, repair(candidate(text, kind, correct_source)))
    assert result.status == "corrected" and result.text.startswith(GOOD) and text in result.text
    assert "UNSUPPORTED_OPTIONAL_CLAIM" not in result.text and ask.await_count == 2
    first = json.loads(ask.await_args_list[0].args[0][-1].content)
    recovery = json.loads(ask.await_args_list[1].args[0][-1].content)
    assert [item["index"] for item in recovery["failed_paragraphs"]] == [1]
    assert GOOD in json.dumps(recovery["accepted_paragraphs"])
    assert "UNVERIFIED_DRAFT" not in json.dumps(recovery)
    original = {item["source_id"]: item["text"] for item in first["sources"]}
    assert all("".join(part["text"] for part in source["excerpts"]) == original[source["source_id"]]
               for source in recovery["sources"])
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_unverified_file_reference_can_be_corrected_only_to_exact_verified_identity():
    bad = paragraph(f"Saved `{PRIVATE}`.", kind="delivery", source="verified_files", quote="observed.png")
    result, ask = await call_review(bad, repair(candidate(
        "Saved `/home/ubuntu/output/observed.png`.", "delivery", "verified_files")))
    assert result.status == "corrected" and "`observed.png`" in result.text and ask.await_count == 2
    assert "/home/" not in result.text and "/Users/" not in result.text
    assert PRIVATE not in json.dumps(result.metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, ABSENT])
async def test_withdrawal_without_explicit_answer_coverage_keeps_valid_text_but_not_success(complete):
    bad = paragraph("The requested second statistic is 999.", kind="analysis", source="verified_files", quote="observed.png")
    result, ask = await call_review(bad, repair(None, complete=complete))
    assert result.status == "unavailable" and result.metadata["reason"] == "answer_coverage_unverified"
    assert GOOD in result.text and "999" not in result.text and ask.await_count == 2
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_explicitly_withdrawing_optional_hallucination_requires_supported_remaining_answer():
    bad = paragraph(f"Saved `{PRIVATE}`.", kind="delivery", source="verified_files", quote="observed.png")
    result, ask = await call_review(bad, repair(None, complete=True))
    assert result.status == "corrected" and result.text == GOOD and ask.await_count == 2
    assert result.metadata["withheld_paragraph_count"] == 1
    assert PRIVATE not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_catalog_only_remainder_cannot_claim_complete_when_core_analysis_was_withdrawn():
    good = paragraph("The selected catalog is Observatory.", kind="context", source="catalog_0001", quote="Observatory")
    bad = paragraph("The requested mean is 999.", kind="analysis", source="catalog_0001", quote="Observatory")
    result, ask = await call_review(bad, repair(None, complete=True), good=good,
                                   evidence=evidence_set(historical_only=True), files=[])
    assert result.status == "unavailable" and result.metadata["reason"] == "answer_coverage_unverified"
    assert "Observatory" in result.text and "999" not in result.text and ask.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [1, "true", None])
async def test_withdrawal_coverage_must_be_an_explicit_boolean_not_truthy_data(complete):
    bad = paragraph("UNSUPPORTED", kind="analysis", source="verified_files", quote="observed.png")
    result, ask = await call_review(bad, repair(None, complete=complete))
    assert result.status == "unavailable" and GOOD in result.text and ask.await_count == 3
    assert result.metadata["validation_state"] == "unavailable"
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_explicit_incomplete_coverage_is_honored_even_without_withdrawing_a_paragraph():
    bad = paragraph("UNSUPPORTED", kind="context", source="tool_0001_result", quote="mean=3")
    result, ask = await call_review(bad, repair(candidate(
        "The selected catalog is Observatory.", "context", "catalog_0001"), complete=False))
    assert result.status == "unavailable" and result.metadata["reason"] == "answer_coverage_unverified"
    assert GOOD in result.text and "Observatory" in result.text and ask.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("has_files", [False, True])
async def test_delivery_only_remainder_needs_real_current_inventory_before_coverage_can_pass(has_files):
    good = paragraph("Saved `observed.png`." if has_files else "The current delivery inventory is empty.",
        kind="delivery", source="verified_files", quote="observed.png" if has_files else "[]")
    bad = paragraph("UNSUPPORTED", kind="analysis", source="catalog_0001", quote="Observatory")
    result, ask = await call_review(bad, repair(None, complete=True), good=good,
        evidence=evidence_set(historical_only=True), files=[file()] if has_files else [])
    assert result.status == ("corrected" if has_files else "unavailable")
    assert ask.await_count == 2 and result.metadata["file_count"] == int(has_files)
    if not has_files:
        assert result.metadata["reason"] == "answer_coverage_unverified"


@pytest.mark.asyncio
async def test_historical_qualification_may_change_from_limitation_to_attributed_analysis():
    evidence = evidence_set(historical_only=True)
    good = paragraph(HISTORY, source="prior_review_0001", quote="Earlier measured mean was 3 mg.")
    bad = paragraph("The earlier result reported a mean of 3 mg.", kind="limitation",
                    source="prior_review_0001", quote="Earlier measured mean was 3 mg.")
    corrected = "Earlier measured mean was 3 mg."
    result, ask = await call_review(bad, repair(candidate(corrected, "analysis", "prior_review_0001")),
        good=good, evidence=evidence, files=[])
    assert result.status == "corrected" and HISTORY in result.text and corrected in result.text
    assert "此前已核验的结果说明：" in result.text
    assert ask.await_count == 2 and result.metadata["file_count"] == 0
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_kind,old_source,new_kind,new_sources", [
    ("analysis", "verified_files", "context", "catalog_0001"),
    ("delivery", "tool_0001_result", "analysis", "tool_0001_result"),
    ("context", "tool_0001_result", "analysis", "tool_0001_result"),
    ("limitation", "catalog_0001", "analysis", "tool_0001_result"),
    ("limitation", "catalog_0001", "analysis", "prior_review_0001"),
    ("limitation", "prior_review_0001", "analysis", "tool_0001_result"),
    ("limitation", "prior_review_0001", "analysis", ["prior_review_0001", "tool_0001_result"]),
])
async def test_kind_changes_cannot_launder_claims_or_mix_new_execution_into_history(old_kind, old_source, new_kind, new_sources):
    evidence = evidence_set()
    source_text = {item["source_id"]: item["text"] for item in evidence.render_sources()}
    quote = "observed.png" if old_source == "verified_files" else source_text[old_source]
    bad = paragraph("UNSUPPORTED_CORE_CLAIM", kind=old_kind, source=old_source, quote=quote)
    result, ask = await call_review(bad, repair(candidate("MUST_NOT_PUBLISH", new_kind, new_sources)), evidence=evidence)
    assert result.status == "unavailable" and GOOD in result.text and ask.await_count == 2
    assert "MUST_NOT_PUBLISH" not in result.text and "UNSUPPORTED_CORE_CLAIM" not in result.text
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_historical_repair_cannot_complete_a_new_analytical_objective():
    good = paragraph(HISTORY, source="prior_review_0001", quote="Earlier measured mean was 3 mg.")
    bad = paragraph("The earlier report described a mean of 3 mg.", kind="limitation",
                    source="prior_review_0001", quote="Earlier measured mean was 3 mg.")
    result, ask = await call_review(bad, repair(candidate("Earlier measured mean was 3 mg.", "analysis", "prior_review_0001")),
        good=good, evidence=evidence_set(historical_only=True), files=[],
        requirements=[DeliverableRequirement(kind="table", objective="Recompute the mean for the current inputs")],
        checks=[{"index": 0, "status": "unclear", "evidence": []}])
    assert result.status == "unavailable" and result.metadata["reason"] == "requirements_unverified"
    assert result.missing_requirement_indices == () and ask.await_count == 2
    assert HISTORY in result.text


@pytest.mark.asyncio
async def test_historical_output_is_not_current_delivery_even_when_its_basename_is_known():
    good = paragraph(HISTORY, source="prior_review_0001", quote="Earlier measured mean was 3 mg.")
    bad = paragraph("Delivered `earlier.png` now.", kind="delivery", source="prior_review_0001",
                    quote="Earlier measured mean was 3 mg.")
    result, ask = await call_review(bad, repair(candidate("Delivered `earlier.png` now.", "delivery", "prior_review_0001")),
        good=good, evidence=evidence_set(historical_only=True), files=[])
    assert result.status == "unavailable" and HISTORY in result.text and "Delivered" not in result.text
    assert ask.await_count == 2 and result.metadata["file_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "timeout", "tool", "unknown_excerpt", "private_path", "wrong_index", "malformed"])
async def test_failed_semantic_repair_retains_locked_text_without_false_completion_or_private_leaks(failure):
    bad = paragraph(f"Saved `{PRIVATE}`.", kind="delivery", source="verified_files", quote="observed.png")
    second = repair(candidate("Saved `observed.png`.", "delivery", "verified_files"))
    if failure == "provider": second = RuntimeError("PRIVATE_PROVIDER_BODY " + PRIVATE)
    elif failure == "timeout": second = TimeoutError()
    elif failure == "tool": second = AIMessage(content="", tool_calls=[{"id": "forbidden", "name": "shell_run", "args": {}}])
    elif failure == "unknown_excerpt": second = repair(candidate("MUST_NOT_PUBLISH", "delivery", "invented_source"))
    elif failure == "private_path": second = repair(candidate(f"Saved `{PRIVATE}`.", "delivery", "verified_files"))
    elif failure == "wrong_index": second = repair(candidate("MUST_NOT_REWRITE", "analysis", "tool_0001_result"), index=0)
    elif failure == "malformed": second = AIMessage(content="not JSON " + PRIVATE)
    result, ask = await call_review(bad, second)
    assert result.status == "unavailable" and GOOD in result.text
    assert ask.await_count == (3 if failure == "wrong_index" else 2)
    assert result.missing_requirement_indices == ()
    public = result.text + json.dumps(result.metadata)
    assert all(value not in public for value in ("PRIVATE_PROVIDER_BODY", "/Users/", "MUST_NOT_PUBLISH", "MUST_NOT_REWRITE"))


@pytest.mark.asyncio
async def test_semantic_repair_cancellation_propagates_without_publishing_fallback():
    bad = paragraph("Optional inspection was executed.", kind="limitation", source="catalog_0001", quote="Observatory")
    with pytest.raises(asyncio.CancelledError):
        await call_review(bad, asyncio.CancelledError())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh", "en"])
async def test_no_new_files_fallback_does_not_deny_previously_delivered_files(language):
    ask = AsyncMock(side_effect=TimeoutError())
    result = await review.review_answer(ask=ask, question="Explain the earlier result", draft="", files=[],
                                       evidence=evidence_set(historical_only=True), language=language)
    assert result.status == "unavailable" and result.metadata["file_count"] == 0
    assert "目前没有可确认交付的文件" not in result.text
    assert "No delivered files can currently be confirmed" not in result.text
    assert "/home/" not in result.text and ask.await_count == 1
