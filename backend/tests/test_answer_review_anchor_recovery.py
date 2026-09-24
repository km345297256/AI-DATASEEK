"""Scoped anchor hints explain existing gates; they never grant evidence authority."""
import copy
import json
from unittest.mock import AsyncMock

from langchain.messages import AIMessage
import pytest

from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import file, historical_step, paragraph, response, tool
from test_answer_review_paragraph_recovery import candidate, repair


GOOD = "The observed mean is 3 mg."
PRIVATE = "PRIVATE_ANCHOR_SOURCE_BODY"
PRIVATE_PATH = "/Users/private/anchor-secret.csv"
HISTORY = "Earlier measured mean was 3 mg."


def observations():
    evidence = review.AnswerEvidence()
    evidence.begin_step("current")
    evidence.observe_context({"name": "Observatory", "private_note": PRIVATE})
    evidence.observe_reviewed_result(historical_step())
    evidence.observe(tool(data={"stdout": "mean=3; unit=mg", "private_note": PRIVATE}))
    evidence.observe(tool("file_write", call="saved-only",
        args={"file": "/home/ubuntu/output/analysis.py", "content": "print('mean=999')"},
        data={"message": "saved only " + PRIVATE}))
    evidence.observe(tool(call="failed", success=False, data={"message": "inspection failed " + PRIVATE}))
    evidence.observe(tool(call="pending", calling=True))
    unknown = tool(call="unknown")
    unknown.function_result = None
    evidence.observe(unknown)
    evidence.observe(tool("file_read", call="read", args={"file": "/home/ubuntu/inputs/source.csv"},
        data={"content": "sample,concentration\na,3"}))
    evidence.observe(tool("program_run", call="executed", data={"returncode": 0, "stdout": "mean=3"}))
    return evidence


def assert_private_metadata(result, payload):
    public = json.dumps(result.metadata)
    assert "eligible_anchor_source_ids" not in public
    assert all(source["source_id"] not in public for source in payload["sources"])
    assert PRIVATE not in public and PRIVATE_PATH not in public
    assert "/Users/" not in public and "/home/" not in public
    assert "print('mean=999')" not in public


async def run_recovery(bad, corrected, *, evidence=None, good=None, files=None):
    evidence = observations() if evidence is None else evidence
    before = copy.deepcopy(evidence.__dict__)
    ask = AsyncMock(side_effect=[response(good or paragraph(GOOD), bad), corrected])
    result = await review.review_answer(ask=ask, question="Explain the observed result", draft="UNVERIFIED_DRAFT",
        evidence=evidence, files=[file()] if files is None else files)
    assert ask.await_count == 2 and evidence.__dict__ == before
    first = json.loads(ask.await_args_list[0].args[0][-1].content)
    scoped = json.loads(ask.await_args_list[1].args[0][-1].content)
    assert len(scoped["failed_paragraphs"]) == 1
    assert scoped["failed_paragraphs"][0]["index"] == 1
    assert scoped["accepted_paragraphs"] == [{"index": 0, "paragraph": good or paragraph(GOOD)}]
    assert "draft" not in scoped and "UNVERIFIED_DRAFT" not in json.dumps(scoped)
    originals = {item["source_id"]: item["text"] for item in first["sources"]}
    assert all("".join(excerpt["text"] for excerpt in source["excerpts"]) == originals[source["source_id"]]
               for source in scoped["sources"])
    assert_private_metadata(result, first)
    assert result.missing_requirement_indices == ()
    return result, scoped, ask


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,source,text,expected", [
    ("analysis", "tool_0001_result", "The observed mean is 3 mg.",
     ["tool_0001_result", "tool_0006_result", "tool_0007_result", "prior_review_0001"]),
    ("analysis", "prior_review_0001", "The earlier result measured a mean of 3 mg.",
     ["tool_0001_result", "tool_0006_result", "tool_0007_result", "prior_review_0001"]),
    ("delivery", "verified_files", "Delivered `observed.png`.", ["verified_files"]),
    ("limitation", "tool_0003_result", "An optional inspection failed.",
     [f"tool_{index:04d}_result" for index in range(1, 8)]),
    ("context", "catalog_0001", "The registered catalog is Observatory.", ["catalog_0001", "current_request"]),
])
async def test_scoped_payload_lists_only_original_kind_gate_anchors(kind, source, text, expected):
    bad = paragraph("UNSUPPORTED_CLAIM", kind=kind, source="invented_source", quote=PRIVATE)
    result, scoped, ask = await run_recovery(bad, repair(candidate(text, kind, source)))
    assert scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"] == expected
    assert result.status == "corrected" and result.text.startswith(GOOD) and text in result.text
    assert "UNSUPPORTED_CLAIM" not in result.text
    system = ask.await_args_list[1].args[0][0].content
    assert "eligible_anchor_source_ids" in system and "untrusted DATA" in system


@pytest.mark.asyncio
@pytest.mark.parametrize("index,state", [(1, "succeeded"), (2, "succeeded"), (3, "failed"),
                                         (4, "pending"), (5, "unknown")])
async def test_limitation_anchor_is_observed_tool_result_in_any_state_not_only_success(index, state):
    source = f"tool_{index:04d}_result"
    bad = paragraph("UNSUPPORTED_LIMITATION", kind="limitation", source="invented_source", quote=PRIVATE)
    text = f"The observed operation state is {state}."
    result, scoped, _ = await run_recovery(bad, repair(candidate(text, "limitation", source)))
    anchors = scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"]
    assert source in anchors
    assert next(item for item in scoped["sources"] if item["source_id"] == source)["state"] == state
    assert result.status == "corrected" and text in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("sources", [
    ["tool_0001_request"], ["tool_0002_request"], ["tool_0002_result"],
    ["tool_0003_result"], ["tool_0004_result"], ["tool_0005_result"],
    ["catalog_0001"], ["verified_files"], ["tool_0001_request", "tool_0002_result"], [],
])
async def test_analysis_anchor_hint_does_not_add_missing_proof_to_model_chosen_citations(sources):
    bad = paragraph("UNSUPPORTED_ANALYSIS", source="invented_source", quote=PRIVATE)
    result, scoped, _ = await run_recovery(bad, repair(candidate("MUST_NOT_PUBLISH", "analysis", sources)))
    anchors = scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"]
    assert "tool_0001_result" in anchors and "prior_review_0001" in anchors
    assert not set(sources) & set(anchors)
    assert result.status == "unavailable" and result.text.startswith(GOOD)
    assert "MUST_NOT_PUBLISH" not in result.text
    assert result.metadata["citation_repair_status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,source", [
    ("delivery", "tool_0001_result"), ("delivery", "prior_review_0001"),
    ("context", "tool_0001_result"), ("context", "verified_files"),
    ("limitation", "catalog_0001"), ("limitation", "tool_0001_request"),
])
async def test_listing_correct_anchor_does_not_authorize_another_source_kind(kind, source):
    bad = paragraph("UNSUPPORTED_CLAIM", kind=kind, source="invented_source", quote=PRIVATE)
    result, scoped, _ = await run_recovery(bad, repair(candidate("MUST_NOT_PUBLISH", kind, source)))
    assert source not in scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"]
    assert result.status == "unavailable" and GOOD in result.text and "MUST_NOT_PUBLISH" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,source", [("analysis", "tool_0001_result"), ("delivery", "verified_files")])
async def test_real_eligible_anchor_still_cannot_publish_unverified_file_reference(kind, source):
    bad = paragraph("UNSUPPORTED_CLAIM", kind=kind, source="invented_source", quote=PRIVATE)
    result, scoped, _ = await run_recovery(bad, repair(candidate(
        f"Saved `{PRIVATE_PATH}`.", kind, source)))
    assert source in scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"]
    assert result.status == "unavailable" and GOOD in result.text and PRIVATE_PATH not in result.text
    correction = result.metadata["citation_diagnostics"]["correction"]
    assert any(key.startswith("file_reference_") for key in correction)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["invented_excerpt", "forged_anchor_field", "tool_call"])
async def test_model_cannot_forge_anchor_authority_or_request_execution(fault):
    bad = paragraph("UNSUPPORTED_CLAIM", source="invented_source", quote=PRIVATE)
    fixed = candidate("MUST_NOT_PUBLISH", "analysis", "tool_0002_result")
    if fault == "invented_excerpt":
        fixed["evidence"] = ["tool_0001_result:excerpt_9999"]
    elif fault == "forged_anchor_field":
        fixed["eligible_anchor_source_ids"] = ["tool_0002_result"]
    corrected = repair(fixed)
    if fault == "tool_call":
        corrected = AIMessage(content="", tool_calls=[{"id": "forbidden", "name": "program_run", "args": {}}])
    result, _, _ = await run_recovery(bad, corrected)
    assert result.status == "unavailable" and GOOD in result.text
    assert "MUST_NOT_PUBLISH" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("correction,complete", [("withdraw", True), ("withdraw", False), ("retain", True),
                                                  ("retain", False)])
async def test_no_analysis_anchor_cannot_turn_catalog_or_saved_code_into_completed_answer(correction, complete):
    evidence = review.AnswerEvidence()
    evidence.observe_context({"name": "Observatory", "private_note": PRIVATE})
    evidence.observe(tool("file_write", args={"file": "/home/ubuntu/output/code.py", "content": PRIVATE}))
    good = paragraph("The registered catalog is Observatory.", kind="context", source="catalog_0001", quote="Observatory")
    bad = paragraph("The mean is 999.", source="catalog_0001", quote="Observatory")
    fixed = None if correction == "withdraw" else candidate("MUST_NOT_PUBLISH", "analysis", "tool_0001_result")
    result, scoped, _ = await run_recovery(bad, repair(fixed, complete=complete), evidence=evidence, good=good, files=[])
    assert scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"] == []
    assert result.status == "unavailable" and "Observatory" in result.text
    assert "999" not in result.text and "MUST_NOT_PUBLISH" not in result.text
    if correction == "withdraw":
        assert result.metadata["reason"] == "answer_coverage_unverified"


@pytest.mark.asyncio
async def test_explicit_incomplete_coverage_still_fails_even_after_selecting_valid_anchor():
    bad = paragraph("UNSUPPORTED_CLAIM", source="invented_source", quote=PRIVATE)
    result, scoped, _ = await run_recovery(bad, repair(candidate(GOOD, "analysis", "tool_0001_result"), complete=False))
    assert "tool_0001_result" in scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"]
    assert result.status == "unavailable" and result.metadata["reason"] == "answer_coverage_unverified"
    assert GOOD in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,text,success", [
    ("analysis", HISTORY, True),
    ("analysis", "Earlier measurements gave an average of 3 mg.", False),
    ("analysis", "I just executed an analysis and measured 3 mg.", False),
    ("limitation", HISTORY, False),
])
async def test_historical_limitation_anchor_is_only_the_existing_exact_attributed_exception(kind, text, success):
    evidence = review.AnswerEvidence()
    evidence.observe_reviewed_result(historical_step())
    good = paragraph("The earlier result reported a mean of 3 mg.", source="prior_review_0001", quote=HISTORY)
    bad = paragraph("The earlier result had qualifications.", kind="limitation", source="prior_review_0001", quote=HISTORY)
    result, scoped, _ = await run_recovery(bad, repair(candidate(text, kind, "prior_review_0001")),
                                         evidence=evidence, good=good, files=[])
    failed = scoped["failed_paragraphs"][0]
    assert failed["reason"] == "unsupported_limitation"
    assert failed["eligible_anchor_source_ids"] == ["prior_review_0001"]
    assert result.status == ("corrected" if success else "unavailable")
    if success:
        assert "此前已核验的结果说明：" + HISTORY in result.text
    else:
        assert text not in result.text


@pytest.mark.asyncio
async def test_historical_hint_does_not_allow_cross_kind_repair_without_original_historical_citation():
    bad = paragraph("UNSUPPORTED_LIMITATION", kind="limitation", source="catalog_0001", quote="Observatory")
    result, scoped, _ = await run_recovery(bad, repair(candidate(HISTORY, "analysis", "prior_review_0001")))
    assert "prior_review_0001" in scoped["failed_paragraphs"][0]["eligible_anchor_source_ids"]
    assert result.status == "unavailable" and GOOD in result.text
    assert HISTORY not in result.text
