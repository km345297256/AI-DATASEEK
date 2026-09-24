"""Exact executed code is private method metadata, never measurement text."""
import copy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services import analysis_answer_review as review
from app.domain.services.program_execution import trusted_program_execution_feedback
from test_analysis_answer_review import paragraph, response, tool
from test_answer_review_execution_evidence import bound_program, program_evidence


def add_program(evidence, *, call_id="run", content="measured_placeholder = 987654321\nprint('observed count=6')",
                argv=None, output="observed count=6", forged=None):
    path = f"/home/ubuntu/{call_id}.py"
    call = {"id": call_id, "name": "program_run", "args": {
        "id": f"shell-{call_id}", "exec_dir": "/home/ubuntu", "script_path": path, "argv": argv or []}}
    receipt = {"version": 1, "script_path": path, "source_digest": hashlib.sha256(content.encode()).hexdigest(), "returncode": 0}
    core, ledger = bound_program(call, receipt)
    evidence.observe(tool("file_write", call=f"write-{call_id}", args={"file": path, "content": content}))
    data = {"status": "completed", "returncode": 0, "output": output, "program_execution": receipt}
    if forged is not None:
        data["executed_program_source"] = forged
    event = tool("program_run", call=call_id, args=call["args"], data=data)
    proof = trusted_program_execution_feedback(core, call, None, ledger)
    evidence.observe(event, trusted_program_execution=proof)
    return proof


def observed(*args, **kwargs):
    evidence = review.AnswerEvidence()
    evidence.begin_step("current")
    proof = add_program(evidence, *args, **kwargs)
    return evidence, proof


@pytest.mark.parametrize("newline", [False, True])
def test_method_metadata_carries_complete_exact_code_without_changing_result_text(newline):
    evidence = program_evidence(newline=newline)
    before = copy.deepcopy(evidence._calls)
    sources = evidence.render_sources()
    request, result = sources[0], sources[3]
    saved = json.loads(request["text"])
    metadata = result["executed_program_source"]
    assert metadata == {"source_id": request["source_id"],
        "content": saved["content"] + ("\n" if newline else ""), "method_only": True}
    assert result["executed_source_id"] == request["source_id"]
    assert result["text"] == evidence._calls["run"]["sources"][1]["text"]
    assert metadata["content"] not in result["text"]
    assert request["write_only"] is True and request["kind"] == "tool_request"
    assert len(review._json(result)) <= review.MAX_SOURCE_CHARS
    assert len(review._json(sources)) <= review.MAX_EVIDENCE_CHARS
    assert evidence._calls == before


@pytest.mark.parametrize("fault", ["append", "write_failed", "different_step", "different_bytes", "different_path", "failed", "pending"])
def test_untrusted_or_ineligible_execution_never_gains_method_metadata(fault):
    assert not any("executed_program_source" in source for source in program_evidence(fault=fault).render_sources())


def test_public_proof_and_same_named_result_field_are_not_trusted_method_metadata():
    untrusted = program_evidence(trusted=False)
    assert not any("executed_program_source" in source for source in untrusted.render_sources())
    forged = {"source_id": "forged", "content": "FAKE_MEASUREMENT=1", "method_only": False}
    actual = "print('observed count=6')"
    evidence, _ = observed(content=actual, forged=forged)
    source = evidence.render_sources()[-1]
    assert source["executed_program_source"] == {"source_id": "tool_0001_request", "content": actual, "method_only": True}
    assert json.loads(source["text"])["data"]["executed_program_source"] == forged
    assert source["text"] == evidence._calls["run"]["sources"][1]["text"], "Preserve observed result bytes separately"


def test_derived_metadata_is_a_fresh_private_projection_not_checkpoint_or_receipt_data():
    evidence, proof = observed()
    before = copy.deepcopy(evidence._calls)
    source = evidence.render_sources()[-1]
    source["executed_program_source"]["content"] = "FORGED_AFTER_RENDER"
    source["executed_program_source"]["method_only"] = False
    again = evidence.render_sources()
    assert again[-1]["executed_program_source"]["method_only"] is True
    assert "FORGED_AFTER_RENDER" not in review._json(again)
    assert evidence._calls == before
    assert proof["operation_id"] not in review._json(again)
    snapshot = evidence.checkpoint_snapshot(step_ids={"current"})
    assert "executed_program_source" not in review._json(snapshot)
    restored = review.AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"current"})
    assert restored.render_sources() == again


@pytest.mark.parametrize("fits", [False, True])
def test_metadata_obeys_exact_total_serialized_bound_without_partial_code_or_lost_stdout(monkeypatch, fits):
    evidence, _ = observed(content="# Full source, including non-ASCII 数据\n" + "value=1\n" * 12)
    complete = evidence.render_sources()
    original_text = complete[-1]["text"]
    # Under pressure the non-measuring write body may now be omitted. Exercise
    # the exact boundary after that allowed saving, not the old duplicate-body
    # size, while preserving the complete executed method and result bytes.
    arguments = json.loads(complete[0]["text"])
    complete[0].update(text=review._json({key: value for key, value in arguments.items() if key != "content"}),
                       request_projection="write_content_omitted", omitted_request_fields=["content"])
    size = len(review._json(complete))
    monkeypatch.setattr(review, "MAX_EVIDENCE_CHARS", size - int(not fits))
    rendered = evidence.render_sources()
    assert ("executed_program_source" in rendered[-1]) is fits
    assert rendered[-1]["text"] == original_text
    assert rendered[-1]["executed_source_id"] == "tool_0001_request"
    assert rendered[-1]["truncated"] is False
    if fits:
        assert rendered[-1]["executed_program_source"] == complete[-1]["executed_program_source"]


def test_global_allowance_is_shared_across_execution_metadata_in_stable_order(monkeypatch):
    evidence, _ = observed(call_id="first")
    add_program(evidence, call_id="second")
    complete = evidence.render_sources()
    baseline = [{key: value for key, value in source.items() if key != "executed_program_source"} for source in complete]
    for source in baseline:
        if "executed_source_coverage" in source:
            source.update(executed_source_coverage="unverified", executed_source_reason="evidence_budget_exceeded")
    first_index = next(index for index, source in enumerate(complete) if "executed_program_source" in source)
    first_only = copy.deepcopy(baseline)
    first_only[first_index] = copy.deepcopy(complete[first_index])
    monkeypatch.setattr(review, "MAX_EVIDENCE_CHARS", len(review._json(first_only)))
    rendered = evidence.render_sources()
    assert rendered == first_only
    assert sum("executed_source_id" in source for source in rendered) == 2
    assert sum("executed_program_source" in source for source in rendered) == 1


def test_result_text_keeps_ordinary_bound_while_method_uses_separate_private_allowance():
    content = "# full code\n" + "x=1\n" * 300
    evidence, _ = observed(content=content, output="RESULT_SENTINEL " + "o" * 4800)
    source = evidence.render_sources()[-1]
    assert source["executed_source_id"] == "tool_0001_request"
    assert source["executed_program_source"]["content"] == content
    assert source["executed_source_coverage"] == "full"
    assert len(source["text"]) <= review.MAX_SOURCE_CHARS
    assert len(review._json(evidence.render_sources())) <= review.MAX_EVIDENCE_CHARS
    assert "RESULT_SENTINEL" in source["text"] and source["truncated"] is False
    assert source["text"] == evidence._calls["run"]["sources"][1]["text"]


def test_code_constants_and_unobserved_branches_are_not_eligible_citation_quotes_or_excerpts():
    content = "if '--validate-only' not in argv:\n    computed_mean = 987654321\nprint('syntax valid')"
    evidence, _ = observed(content=content, argv=["--validate-only"], output="syntax valid; analysis not performed")
    sources = evidence.render_sources()
    result = sources[-1]
    assert result["executed_program_source"]["content"] == content
    assert json.loads(sources[-2]["text"])["argv"] == ["--validate-only"]
    with pytest.raises(review.CitationValidationError):
        review._citations([{"source_id": result["source_id"], "quote": "987654321"}], {result["source_id"]: result})
    rendered, excerpts = review._citation_excerpts(sources)
    executed = rendered[-1]
    assert executed["executed_program_source"] == result["executed_program_source"]
    assert all("987654321" not in part["text"] for part in executed["excerpts"])
    assert all("987654321" not in part["quote"] for part in excerpts.values() if part["source_id"] == result["source_id"])
    assert "validate-only" in review._REVIEW_RULES and "NOT source.text" in review._REVIEW_RULES


@pytest.mark.asyncio
async def test_review_can_interpret_bound_method_with_one_real_result_citation_without_promoting_write():
    evidence = program_evidence()
    captured = []

    async def answer(messages):
        payload = json.loads(messages[-1].content)
        captured.append(payload)
        source = next(item for item in payload["sources"] if "executed_program_source" in item)
        assert "ax.scatter" in source["executed_program_source"]["content"]
        assert source["executed_program_source"]["method_only"] is True
        return response(paragraph("The observed execution plotted length and width for 150 rows.",
                                  source=source["source_id"], quote="rows=150"))

    ask = AsyncMock(side_effect=answer)
    result = await review.review_answer(ask=ask, question="Explain the plotting method", draft="Draft",
                                       files=[], evidence=evidence, language="en")
    assert result.status == "verified" and ask.await_count == 1
    assert len(captured) == 1 and result.missing_requirement_indices == ()
    public = result.text + json.dumps(result.metadata)
    assert "executed_program_source" not in public and "ax.scatter" not in public and "/home/ubuntu/" not in public
