"""Regression: long escaped observations remain valid, exact, bounded JSON."""
import json

import pytest

from app.domain.services import analysis_answer_review as review
from app.domain.services.review_evidence_json import bounded_json
from test_analysis_answer_review import paragraph, response, run, tool


@pytest.mark.parametrize("limit", [64, 80, 100, 6000])
def test_escaped_long_content_is_exact_prefix_not_broken_json(limit):
    content = '名称,"quoted"\\path\n甲,2\n' * 1000
    encoded, cut = bounded_json({"success": True, "data": {"content": content}}, limit)
    retained = json.loads(encoded)["data"]["content"]
    assert cut and len(encoded) <= limit
    assert content.startswith(retained) and retained
    assert review._quote_in_source(retained, encoded)
    assert not review._quote_in_source("OMITTED_SENTINEL", encoded)


def test_exact_budget_boundary_and_complete_values_are_unchanged():
    value = {"success": True, "data": {"content": 'A,"B"\nC,4\n'}}
    original = review._json(value)
    assert bounded_json(value, len(original)) == (original, False)
    shorter, cut = bounded_json(value, len(original) - 1)
    assert cut and len(shorter) < len(original)
    assert json.loads(shorter)["data"]["content"] in value["data"]["content"]


def test_records_keep_prefix_order_and_never_join_separate_strings():
    value = {"data": {"rows": [{"left": "alpha\n", "right": "beta"}] * 100}}
    encoded, cut = bounded_json(value, 200)
    data = json.loads(encoded)
    assert cut and data["data"]["rows"][0] == value["data"]["rows"][0]
    assert not review._quote_in_source("alpha\nbeta", encoded)
    assert all(row.get("left", "") in "alpha\n" for row in data["data"]["rows"])


def test_nonfinite_scalar_is_omitted_and_never_invented_as_zero_or_null():
    encoded, cut = bounded_json({"value": float("nan"), "retained": 2}, 100)
    assert cut and json.loads(encoded) == {"retained": 2}


def test_json_scalar_object_keys_keep_json_spelling_and_collisions_are_explicitly_truncated():
    value = {True: "flag", None: "empty key", 2.5: "numeric key"}
    assert bounded_json(value, 200) == (review._json(value), False)
    encoded, cut = bounded_json({1: "first observation", "1": "second observation"}, 200)
    assert cut and json.loads(encoded) == {"1": "first observation"}


@pytest.mark.asyncio
async def test_retained_multiline_pdf_excerpt_passes_without_citation_repair():
    quote = 'A reproducible workflow\nuses "recorded" dependencies.'
    result, ask = await run(response(paragraph("The source recommends recorded dependencies.", quote=quote)),
        files=[], events=[tool("file_read", data={"content": quote + "\n" + "x" * 10000})])
    assert result.status == "verified" and result.metadata["evidence_truncated"]
    ask.assert_awaited_once()
    payload = json.loads(ask.await_args.args[0][-1].content)
    source = next(s for s in payload["sources"] if s["source_id"] == "tool_0001_result")
    assert quote in json.loads(source["text"])["data"]["content"]


@pytest.mark.asyncio
async def test_omitted_source_tail_still_cannot_support_a_quote():
    result, _ = await run(response(paragraph("Must not publish.", quote="OMITTED_SENTINEL")),
        files=[], events=[tool("file_read", data={"content": "x" * 10000 + "OMITTED_SENTINEL"})])
    assert result.status == "unavailable" and "Must not publish." not in result.text
    # Repeating a full-answer response in the correction protocol leaves the
    # review incomplete. The unsupported quote remains withheld either way.
    assert result.metadata["validation_state"] == "unavailable"


def test_catalog_and_request_evidence_are_also_parseable_when_clipped():
    evidence = review.AnswerEvidence()
    evidence.observe_context({"description": 'line\n"quoted" ' * 2000})
    evidence.observe(tool(args={"content": "x" * 10000}, data={"stdout": "done"}))
    for source in evidence.render_sources():
        json.loads(source["text"])
        assert len(source["text"]) <= review.MAX_SOURCE_CHARS
