"""Whole-target borrowing changes coverage, never a scientific verdict."""
import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import tool
from test_report_answer_review import GOOD
from test_structured_delivery_review import prepared, checked_answer


@pytest.mark.asyncio
async def test_large_complete_table_reaches_same_review_without_changing_observations_or_verdict():
    body = ("value,note\n" + "3,observed_original_value\n" * 1400).encode()
    info, _, target = await prepared(body, "csv")
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    before = copy.deepcopy(evidence.render_sources())
    ask = AsyncMock(side_effect=lambda messages: checked_answer(messages, scientific_status="rejected"))
    result = await review.review_answer(ask=ask, question="Explain the observed data", draft=GOOD,
        files=[info], evidence=evidence, report_targets=[target])
    ask.assert_awaited_once()
    payload = json.loads(ask.await_args.args[0][-1].content)
    submitted = payload["report_targets"][0]
    assert b"".join(block["text"].encode() for block in submitted["blocks"]) == body
    assert submitted["sha256"] == target.sha256 and submitted["size"] == len(body)
    assert submitted["blocks"][-1]["byte_end"] == len(body)
    assert payload["sources"][:len(before)] == before
    assert evidence.render_sources() == before
    assert result.metadata["review_payload_capacity"]["restored_target_count"] == 1
    assert result.metadata["scientific_review"]["status"] == "rejected"
    assert result.status == "unavailable" and not result.missing_requirement_indices


@pytest.mark.asyncio
async def test_existing_schema_recovery_reuses_identical_borrowed_payload():
    body = ("value,note\n" + "3,original_observation\n" * 1600).encode()
    info, _, target = await prepared(body, "csv")
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    calls = 0

    def respond(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"invalid_schema":true}'
        return checked_answer(messages, scientific_status="unclear",
                              mutation=lambda value: value.update(answer_complete=True))

    ask = AsyncMock(side_effect=respond)
    result = await review.review_answer(ask=ask, question="Explain the observed data", draft=GOOD,
        files=[info], evidence=evidence, report_targets=[target])
    assert calls == 2
    assert ask.await_args_list[0].args[0][-1] is ask.await_args_list[1].args[0][-1]
    assert result.metadata["review_payload_capacity"]["restored_target_count"] == 1
    assert result.metadata["scientific_review"]["status"] == "unavailable"
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
