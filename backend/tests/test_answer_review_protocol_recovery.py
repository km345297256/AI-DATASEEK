"""Read-only recovery of requirement checks against immutable observations.

No model, sandbox, dataset, file write, or analytical execution is used here.
"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import file, paragraph, response, tool


OBJECTIVE = DeliverableRequirement(kind="table", objective="Compute the observed mean")
GOOD_TEXT = "The observed mean is 7."


def check(status="met", source="tool_0002_result", quote="mean=7", *, index=0):
    return {"index": index, "status": status,
            "evidence": [] if status == "unclear" else [{"source_id": source, "quote": quote}]}


def observations():
    evidence = review.AnswerEvidence()
    evidence.begin_step("current")
    evidence.observe(tool(call="failed", success=False, data={"output": "mean=999", "returncode": 1}))
    evidence.observe(tool(call="measured", data={"output": "count=4; mean=7", "returncode": 0}))
    return evidence


def correction(status="met", source="tool_0002_result", *, index=0):
    return AIMessage(content=json.dumps({"paragraph_corrections": [], "requirement_corrections": [
        {"index": index, "check": {"index": index, "status": status,
          "evidence": [] if status == "unclear" else [source + ":excerpt_0001"]}}]}))


async def execute(first_check, second, *, evidence=None, requirements=None, first_paragraph=None):
    evidence = observations() if evidence is None else evidence
    before = copy.deepcopy(evidence.__dict__)
    first = response(first_paragraph or paragraph(GOOD_TEXT, source="tool_0002_result", quote="mean=7"),
                     checks=first_check if isinstance(first_check, list) else [first_check])
    ask = AsyncMock(side_effect=[first, second])
    result = await review.review_answer(ask=ask, question="Compute the observed mean", draft="UNTRUSTED_DRAFT",
        files=[file()], evidence=evidence, requirements=[OBJECTIVE] if requirements is None else requirements)
    assert evidence.__dict__ == before
    return result, ask


@pytest.mark.asyncio
@pytest.mark.parametrize("source,quote", [
    ("tool_0001_result", "mean=999"),
    ("tool_0002_request", "python analysis.py"),
    ("verified_files", "observed.png"),
])
async def test_wrong_requirement_source_kind_is_corrected_using_real_positive_observation(source, quote):
    result, ask = await execute(check(source=source, quote=quote), correction())
    assert result.status == "corrected" and result.text == GOOD_TEXT
    assert result.missing_requirement_indices == () and ask.await_count == 2
    assert result.metadata["citation_diagnostics"]["initial"] == {"invalid_requirement_check": 1}
    assert result.metadata["unresolved_citation_count"] == 0


@pytest.mark.asyncio
async def test_unclear_objective_is_rechecked_once_without_changing_locked_text_or_checks():
    first_checks = [check("unclear"), check(index=1)]
    result, ask = await execute(first_checks, correction(), requirements=[OBJECTIVE, OBJECTIVE])
    assert result.status == "corrected" and result.text == GOOD_TEXT and ask.await_count == 2
    assert result.missing_requirement_indices == ()
    first_payload = json.loads(ask.await_args_list[0].args[0][-1].content)
    retry_messages = ask.await_args_list[1].args[0]
    retry_payload = json.loads(retry_messages[-1].content)
    assert len(retry_messages) == 2
    assert retry_payload["failed_paragraphs"] == []
    assert retry_payload["failed_requirement_checks"] == [first_checks[0]]
    assert retry_payload["requirements"] == first_payload["requirements"]
    assert "draft" not in retry_payload and "UNTRUSTED_DRAFT" not in json.dumps(retry_payload)
    original = {item["source_id"]: item for item in first_payload["sources"]}
    for item in retry_payload["sources"]:
        assert "".join(part["text"] for part in item["excerpts"]) == original[item["source_id"]]["text"]
    assert "Never use tools" in retry_messages[0].content
    assert result.metadata["citation_diagnostics"]["initial"] == {"requirement_unclear": 1}


@pytest.mark.asyncio
async def test_repeated_unclear_remains_unavailable_and_does_not_authorize_analytical_replay():
    result, ask = await execute(check("unclear"), correction("unclear"))
    assert result.status == "unavailable" and result.metadata["reason"] == "requirements_unverified"
    assert GOOD_TEXT in result.text and result.missing_requirement_indices == ()
    assert result.metadata["requirement_diagnostics"]["issues"] == [{"index": 0, "code": "requirement_unclear"}]
    assert ask.await_count == 2


@pytest.mark.asyncio
async def test_unclear_cannot_be_converted_to_negative_execution_permission():
    result, ask = await execute(check("unclear"), correction("confirmed_not_performed", "tool_0001_result"))
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    assert GOOD_TEXT in result.text and ask.await_count == 2
    assert result.metadata["citation_diagnostics"]["correction"] == {"invalid_requirement_check": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tool_0001_result", "tool_0002_request", "verified_files", "invented"])
async def test_recovery_cannot_bless_failed_or_non_result_sources_or_fabricated_excerpt_ids(source):
    result, ask = await execute(check("unclear"), correction(source=source))
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    assert GOOD_TEXT in result.text and ask.await_count == 2
    assert result.metadata["citation_repair_status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,data,name", [
    (False, {"output": "mean=7", "returncode": 1}, "shell_run"),
    (True, {"output": "mean=7", "status": "running"}, "shell_run"),
    (True, {"output": "mean=7", "execution_receipt": {"state": "unknown"}}, "shell_run"),
    (True, {"output": "mean=7"}, "file_write"),
])
@pytest.mark.parametrize("initial_status", ["unclear", "met"])
async def test_no_successful_measured_observation_does_not_start_requirement_recovery(status, data, name, initial_status):
    evidence = review.AnswerEvidence()
    evidence.observe(tool(name, success=status, data=data))
    first_check = check(initial_status, source="tool_0001_result", quote="mean=7")
    result, ask = await execute(first_check, correction(), evidence=evidence,
        first_paragraph=paragraph("Saved `observed.png`.", kind="delivery", source="verified_files", quote="observed.png"))
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    assert ask.await_count == 1 and "citation_repair_attempted" not in result.metadata


@pytest.mark.asyncio
async def test_unclear_inventory_only_requirement_without_objective_needs_no_execution_review():
    result, ask = await execute(check("unclear"), correction(),
        requirements=[DeliverableRequirement(kind="table")])
    assert result.status == "verified" and ask.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE_PROVIDER_FAILURE"), TimeoutError()])
async def test_readonly_recheck_failure_preserves_good_text_without_reclassifying_unclear(failure):
    result, ask = await execute(check("unclear"), failure)
    assert result.status == "unavailable" and result.metadata["reason"] == "requirements_unverified"
    assert GOOD_TEXT in result.text and result.missing_requirement_indices == () and ask.await_count == 2
    public = json.dumps(result.metadata)
    assert all(value not in public for value in ("PRIVATE_", "mean=7", "tool_0002", "/home/"))


@pytest.mark.asyncio
async def test_cancellation_during_recheck_propagates_without_fallback_or_retry():
    with pytest.raises(asyncio.CancelledError):
        await execute(check("unclear"), asyncio.CancelledError())


@pytest.mark.asyncio
async def test_requested_tool_is_rejected_not_executed_or_retried():
    result, ask = await execute(check("unclear"), AIMessage(content="", tool_calls=[
        {"id": "forbidden", "name": "shell_run", "args": {"command": "do not run"}}]))
    assert result.status == "unavailable" and result.missing_requirement_indices == () and ask.await_count == 2
    assert GOOD_TEXT in result.text
    assert result.metadata["citation_diagnostics"]["correction"] == {"review_requested_tools": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["paragraph", "extra_check", "duplicate_check", "unclear_with_evidence"])
async def test_recheck_cannot_modify_locked_items_or_smuggle_extra_checks(mutation):
    payload = json.loads(correction().content)
    if mutation == "paragraph":
        payload["paragraph_corrections"] = [{"index": 0, "paragraph": {
            "text": "Saved `/Users/private/fake.csv`.", "kind": "analysis",
            "evidence": ["tool_0002_result:excerpt_0001"]}}]
    elif mutation == "extra_check":
        payload["requirement_corrections"].append({"index": 1, "check": {
            "index": 1, "status": "met", "evidence": ["tool_0002_result:excerpt_0001"]}})
    elif mutation == "duplicate_check":
        payload["requirement_corrections"] *= 2
    else:
        payload["requirement_corrections"][0]["check"]["status"] = "unclear"
    result, ask = await execute(check("unclear"), AIMessage(content=json.dumps(payload)))
    assert result.status == "unavailable" and result.missing_requirement_indices == () and ask.await_count == 2
    assert GOOD_TEXT in result.text and "private" not in result.text and "fake" not in result.text


@pytest.mark.asyncio
async def test_persistent_initial_schema_failure_stops_after_one_frozen_protocol_recovery():
    ask = AsyncMock(return_value=AIMessage(content='{"paragraphs": [], "unexpected": true}'))
    result = await review.review_answer(ask=ask, question="mean", draft="", files=[],
                                        evidence=observations(), requirements=[OBJECTIVE])
    assert result.status == "unavailable" and result.metadata["reason"] == "invalid_review"
    assert ask.await_count == 2
    assert ask.await_args_list[0].args[0][1] is ask.await_args_list[1].args[0][1]
    assert result.metadata["review_schema_repair_status"] == "unavailable"
    assert "citation_repair_attempted" not in result.metadata
