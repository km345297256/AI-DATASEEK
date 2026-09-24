"""Publication/correction evidence is auditable without logging research text.

Synthetic reviewer responses exercise the host boundary; these tests do not
claim that a model will reliably judge scientific entailment or units.
"""
import hashlib
import json
from unittest.mock import AsyncMock

from langchain.messages import AIMessage
import pytest

from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import paragraph, response, tool, check, requirement


async def reviewed(first, second, *, language="zh", requirements=()):
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    ask = AsyncMock(side_effect=[first, second, second])
    result = await review.review_answer(ask=ask, question="Explain the measured result.",
        draft="PRIVATE_DRAFT", files=[], evidence=evidence, language=language,
        requirements=requirements)
    # Only a root/index protocol error permits one identical-evidence retry.
    expected_calls = 3 if result.metadata.get("citation_schema_repair_attempted") else 2
    assert ask.await_count == expected_calls
    return result, ask


def correction(entries):
    return AIMessage(content=json.dumps({"answer_complete": True,
        "paragraph_corrections": entries, "requirement_corrections": []}))


def repaired(index=1, **changes):
    candidate = {"text": "The observed mean equals 3 mg.", "kind": "analysis",
                 "evidence": ["tool_0001_result:excerpt_0001"]}
    candidate.update(changes)
    return {"index": index, "paragraph": candidate}


@pytest.mark.asyncio
async def test_rejected_correction_keeps_checked_facts_and_audits_the_actual_publication():
    good = paragraph()
    result, ask = await reviewed(response(good, paragraph("PRIVATE_REJECTED", quote="not observed")),
                                 AIMessage(content="{not valid JSON"))
    assert result.status == "unavailable"
    assert result.text.startswith(good["text"])
    assert "PRIVATE_REJECTED" not in result.text
    assert "有 1 段说明" in result.text and "格式或范围检查" in result.text
    assert "引用通过不等于计算方法和科学结论正确" in result.text
    diagnostic = result.metadata["citation_diagnostics"]
    assert diagnostic["correction"] == {"invalid_json": 1}
    assert diagnostic["paragraph_publication"] == {
        "scope": "citation_validation_only",
        "published": [{"review_index": 0, "published_index": 0, "disposition": "retained",
                       "text_sha256": hashlib.sha256(good["text"].encode()).hexdigest()}],
        "withheld_review_indices": [1],
        "initial_failures": [{"review_index": 1, "code": "quote_not_in_source"}],
    }
    metadata = json.dumps(result.metadata)
    for private in ["PRIVATE_", good["text"], "tool_0001_result", "mean=3; unit=mg"]:
        assert private not in metadata


@pytest.mark.asyncio
async def test_corrected_and_withdrawn_paragraph_indices_bind_to_published_text():
    result, _ = await reviewed(response(paragraph(), paragraph("REPAIR", quote="bad"),
        paragraph("WITHDRAW", quote="bad")), correction([repaired(), {"index": 2, "paragraph": None}]))
    assert result.status == "corrected"
    assert result.text == "Observed mean is 3 mg.\n\nThe observed mean equals 3 mg."
    manifest = result.metadata["citation_diagnostics"]["paragraph_publication"]
    assert manifest["withheld_review_indices"] == [2]
    assert [(p["review_index"], p["published_index"], p["disposition"]) for p in manifest["published"]] == [
        (0, 0, "retained"), (1, 1, "corrected")]
    for item, text in zip(manifest["published"], result.text.split("\n\n")):
        assert item["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("second,code", [
    (AIMessage(content='{"unexpected":"PRIVATE_RESPONSE"}'), "correction_schema_root"),
    (correction([repaired(index=0)]), "correction_schema_indices"),
    (correction([repaired(), repaired()]), "correction_schema_indices"),
    (correction([{"index": 1, "paragraph": {"text": "PRIVATE_RESPONSE"}}]), "correction_schema_paragraph"),
    (correction([repaired(kind=[])]), "correction_schema_paragraph"),
    (correction([repaired(kind="context")]), "correction_kind_change"),
])
async def test_correction_failures_have_fixed_specific_codes_and_do_not_publish_rejected_text(second, code):
    result, _ = await reviewed(response(paragraph(), paragraph("PRIVATE_REJECTED", quote="bad")), second)
    assert result.status == "unavailable"
    assert result.metadata["citation_diagnostics"]["correction"] == {code: 1}
    assert "PRIVATE_" not in result.text + json.dumps(result.metadata)
    assert result.text.startswith("Observed mean is 3 mg.")


@pytest.mark.asyncio
async def test_requirement_only_rejection_does_not_claim_paragraphs_were_removed():
    first = response(paragraph(), checks=[check("met", quote="not observed")])
    result, _ = await reviewed(first, AIMessage(content="{}"), requirements=[requirement()], language="en")
    assert result.status == "unavailable"
    assert "Review of execution-evidence citations for some requested analyses is incomplete" in result.text
    assert result.metadata["validation_state"] == "unavailable"
    assert "paragraph(s)" not in result.text
    assert result.metadata["citation_diagnostics"]["paragraph_publication"]["withheld_review_indices"] == []


@pytest.mark.asyncio
async def test_whole_answer_fallback_does_not_audit_candidate_text_as_published(monkeypatch):
    # A later whole-answer validator may still fail after a valid scoped repair.
    # Simulate a bounded publication size check rejecting combined output;
    # repair validation used the original individual-text bound beforehand.
    original_correct = review._correct_citations

    async def change_bound_after_correction(**kwargs):
        result = await original_correct(**kwargs)
        monkeypatch.setattr(review, "MAX_ANSWER_CHARS", 1)
        return result

    monkeypatch.setattr(review, "_correct_citations", change_bound_after_correction)
    result, _ = await reviewed(response(paragraph(), paragraph("PRIVATE_BAD", quote="bad")), correction([repaired()]))
    assert result.status == "unavailable" and "Observed mean" not in result.text
    manifest = result.metadata["citation_diagnostics"]["paragraph_publication"]
    assert manifest["published"] == [] and manifest["withheld_review_indices"] == [0, 1]
    assert manifest["publication_withheld_reason"] == "answer_too_long"


@pytest.mark.asyncio
async def test_same_review_calls_require_scoped_inference_and_consistent_independent_paragraphs():
    _, ask = await reviewed(response(paragraph(), paragraph("bad", quote="bad")), correction([repaired()]))
    # This verifies delivery of the rules to both model calls, not model quality.
    for call in ask.await_args_list:
        system = call.args[0][0].content
        assert "Failure of a finite set of candidate calculations" in system
        assert "unit, calendar, epoch or reference frame" in system
        assert "without depending on a rejected/withdrawn paragraph" in system
        assert "Do not simultaneously say a unit/reference is unknown" in system
