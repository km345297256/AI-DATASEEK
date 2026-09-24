"""Publication checks use synthetic observations, never models or real tools."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

from langchain.messages import AIMessage, HumanMessage, SystemMessage
import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.models.plan import ExecutionStatus, Step
from app.domain.models.tool_result import ToolResult
from app.domain.services import analysis_answer_review as review


def tool(name="shell_run", *, call="call-1", success=True, args=None, data=None, calling=False):
    return ToolEvent(tool_call_id=call, tool_name="test", function_name=name,
        function_args=args or {"command": "python analysis.py"},
        status=ToolStatus.CALLING if calling else ToolStatus.CALLED,
        function_result=None if calling else ToolResult(success=success, data=data or {"stdout": "mean=3; unit=mg"}))


def file(path="/home/ubuntu/output/observed.png"):
    return FileInfo(file_id="verified-id", filename=path.rsplit("/", 1)[-1], file_path=path, size=120)


def paragraph(text="Observed mean is 3 mg.", *, kind="analysis", source="tool_0001_result", quote="mean=3; unit=mg"):
    return {"text": text, "kind": kind, "evidence": [{"source_id": source, "quote": quote}]}


def response(*paragraphs, corrected=False, checks=None):
    return AIMessage(content=json.dumps({"unsupported_claims": corrected,
        "paragraphs": list(paragraphs), "requirement_checks": checks or []}))


async def run(answer, *, events=None, files=None, evidence=None, requirements=(), draft="UNSUPPORTED DRAFT"):
    evidence = evidence or review.AnswerEvidence()
    for event in events if events is not None else [tool()]:
        evidence.observe(event)
    ask = AsyncMock(return_value=answer)
    result = await review.review_answer(ask=ask, question="Describe the observed data", draft=draft,
        files=files if files is not None else [file()], evidence=evidence, requirements=requirements)
    return result, ask


@pytest.mark.asyncio
async def test_successful_draft_also_uses_fresh_read_only_review_without_executor_tools():
    result, ask = await run(response(paragraph()), draft="Observed mean is 3 mg.")
    assert result.status == "verified" and result.text == "Observed mean is 3 mg."
    ask.assert_awaited_once()
    messages = ask.await_args.args[0]
    assert len(messages) == 2 and isinstance(messages[0], SystemMessage) and isinstance(messages[1], HumanMessage)
    payload = json.loads(messages[1].content)
    assert payload["draft"] not in [source["text"] for source in payload["sources"]]
    assert set(result.metadata) == {"status", "source_count", "file_count", "evidence_truncated", "paragraph_count", "missing_requirement_count"}
    assert "/home/" not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_unsupported_method_and_wrong_files_are_corrected_not_promoted_to_work():
    result, ask = await run(response(paragraph(), paragraph("Saved `observed.png`.", kind="delivery",
        source="verified_files", quote="observed.png"), corrected=True),
        draft="PCA separated three groups. Download invented_pca.png and invented_heatmap.png.")
    assert result.status == "corrected"
    assert "PCA" not in result.text and "invented" not in result.text
    assert result.missing_requirement_indices == ()
    ask.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [
    "`missing.png`", "missing.png", "wine\\_pca.png",
    "`/home/ubuntu/output/other/observed.png`", "`/Users/private/result.png`",
    "[chart](/home/ubuntu/output/observed.png)",
    "`unobserved.customtype`", "file:///Users/private/result.png",
    "文件位于/Users/private/report.png", "文件位于 /Users/私人/report.png",
    "文件位于 /秘密/report.png", "路径:/Users/private/report.png",
    "Generated output/fabricated.png", "目录/虚构.customtype", "output\\fabricated.png",
    "`/home/ubuntu/output/observed.png.bak`", "`/extra/home/ubuntu/output/observed.png`",
])
async def test_unverified_exact_identity_is_rejected_even_if_inventory_quote_is_real(reference):
    result, _ = await run(response(paragraph("Saved " + reference, kind="delivery", source="verified_files", quote="observed.png")))
    assert result.status == "unavailable" and result.metadata["reason"] == "unverified_file_reference"
    assert "observed.png" in result.text and "missing.png" not in result.text and "/Users/" not in result.text


@pytest.mark.asyncio
async def test_similar_basename_never_resolves_a_different_path_and_duplicate_names_need_exact_path():
    files = [file("/home/ubuntu/output/a/chart.png"), file("/home/ubuntu/output/b/chart.png")]
    result, _ = await run(response(paragraph("Saved `chart.png`.", kind="delivery", source="verified_files", quote="chart.png")), files=files)
    assert result.status == "unavailable"
    result, _ = await run(response(paragraph("Saved `/home/ubuntu/output/a/chart.png`.", kind="delivery", source="verified_files", quote="/home/ubuntu/output/a/chart.png")), files=files)
    assert result.status == "verified" and "/home/" not in result.text


@pytest.mark.asyncio
async def test_bare_unique_name_does_not_consume_preceding_prose():
    result, _ = await run(response(paragraph("Generated observed.png", kind="delivery", source="verified_files", quote="observed.png")))
    assert result.status == "verified"


@pytest.mark.asyncio
async def test_verified_unicode_and_space_containing_absolute_paths_are_redacted():
    path = "/home/ubuntu/output/结果/图表 one.png"
    result, _ = await run(response(paragraph(f"已交付`{path}`。", kind="delivery", source="verified_files", quote=path)), files=[file(path)])
    assert result.status == "verified" and "/home/" not in result.text and "图表 one.png" in result.text


def test_prior_step_evidence_keeps_its_original_scope_when_completed_late():
    evidence = review.AnswerEvidence()
    evidence.begin_step("one")
    evidence.observe(tool(calling=True))
    evidence.begin_step("two")
    evidence.observe(tool())
    evidence.observe(tool(call="two"))
    assert evidence.current_step_id == "two"
    sources = evidence.render_sources()
    assert [source["step_id"] for source in sources] == ["one", "one", "two", "two"]


def historical_step():
    return Step(id="prior", description="Earlier analysis", success=True, status=ExecutionStatus.COMPLETED,
        outcome=AnalysisOutcome(status="succeeded", reason_code="completed"), result="Earlier measured mean was 3 mg.",
        attachments=["/home/ubuntu/output/earlier.png"],
        outputs={"answer_review": {"version": 1, "status": "verified"}})


@pytest.mark.asyncio
async def test_versioned_previously_reviewed_result_supports_explanation_without_new_tools():
    evidence = review.AnswerEvidence()
    evidence.observe_reviewed_result(historical_step())
    result, _ = await run(response(paragraph("The earlier analysis measured a mean of 3 mg.",
        source="prior_review_0001", quote="Earlier measured mean was 3 mg.")), events=[], files=[], evidence=evidence)
    assert result.status == "verified"
    assert evidence.render_sources()[0]["state"] == "historical"
    assert "/home/ubuntu/output/earlier.png" in evidence.input_paths()


@pytest.mark.asyncio
async def test_historical_file_structure_followup_does_not_require_new_execution_or_limitations():
    evidence = review.AnswerEvidence()
    evidence.observe_context({"input_files": [{"path": "/home/ubuntu/inputs/group/source.csv"}]})
    earlier = historical_step()
    earlier.result = "Read source.csv; columns: sample, concentration."
    evidence.observe_reviewed_result(earlier)
    result, ask = await run(response(paragraph(
        "The earlier result read `source.csv` with columns sample and concentration.",
        source="prior_review_0001", quote=earlier.result)), events=[], files=[], evidence=evidence)
    assert result.status == "verified"
    ask.assert_awaited_once()
    system = ask.await_args.args[0][0].content
    assert "Simple factual follow-ups need only the" in system
    assert "earlier read or calculation as newly performed" in system
    assert "observed in a tool_result" in system


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["prior_review_0001", "catalog_0001"])
async def test_no_tool_followup_cannot_claim_a_new_execution_limitation(source):
    evidence = review.AnswerEvidence()
    evidence.observe_context({"input_files": [{"path": "/home/ubuntu/inputs/group/source.csv"}]})
    evidence.observe_reviewed_result(historical_step())
    sources = {item["source_id"]: item for item in evidence.render_sources()}
    result, ask = await run(response(paragraph("A new file read failed.", kind="limitation",
        source=source, quote=sources[source]["text"])), events=[], files=[], evidence=evidence)
    assert result.status == "unavailable"
    assert result.metadata["reason"] == "unsupported_limitation"
    assert ask.await_count == 3
    assert "A new file read failed" not in result.text
    assert result.metadata["citation_diagnostics"]["initial"] == {"unsupported_limitation": 1}


@pytest.mark.parametrize("change", ["unversioned", "bad_version", "unavailable", "failed", "uncompleted", "bare_dict"])
def test_legacy_or_untrusted_historical_success_cannot_become_evidence(change):
    step = historical_step()
    if change == "unversioned":
        step.outputs["answer_review"].pop("version")
    elif change == "bad_version":
        step.outputs["answer_review"]["version"] = True
    elif change == "unavailable":
        step.outputs["answer_review"]["status"] = "unavailable"
    elif change == "failed":
        step.outcome.status = "failed"
    elif change == "uncompleted":
        step.status = ExecutionStatus.PENDING
    elif change == "bare_dict":
        step = step.model_dump()
    evidence = review.AnswerEvidence()
    evidence.observe_reviewed_result(step)
    assert evidence.render_sources() == [] and evidence.input_paths() == set()


@pytest.mark.asyncio
async def test_historical_facts_cannot_prove_a_new_authorized_objective_was_executed():
    evidence = review.AnswerEvidence()
    evidence.observe_reviewed_result(historical_step())
    result, _ = await run(response(paragraph("The earlier analysis measured a mean of 3 mg.",
        source="prior_review_0001", quote="Earlier measured mean was 3 mg."),
        checks=[check("met", source="prior_review_0001", quote="Earlier measured mean was 3 mg.")]),
        events=[], files=[], evidence=evidence, requirements=[requirement()])
    assert result.status == "unavailable" and result.missing_requirement_indices == ()


def receipt(path, *, valid=False, reason="missing_artifact"):
    return {"path": path, "valid": valid, "reason": reason}


@pytest.mark.parametrize("name", ["generic.png", "任意名称.customtype", "report2.json"])
def test_draft_only_missing_claim_can_be_classified_without_hiding_receipts(name):
    path = "/home/ubuntu/output/" + name
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    records = [receipt(path)]
    assert review.rejected_missing_claim_paths(declared_paths=[path], requirements=[], records=records, evidence=evidence) == {path}
    assert records == [receipt(path)]


@pytest.mark.parametrize("scenario", ["attempted", "same_name_attempt", "same_name_receipt", "required", "objective", "truncated", "pending", "write_only", "invalid", "conflict"])
def test_genuine_or_uncertain_auxiliary_failures_remain_visible(scenario):
    path = "/home/ubuntu/output/result.png"
    evidence = review.AnswerEvidence()
    records, requirements = [receipt(path)], []
    evidence.observe(tool("file_write" if scenario == "write_only" else "shell_run"))
    if scenario in {"attempted", "same_name_attempt"}:
        evidence.observe(tool("file_write", call="write", args={"path": path if scenario == "attempted" else "/home/ubuntu/output/other/result.png"}))
    elif scenario == "same_name_receipt":
        records.append(receipt("/home/ubuntu/output/other/result.png", valid=True))
    elif scenario == "required":
        requirements = [DeliverableRequirement(kind="image", output_paths=[path])]
    elif scenario == "objective":
        requirements = [DeliverableRequirement(kind="image", objective="Create result.png")]
    elif scenario == "truncated":
        evidence.observe(tool(call="cut", data={"truncated": True}))
    elif scenario == "pending":
        evidence.observe(tool(call="pending", calling=True))
    elif scenario == "invalid":
        records = [receipt(path, reason="invalid_content")]
    elif scenario == "conflict":
        records.append(receipt(path, valid=True))
    assert review.rejected_missing_claim_paths(declared_paths=[path], requirements=requirements, records=records, evidence=evidence) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,quote", [("unknown", "mean=3"), ("tool_0001_result", "mean=97.3"), ("tool_0001_result", "")])
async def test_invented_source_or_quote_fails_closed(source, quote):
    result, _ = await run(response(paragraph(source=source, quote=quote)))
    assert result.status == "unavailable" and result.metadata["reason"] == "invalid_citations"
    assert "Observed mean" not in result.text and "observed.png" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("source,quote,events", [
    ("verified_files", "observed.png", []),
    ("tool_0001_request", "python analysis.py", [tool()]),
    ("tool_0001_result", "mean=3; unit=mg", [tool("file_write")]),
    ("tool_0001_result", "mean=3; unit=mg", [tool(success=False)]),
    ("tool_0001_result", "running", [tool(data={"status": "running"})]),
])
async def test_file_existence_saved_script_attempt_or_failure_is_not_computational_proof(source, quote, events):
    result, ask = await run(response(paragraph(source=source, quote=quote)), events=events)
    assert result.status == "unavailable"
    # A read-only paragraph correction may withdraw an unsupported claim, but
    # repeating the same invalid response cannot manufacture execution proof.
    assert result.metadata["reason"] == "unsupported_analysis"
    assert "Observed mean" not in result.text and ask.await_count == 3
    assert result.metadata["citation_diagnostics"]["initial"] == {"unsupported_analysis": 1}


@pytest.mark.asyncio
async def test_observed_input_filename_is_not_mistaken_for_a_missing_output():
    result, _ = await run(response(paragraph("The observed mean in `input.csv` is 3 mg.")),
        events=[tool("dataset_quicklook", args={"input_path": "/home/ubuntu/datasets/source/input.csv"})])
    assert result.status == "verified"


@pytest.mark.asyncio
async def test_registered_metadata_can_support_catalog_facts_but_not_new_analytical_claims():
    evidence = review.AnswerEvidence()
    evidence.observe_context({"filename": "input.csv", "description": "Registered soil observations"})
    valid = paragraph("`input.csv` is registered as soil observations.", kind="context", source="catalog_0001", quote="Registered soil observations")
    result, _ = await run(response(valid), events=[], evidence=evidence, files=[])
    assert result.status == "verified"
    valid["kind"] = "analysis"
    result, _ = await run(response(valid), events=[], evidence=evidence, files=[])
    assert result.status == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not json", '{"unsupported_claims":false,"unsupported_claims":true}', "NaN"])
async def test_invalid_model_json_does_not_start_model_json_repair(raw):
    result, ask = await run(AIMessage(content=raw))
    assert result.status == "unavailable"
    ask.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_tool_request_is_never_executed():
    value = AIMessage(content="", tool_calls=[{"id": "evil", "name": "shell_run", "args": {"command": "do something"}}])
    result, ask = await run(value)
    assert result.status == "unavailable" and result.metadata["reason"] == "review_requested_tools"
    ask.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_failure_retains_truthful_delivery_and_no_exception_details():
    ask = AsyncMock(side_effect=RuntimeError("secret /Users/private/key"))
    result = await review.review_answer(ask=ask, question="question", draft="unverified successful answer", files=[file()], evidence=review.AnswerEvidence())
    assert result.status == "unavailable" and "observed.png" in result.text
    assert "secret" not in result.text and "unverified successful answer" not in result.text
    assert "/Users/" not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_review_call_timeout_is_bounded_but_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_TIMEOUT_SECONDS", 0.001)
    async def stalled(messages):
        await asyncio.Event().wait()
    result = await review.review_answer(ask=stalled, question="", draft="", files=[file()], evidence=review.AnswerEvidence())
    assert result.status == "unavailable"
    assert result.metadata["reason"] == "review_timeout"
    ask = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await review.review_answer(ask=ask, question="", draft="", files=[], evidence=review.AnswerEvidence())


@pytest.mark.asyncio
async def test_transport_retry_window_is_not_cut_off_by_default_review_deadline(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_TIMEOUT_SECONDS", 0.001)
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    async def retrying_transport(messages):
        # A read-only transport can retry inside its explicitly bounded window.
        await asyncio.sleep(0.01)
        return response(paragraph())
    result = await review.review_answer(ask=retrying_transport, question="", draft="", files=[file()],
        evidence=evidence, timeout_seconds=1.0)
    assert result.status == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [0, -1, float("inf"), float("nan"), True, "45"])
async def test_review_deadline_must_be_finite_positive_operator_value(deadline):
    ask = AsyncMock()
    with pytest.raises(ValueError, match="invalid_review_deadline"):
        await review.review_answer(ask=ask, question="", draft="", files=[],
            evidence=review.AnswerEvidence(), timeout_seconds=deadline)
    ask.assert_not_awaited()


def requirement():
    return DeliverableRequirement(kind="image", objective="Compare measured group distributions")


def check(status, *, source="tool_0001_result", quote="mean=3; unit=mg"):
    return {"index": 0, "status": status, "evidence": [{"source_id": source, "quote": quote}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("checks", [[], [check("unclear")]])
async def test_missing_or_unclear_authorized_objective_cannot_pass_as_completed(checks):
    result, _ = await run(response(paragraph(), checks=checks), requirements=[requirement()])
    assert result.status == "unavailable" and result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_inventory_alone_cannot_claim_authorized_analytical_objective_met():
    result, _ = await run(response(paragraph(), checks=[check("met", source="verified_files", quote="observed.png")]), requirements=[requirement()])
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_confirmed_missing_objective_requires_explicit_result_not_draft_or_absence():
    result, _ = await run(response(paragraph("Execution reports the requested comparison was not performed.", kind="limitation", quote="comparison not performed"),
        corrected=True, checks=[check("confirmed_not_performed", quote="comparison not performed")]),
        events=[tool(success=False, data={"message": "comparison not performed"})], requirements=[requirement()])
    assert result.status == "corrected" and result.missing_requirement_indices == (0,)


@pytest.mark.asyncio
async def test_truncated_evidence_does_not_authorize_repair_from_negative_claim():
    evidence = review.AnswerEvidence()
    evidence.observe(tool(data={"stdout": "comparison not performed " + "x" * review.MAX_SOURCE_CHARS}))
    result, _ = await run(response(paragraph("Observed comparison is not yet confirmed.", kind="limitation", quote="comparison not performed"),
        corrected=True, checks=[check("confirmed_not_performed", quote="comparison not performed")]),
        events=[], evidence=evidence, requirements=[requirement()])
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    assert result.metadata["evidence_truncated"] is True


def test_collector_reuses_call_identity_and_never_grows_unbounded():
    evidence = review.AnswerEvidence()
    evidence.observe(tool(calling=True))
    first_ids = [item["source_id"] for item in evidence.render_sources()]
    evidence.observe(tool())
    evidence.observe(tool(calling=True))
    assert [item["source_id"] for item in evidence.render_sources()] == first_ids
    assert all(item["state"] == "succeeded" for item in evidence.render_sources())
    for index in range(review.MAX_CALLS * 3):
        evidence.observe(tool(call=str(index), data={"stdout": "x" * (review.MAX_SOURCE_CHARS * 2)}))
    assert evidence.truncated
    sources = evidence.render_sources()
    assert len(sources) <= review.MAX_CALLS * 2
    assert sum(len(source["text"]) for source in sources) <= review.MAX_EVIDENCE_CHARS
    assert len({source["source_id"] for source in sources}) == len(sources)


def test_upstream_truncation_and_unknown_execution_receipt_are_not_success_proof():
    evidence = review.AnswerEvidence()
    evidence.observe(tool(data={"stdout": "prefix", "truncated": True,
        "execution_receipt": {"state": "unknown", "returncode": None}}))
    assert evidence.truncated
    assert all(source["state"] == "pending" for source in evidence.render_sources())


def citation_correction(messages, *, paragraph_index=0, text="Observed mean is 3 mg.",
                        source="tool_0001_result", kind="analysis", checks=None):
    payload = json.loads(messages[-1].content)
    observed = next(item for item in payload["sources"] if item["source_id"] == source)
    return json.dumps({"answer_complete": True, "paragraph_corrections": [{"index": paragraph_index, "paragraph": {
        "text": text, "kind": kind, "evidence": [observed["excerpts"][0]["evidence_id"]]}}],
        "requirement_corrections": checks or []})


async def run_correction(first, correction, *, evidence=None, requirements=(), files=None):
    evidence = evidence or review.AnswerEvidence()
    if not evidence.render_sources():
        evidence.observe(tool())
    calls = []

    async def ask(messages):
        calls.append(messages)
        if len(calls) == 1:
            return first
        if isinstance(correction, BaseException):
            raise correction
        return correction(messages)

    result = await review.review_answer(ask=ask, question="Describe the observed data", draft="DRAFT_ONLY",
        files=[file()] if files is None else files, evidence=evidence, requirements=requirements)
    return result, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_source,bad_quote", [("unknown", "mean=3"), ("tool_0001_result", "mean = 3 mg")])
async def test_citation_correction_uses_host_resolved_exact_excerpts_without_reexecuting(bad_source, bad_quote):
    result, calls = await run_correction(response(paragraph(source=bad_source, quote=bad_quote)), citation_correction)
    assert result.status == "corrected" and result.text == "Observed mean is 3 mg."
    assert result.metadata["citation_repair_status"] == "corrected"
    assert result.metadata["unresolved_citation_count"] == 0
    assert len(calls) == 2 and all(len(messages) == 2 for messages in calls)
    correction = json.loads(calls[1][-1].content)
    assert "draft" not in correction
    assert correction["failed_paragraphs"][0]["index"] == 0
    assert all("text" not in source for source in correction["sources"])
    original = {source["source_id"]: source for source in json.loads(calls[0][-1].content)["sources"]}
    for source in correction["sources"]:
        assert "".join(excerpt["text"] for excerpt in source["excerpts"]) == original[source["source_id"]]["text"]
    assert "tool_0001" not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_source_description_citation_can_be_corrected_without_creating_outputs():
    evidence = review.AnswerEvidence()
    evidence.observe(tool("file_read", args={"file": "/home/ubuntu/inputs/" + "a" * 24 + "/source.csv"},
                          data={"content": "label,value\nA,2\n"}))
    text = "`source.csv` contains the columns label and value."
    first = response(paragraph(text, kind="analysis", quote="label, value\nA,2"))
    result, calls = await run_correction(first,
        lambda messages: citation_correction(messages, text=text, kind="analysis"), evidence=evidence, files=[])
    assert result.status == "corrected" and result.text == text
    assert result.metadata["file_count"] == 0 and result.missing_requirement_indices == ()
    assert result.metadata["citation_diagnostics"]["initial"] == {"quote_not_in_source": 1}
    assert len(calls) == 2
    # The correction has one schema, not two contradictory evidence protocols.
    system = calls[1][0].content
    assert '"paragraph_corrections"' in system and '"quote"' not in system
    assert '"unsupported_claims"' not in system


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["label,value", "missing quote"])
async def test_kind_correction_cannot_promote_input_observation_to_delivered_output(quote):
    evidence = review.AnswerEvidence()
    evidence.observe(tool("file_read", args={"file": "/home/ubuntu/inputs/" + "a" * 24 + "/source.csv"},
                          data={"content": "label,value\nA,2\n"}))
    first = response(paragraph("Delivered `source.csv`.", kind="delivery", quote=quote))
    result, calls = await run_correction(first,
        lambda messages: citation_correction(messages, text="Delivered `source.csv`.", kind="analysis"),
        evidence=evidence, files=[])
    assert result.status == "unavailable" and "Delivered" not in result.text
    assert result.metadata["citation_diagnostics"]["correction"] == {"correction_kind_change": 1}
    assert len(calls) == 2
    if quote == "missing quote":
        assert result.metadata["reason"] == "invalid_citations"
    else:
        assert result.metadata["reason"] == "unsupported_delivery"
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,quote,expected", [
    ("not-a-real-source", "mean=3", "unknown_source_id"),
    ("tool_0001_result", "quote not present", "quote_not_in_source"),
    ("tool_0001_result", "", "empty_quote"),
])
async def test_citation_diagnostics_distinguish_source_and_quote_without_logging_evidence(source, quote, expected):
    result, _ = await run_correction(response(paragraph(source=source, quote=quote)), citation_correction)
    diagnostics = result.metadata["citation_diagnostics"]
    assert diagnostics["initial"] == {expected: 1}
    assert diagnostics["correction"] == {}
    assert result.status == "corrected"
    serialized = json.dumps(diagnostics)
    assert "mean=3" not in serialized
    if quote:
        assert quote not in serialized
    assert "tool_0001" not in serialized and "/home/" not in serialized


@pytest.mark.asyncio
async def test_unknown_repair_excerpt_is_diagnosed_but_still_withheld():
    def bad_correction(_messages):
        return json.dumps({"paragraph_corrections": [{"index": 0, "paragraph": {
            "text": "Claim must stay unpublished.", "kind": "analysis", "evidence": ["invented-private-id"]}}],
            "requirement_corrections": []})
    result, _ = await run_correction(response(paragraph(quote="mismatch")), bad_correction)
    assert result.status == "unavailable" and "Claim must stay unpublished" not in result.text
    assert result.metadata["citation_diagnostics"]["correction"] == {"unknown_excerpt_id": 1}
    assert "invented-private-id" not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_uploaded_file_read_body_is_in_review_and_exact_newline_quote_is_valid():
    evidence = review.AnswerEvidence()
    evidence.observe(tool("file_read", args={"file": "/home/ubuntu/inputs/" + "a" * 24 + "/sample.csv"},
        data={"content": "label,value\nA,2\nB,4\n", "file": "sample.csv"}))
    # The model sees JSON source.text, in which the newline is escaped. The
    # exact decoded file content remains the same observed evidence.
    first = response(paragraph("Columns are label and value.", quote="label,value\nA,2"))
    result, calls = await run_correction(first,
        lambda messages: citation_correction(messages, text="Columns are label and value."), evidence=evidence)
    assert len(calls) == 1
    assert "citation_repair_attempted" not in result.metadata
    original_payload = json.loads(calls[0][-1].content)
    source = next(item for item in original_payload["sources"] if item["kind"] == "tool_result")
    assert json.loads(source["text"])["data"]["content"] == "label,value\nA,2\nB,4\n"
    assert result.status == "verified"


@pytest.mark.asyncio
async def test_invalid_correction_schema_diagnostic_never_copies_response_or_exception():
    result, _ = await run_correction(response(paragraph(quote="mismatch")),
        lambda _: "not json /Users/private/file.csv SECRET_CONTENT")
    assert result.status == "unavailable"
    assert result.metadata["citation_diagnostics"]["correction"] == {"invalid_json": 1}
    assert "/Users/" not in json.dumps(result.metadata) and "SECRET_CONTENT" not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_correction_cannot_modify_already_accepted_paragraphs():
    good = paragraph()
    bad = paragraph("Second measurement is 8 mg.", quote="not present")

    def malicious(messages):
        # Index 0 is not within the failed-item scope.
        return citation_correction(messages, paragraph_index=0, text="UNAUTHORIZED_REWRITE")

    result, calls = await run_correction(response(good, bad), malicious)
    assert result.status == "unavailable" and result.metadata["reason"] == "invalid_citations"
    assert "Observed mean is 3 mg." in result.text
    assert "UNAUTHORIZED_REWRITE" not in result.text and "Second measurement" not in result.text
    assert result.metadata["withheld_paragraph_count"] == 1
    assert len(calls) == 3
    assert [item["index"] for item in json.loads(calls[1][-1].content)["failed_paragraphs"]] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_id", "quote_instead_of_id", "invented_file", "inventory_as_analysis", "provider", "tool", "malformed", "duplicate_index"])
async def test_failed_correction_is_bounded_and_preserves_supported_paragraphs(failure):
    good, bad = paragraph(), paragraph("UNSUPPORTED", quote="not present")

    def incorrect(messages):
        if failure == "provider":
            raise RuntimeError("PRIVATE /Users/private/token")
        if failure == "tool":
            return AIMessage(content="", tool_calls=[{"id": "no", "name": "shell_run", "args": {}}])
        if failure == "malformed":
            return "not json"
        value = json.loads(citation_correction(messages, paragraph_index=1))
        candidate = value["paragraph_corrections"][0]["paragraph"]
        if failure == "unknown_id":
            candidate["evidence"] = ["tool_evicted_result:excerpt_0001"]
        elif failure == "quote_instead_of_id":
            candidate["evidence"] = [{"source_id": "tool_0001_result", "quote": "mean=3; unit=mg"}]
        elif failure == "invented_file":
            candidate["text"] = "Saved `/Users/private/invented.png`."
        elif failure == "inventory_as_analysis":
            candidate["evidence"] = ["verified_files:excerpt_0001"]
        elif failure == "duplicate_index":
            value["paragraph_corrections"] *= 2
        return json.dumps(value)

    result, calls = await run_correction(response(good, bad), incorrect)
    assert result.status == "unavailable" and "Observed mean is 3 mg." in result.text
    assert "UNSUPPORTED" not in result.text and "PRIVATE" not in result.text and "/Users/" not in result.text
    assert result.missing_requirement_indices == ()
    assert len(calls) == (3 if failure == "duplicate_index" else 2)
    assert result.metadata["unresolved_citation_count"] == 1


@pytest.mark.asyncio
async def test_corrector_may_explicitly_withdraw_an_unsupported_optional_claim():
    def withdraw(messages):
        return json.dumps({"answer_complete": True,
            "paragraph_corrections": [{"index": 1, "paragraph": None}], "requirement_corrections": []})

    result, _ = await run_correction(response(paragraph(), paragraph("UNSUPPORTED", quote="not present")), withdraw)
    assert result.status == "corrected" and result.text == "Observed mean is 3 mg."
    assert result.metadata["withheld_paragraph_count"] == 1
    assert result.metadata["unresolved_citation_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired_status", ["met", "unclear"])
async def test_requirement_citations_are_corrected_without_modifying_valid_paragraph(repaired_status):
    def repair(messages):
        return json.dumps({"paragraph_corrections": [], "requirement_corrections": [{"index": 0, "check": {
            "index": 0, "status": repaired_status,
            "evidence": ["tool_0001_result:excerpt_0001"] if repaired_status == "met" else []}}]})

    result, calls = await run_correction(response(paragraph(), checks=[check("met", quote="invalid")]), repair,
        requirements=[requirement()])
    assert "Observed mean is 3 mg." in result.text
    assert result.status == ("corrected" if repaired_status == "met" else "unavailable")
    assert result.missing_requirement_indices == () and len(calls) == 2
    assert json.loads(calls[1][-1].content)["failed_paragraphs"] == []


@pytest.mark.asyncio
async def test_failed_requirement_correction_never_authorizes_analytical_replay():
    result, calls = await run_correction(response(paragraph(), checks=[check("met", quote="invalid")]),
        RuntimeError("provider unavailable"), requirements=[requirement()])
    assert result.status == "unavailable" and result.metadata["reason"] == "invalid_citations"
    assert "Observed mean is 3 mg." in result.text and result.missing_requirement_indices == ()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_correction_cannot_recover_omitted_text_or_use_truncation_as_negative_proof():
    evidence = review.AnswerEvidence()
    evidence.observe(tool(data={"stdout": "comparison not performed " + "x" * review.MAX_SOURCE_CHARS + "OMITTED_SENTINEL"}))

    def repair(messages):
        payload = json.loads(messages[-1].content)
        assert payload["truncated"] is True and "OMITTED_SENTINEL" not in json.dumps(payload["sources"])
        return json.dumps({"paragraph_corrections": [], "requirement_corrections": [{"index": 0, "check": {
            "index": 0, "status": "confirmed_not_performed", "evidence": ["tool_0001_result:excerpt_0001"]}}]})

    valid = paragraph("The observed comparison is not yet confirmed.", kind="limitation", quote="comparison not performed")
    result, _ = await run_correction(response(valid, checks=[check("confirmed_not_performed", quote="invalid")]),
        repair, evidence=evidence, requirements=[requirement()])
    assert result.status == "unavailable" and result.missing_requirement_indices == ()
    assert result.metadata["reason"] == "requirements_unverified"


@pytest.mark.asyncio
async def test_citation_correction_timeout_preserves_valid_text_and_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_TIMEOUT_SECONDS", 0.001)
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    first = response(paragraph(), paragraph("UNSUPPORTED", quote="invalid"))
    calls = []

    async def stalled(messages):
        calls.append(messages)
        if len(calls) == 1:
            return first
        await asyncio.Event().wait()

    result = await review.review_answer(ask=stalled, question="", draft="", files=[file()], evidence=evidence)
    assert result.status == "unavailable" and "Observed mean is 3 mg." in result.text and len(calls) == 2
    with pytest.raises(asyncio.CancelledError):
        await run_correction(first, asyncio.CancelledError())


@pytest.mark.asyncio
async def test_citation_correction_also_honors_complete_transport_window(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_TIMEOUT_SECONDS", 0.001)
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    calls = []
    async def ask(messages):
        calls.append(messages)
        if len(calls) == 1:
            return response(paragraph(quote="not a source quote"))
        await asyncio.sleep(0.01)
        return citation_correction(messages)
    result = await review.review_answer(ask=ask, question="", draft="", files=[file()],
        evidence=evidence, timeout_seconds=1.0)
    assert result.status == "corrected"
    assert result.metadata["citation_repair_status"] == "corrected"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_long_execution_review_repairs_reference_from_same_bounded_frozen_evidence():
    evidence = review.AnswerEvidence()
    for index in range(41):
        evidence.observe(tool(call=f"call-{index}", data={"stdout": f"iteration={index}; mean=3; unit=mg"}))
    expected_ids = [source["source_id"] for source in evidence.render_sources()]

    def repair(messages):
        payload = json.loads(messages[-1].content)
        assert payload["truncated"] is True
        assert [source["source_id"] for source in payload["sources"]
                if source["kind"] not in {"current_request", "delivery_inventory"}] == expected_ids
        assert [source["source_id"] for source in payload["sources"][-2:]] == ["current_request", "verified_files"]
        assert "tool_0001_result" not in expected_ids
        return citation_correction(messages, source="tool_0041_result")

    result, calls = await run_correction(response(paragraph()), repair, evidence=evidence)
    assert result.status == "corrected" and result.text == "Observed mean is 3 mg."
    assert result.metadata["evidence_truncated"] is True and len(calls) == 2
    assert [source["source_id"] for source in evidence.render_sources()] == expected_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("content,quote", [
    ("label,value\nA,2\nB,4\n", "label,value\nA,2"),
    ('The column is "measured value".', 'column is "measured value"'),
    ("Observed labels: α\tβ; path separator: \\", "α\tβ"),
])
async def test_exact_decoded_observation_quote_does_not_need_model_repair(content, quote):
    result, ask = await run(response(paragraph("The observed source was read.", quote=quote),
        checks=[check("met", quote=quote)]),
        events=[tool("file_read", data={"content": content})], requirements=[requirement()])
    assert result.status == "verified"
    ask.assert_awaited_once()
    assert "citation_repair_attempted" not in result.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("content,quote", [
    ("label,value\nA,2\nB,4\n", "label,value\nA,3"),
    ("label,value\nA,2\nB,4\n", "label,value A,2"),
    ("Observed mean = 3", "Observed mean=3"),
    ("literal \\n is not a newline", "literal \n is not a newline"),
])
async def test_decoded_citation_does_not_fuzz_numbers_whitespace_or_escaping(content, quote):
    result, _ = await run(response(paragraph("MUST NOT PUBLISH", quote=quote)),
        events=[tool("file_read", data={"content": content})])
    assert result.status == "unavailable"
    assert "MUST NOT PUBLISH" not in result.text


@pytest.mark.parametrize("text,quote", [
    ('{"first":"alpha\\n","second":"beta"}', "alpha\nbeta"),
    ('{"content":"alpha\\nbeta', "alpha\nbeta"),
    ("plain text alpha\\nbeta", "alpha\nbeta"),
])
def test_decoded_quote_cannot_join_fields_or_reconstruct_missing_evidence(text, quote):
    assert not review._quote_in_source(quote, text)


@pytest.mark.asyncio
async def test_unverified_objectives_have_safe_indices_and_fixed_reasons():
    requirements = [requirement(), DeliverableRequirement(kind="table", objective="PRIVATE_OBJECTIVE")]
    checks = [check("met"), {"index": 1, "status": "unclear", "evidence": []}]
    result, _ = await run(response(paragraph(), checks=checks), requirements=requirements)
    assert result.status == "unavailable"
    assert result.metadata["reason"] == "requirements_unverified"
    assert result.metadata["requirement_diagnostics"] == {
        "status_counts": {"met": 1, "unclear": 1},
        "issues": [{"index": 1, "code": "requirement_unclear"}],
    }
    public = json.dumps(result.metadata)
    assert all(value not in public for value in ("PRIVATE_OBJECTIVE", "mean=3", "tool_0001", "/home/"))


@pytest.mark.asyncio
async def test_unresolved_requirement_citation_diagnostics_preserve_exact_safe_index():
    requirements = [requirement(), DeliverableRequirement(kind="table", objective="PRIVATE_OBJECTIVE")]
    checks = [check("met"), {**check("met", quote="PRIVATE_BAD_QUOTE"), "index": 1}]
    result, _ = await run_correction(response(paragraph(), checks=checks),
        RuntimeError("PRIVATE_PROVIDER_FAILURE"), requirements=requirements)
    assert result.status == "unavailable"
    diagnostics = result.metadata["citation_diagnostics"]
    assert diagnostics["failed_requirement_indices"] == [1]
    assert diagnostics["unresolved_requirement_indices"] == [1]
    public = json.dumps(result.metadata)
    assert all(value not in public for value in ("PRIVATE_", "mean=3", "tool_0001", "/home/"))


@pytest.mark.asyncio
async def test_completed_analysis_with_failed_optional_inspection_keeps_exact_csv_citations():
    """Regression shape from the frozen real analysis; no dataset/model prose stored."""
    evidence = review.AnswerEvidence()
    evidence.begin_step("analysis")
    evidence.observe(tool("file_read", call="source", data={
        "content": 'ax.set_xlabel("Length (mm)")\nax.set_ylabel("Mass (mg)")\n'}))
    evidence.observe(tool("program_run", call="analysis", data={
        "status": "completed", "returncode": 0,
        "output": "group A: n=2, mean=3; generated summary and scatter"}))
    evidence.observe(tool("shell_run", call="optional-inspection", success=False, data={
        "status": "completed", "returncode": 127,
        "output": "group,count,mean\nA,2,3\noptional-inspector: command not found"}))
    requirements = [DeliverableRequirement(kind="table", objective="Group counts and mean"),
                    DeliverableRequirement(kind="image", objective="Scatter with English labels")]
    answer = response(
        paragraph("Group A contains 2 measurements with mean 3.",
            source="tool_0002_result", quote="group A: n=2, mean=3"),
        paragraph("The chart uses English axis labels.", source="tool_0001_result",
            quote='ax.set_xlabel("Length (mm)")'),
        {"text": "Delivered `summary.csv` with columns group, count and mean.", "kind": "delivery",
         "evidence": [{"source_id": "tool_0003_result", "quote": "group,count,mean\nA,2,3"},
                      {"source_id": "verified_files", "quote": "summary.csv"}]},
        paragraph("Delivered `scatter.png`.", kind="delivery", source="verified_files", quote="scatter.png"),
        corrected=True,
        checks=[{**check("met", source="tool_0002_result", quote="generated summary and scatter"), "index": i}
                for i in range(2)])
    result, ask = await run(answer, events=[], evidence=evidence, requirements=requirements,
        files=[file("/home/ubuntu/output/summary.csv"), file("/home/ubuntu/output/scatter.png")])
    assert result.status == "corrected" and result.missing_requirement_indices == ()
    assert result.metadata["paragraph_count"] == 4 and result.metadata["file_count"] == 2
    assert "citation_repair_attempted" not in result.metadata
    ask.assert_awaited_once()


def checkpoint_evidence():
    evidence = review.AnswerEvidence()
    evidence.observe_context({"files": [{"path": "/home/ubuntu/inputs/source.csv"}]})
    evidence.begin_step("analysis")
    evidence.observe(tool("file_read", call="read", args={"file": "/home/ubuntu/inputs/source.csv"},
        data={"content": "group,value\nA,3\n"}))
    evidence.observe(tool("program_run", call="run", data={"returncode": 0, "output": "group A: mean=3"}))
    return evidence


@pytest.mark.asyncio
async def test_delivery_retry_restores_observations_without_promoting_old_draft_or_running_tools():
    evidence = checkpoint_evidence()
    snapshot = evidence.checkpoint_snapshot(step_ids={"analysis"})
    restored = review.AnswerEvidence.from_checkpoint_snapshot(json.loads(json.dumps(snapshot)), step_ids={"analysis"})
    assert restored.render_sources() == evidence.render_sources()
    assert restored.input_paths() == evidence.input_paths()
    assert restored.current_step_id == "analysis" and not restored.truncated
    assert restored.checkpoint_snapshot(step_ids={"analysis"}) == snapshot
    answer = response(paragraph("Group A mean is 3.", source="tool_0002_result", quote="group A: mean=3"),
        checks=[check("met", source="tool_0002_result", quote="group A: mean=3")])
    result, ask = await run(answer, events=[], evidence=restored, requirements=[requirement()])
    assert result.status == "verified" and result.missing_requirement_indices == ()
    ask.assert_awaited_once()
    # Restore copies all mutable containers. Later object mutation cannot
    # change already-authenticated observations through shared references.
    snapshot["calls"][0]["sources"][1]["text"] = "UNTRUSTED_MUTATION"
    assert "UNTRUSTED_MUTATION" not in json.dumps(restored.render_sources())


@pytest.mark.parametrize("status,called", [("pending", False), ("pending", True), ("failed", True), ("unknown", True)])
def test_checkpoint_preserves_non_successful_observation_state(status, called):
    evidence = checkpoint_evidence()
    if not called:
        event = tool(call="last", calling=True)
    elif status == "pending":
        event = tool(call="last", data={"status": "running"})
    elif status == "failed":
        event = tool(call="last", success=False)
    else:
        event = tool(call="last")
        event.function_result = {"data": {"output": "unconfirmed"}}
    evidence.observe(event)
    restored = review.AnswerEvidence.from_checkpoint_snapshot(
        evidence.checkpoint_snapshot(step_ids={"analysis"}), step_ids={"analysis"})
    assert [source["state"] for source in restored.render_sources() if source["source_id"].startswith("tool_0003")] == [status, status]


@pytest.mark.parametrize("change", [
    "extra_field", "version_bool", "version_unknown", "next_id_bool", "next_id_too_small", "negative_next_id",
    "wrong_step", "catalog_wrong_step", "call_wrong_step", "source_wrong_step", "duplicate_call",
    "duplicate_prefix", "bad_prefix", "wrong_source_id", "wrong_kind", "wrong_write_only", "source_extra",
    "long_text", "long_path", "private_host_path", "output_input_path", "path_traversal", "path_control",
    "too_many_inputs", "too_many_calls", "too_many_contexts", "fake_catalog_kind", "duplicate_context_inputs",
    "calling_success", "failed_with_inputs", "source_state_mismatch", "source_function_mismatch",
    "nonbool_truncated", "nonbool_discarded", "nonbool_called", "nonstring_text", "unknown_state",
])
def test_checkpoint_rejects_malformed_or_unbounded_evidence(change):
    snapshot = checkpoint_evidence().checkpoint_snapshot(step_ids={"analysis"})
    call = snapshot["calls"][0]
    source = call["sources"][1]
    if change == "extra_field": snapshot["draft"] = "PRIVATE_REJECTED_DRAFT"
    elif change == "version_bool": snapshot["version"] = True
    elif change == "version_unknown": snapshot["version"] = 2
    elif change == "next_id_bool": snapshot["next_id"] = True
    elif change == "next_id_too_small": snapshot["next_id"] = 1
    elif change == "negative_next_id": snapshot["next_id"] = -1
    elif change == "wrong_step": snapshot["current_step_id"] = "another-plan"
    elif change == "catalog_wrong_step": snapshot["contexts"][0]["step_id"] = "another-plan"
    elif change == "call_wrong_step": call["step_id"] = "another-plan"
    elif change == "source_wrong_step": source["step_id"] = "another-plan"
    elif change == "duplicate_call": snapshot["calls"][1]["call_id"] = call["call_id"]
    elif change == "duplicate_prefix": snapshot["calls"][1]["prefix"] = call["prefix"]
    elif change == "bad_prefix": call["prefix"] = "tool_00001"
    elif change == "wrong_source_id": source["source_id"] = "tool_9999_result"
    elif change == "wrong_kind": source["kind"] = "prior_review"
    elif change == "wrong_write_only": source["write_only"] = True
    elif change == "source_extra": source["draft"] = "PRIVATE_REJECTED_DRAFT"
    elif change == "long_text": source["text"] = "x" * (review.MAX_SOURCE_CHARS + 1)
    elif change == "long_path": call["inputs"] = ["/home/ubuntu/inputs/" + "x" * 1024]
    elif change == "private_host_path": call["inputs"] = ["/Users/private/source.csv"]
    elif change == "output_input_path": call["inputs"] = ["/home/ubuntu/output/analysis.csv"]
    elif change == "path_traversal": call["inputs"] = ["/home/ubuntu/inputs/../private.csv"]
    elif change == "path_control": call["inputs"] = ["/home/ubuntu/inputs/private\n.csv"]
    elif change == "too_many_inputs": call["inputs"] *= review.MAX_FILES + 1
    elif change == "too_many_calls": snapshot["calls"] *= review.MAX_CALLS
    elif change == "too_many_contexts": snapshot["contexts"] *= 5
    elif change == "fake_catalog_kind": snapshot["contexts"][0]["kind"] = "tool_result"
    elif change == "duplicate_context_inputs": snapshot["context_inputs"] *= 2
    elif change == "calling_success": call["called"] = False
    elif change == "failed_with_inputs":
        for item in call["sources"]: item["state"] = "failed"
    elif change == "source_state_mismatch": source["state"] = "failed"
    elif change == "source_function_mismatch": source["function"] = "another_tool"
    elif change == "nonbool_truncated": source["truncated"] = "false"
    elif change == "nonbool_discarded": snapshot["discarded"] = "false"
    elif change == "nonbool_called": call["called"] = "true"
    elif change == "nonstring_text": source["text"] = {"source": "PRIVATE_REJECTED_SOURCE"}
    elif change == "unknown_state": source["state"] = "invented_success"
    with pytest.raises(ValueError, match="^invalid_answer_evidence_checkpoint$"):
        review.AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"analysis"})


def test_checkpoint_retains_truncation_and_eviction_without_resurrecting_sources():
    evidence = checkpoint_evidence()
    for i in range(review.MAX_CALLS * 2):
        evidence.observe(tool(call=f"long-{i}", data={"output": "x" * review.MAX_SOURCE_CHARS}))
    snapshot = evidence.checkpoint_snapshot(step_ids={"analysis"})
    restored = review.AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"analysis"})
    assert restored.truncated and restored.render_sources() == evidence.render_sources()
    assert len(restored._calls) <= review.MAX_CALLS
    assert "tool_0001_result" not in {source["source_id"] for source in restored.render_sources()}


def test_checkpoint_cannot_store_historical_review_prose_as_execution_evidence():
    evidence = checkpoint_evidence()
    evidence._prior_reviews = [{"source_id": "prior_review_0001", "kind": "prior_review",
        "state": "historical", "text": "PRIVATE_HISTORICAL_PROSE", "truncated": False, "step_id": "old"}]
    evidence._historical_paths = {"/home/ubuntu/output/private_history.csv"}
    snapshot = evidence.checkpoint_snapshot(step_ids={"analysis"})
    assert "PRIVATE_HISTORICAL_PROSE" not in json.dumps(snapshot)
    assert "private_history.csv" not in json.dumps(snapshot)
    restored = review.AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"analysis"})
    assert restored.truncated and not restored._prior_reviews and not restored._historical_paths


@pytest.mark.asyncio
async def test_context_only_snapshot_cannot_prove_execution_on_delivery_retry():
    evidence = review.AnswerEvidence()
    evidence.observe_context({"description": "Registered observations"})
    evidence.begin_step("analysis")
    restored = review.AnswerEvidence.from_checkpoint_snapshot(
        evidence.checkpoint_snapshot(step_ids={"analysis"}), step_ids={"analysis"})
    result, _ = await run(response(paragraph("Analysis completed.", source="catalog_0001", quote="Registered observations")),
        events=[], evidence=restored)
    assert result.status == "unavailable" and result.metadata["reason"] == "unsupported_analysis"


def test_checkpoint_enforces_total_observation_budget():
    snapshot = checkpoint_evidence().checkpoint_snapshot(step_ids={"analysis"})
    template = snapshot["calls"][1]
    snapshot["calls"] = []
    for i in range(1, 10):
        call = copy.deepcopy(template)
        call.update(call_id=f"call-{i}", prefix=f"tool_{i:04d}")
        for item, suffix in zip(call["sources"], ("request", "result")):
            item.update(source_id=f"tool_{i:04d}_{suffix}", text="x" * review.MAX_SOURCE_CHARS)
        snapshot["calls"].append(call)
    snapshot["next_id"] = 9
    with pytest.raises(ValueError, match="^invalid_answer_evidence_checkpoint$"):
        review.AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"analysis"})


@pytest.mark.asyncio
@pytest.mark.parametrize("text,code", [
    ("[PRIVATE_LINK](/home/ubuntu/output/observed.png)", "active_link"),
    ("PRIVATE_PATH /Users/private/private.csv", "unrecognized_absolute_path"),
    ("PRIVATE_PATH /private", "unrecognized_root_path"),
    ("PRIVATE_PATH results/private.csv", "unrecognized_relative_path"),
    ("PRIVATE_IDENTIFIER `./x/y`", "unrecognized_relative_path"),
    ("PRIVATE_IDENTIFIER `private.customtype`", "unrecognized_identifier"),
])
async def test_file_reference_rejection_has_fixed_diagnostic_without_rejected_text(text, code):
    result, ask = await run(response(paragraph(text)))
    assert result.status == "unavailable"
    assert result.metadata["reason"] == "unverified_file_reference"
    assert result.metadata["citation_diagnostics"]["initial"] == {"file_reference_" + code: 1}
    assert result.metadata["citation_diagnostics"]["correction"] == {"correction_schema_root": 1}
    assert "PRIVATE_" not in json.dumps(result.metadata) + result.text
    assert "private.csv" not in json.dumps(result.metadata) + result.text
    assert "/Users" not in json.dumps(result.metadata) + result.text
    assert ask.await_count == 3


@pytest.mark.asyncio
async def test_ambiguous_file_diagnostic_does_not_relax_exact_identity_boundary():
    files = [file("/home/ubuntu/output/first/observed.png"), file("/home/ubuntu/output/second/observed.png")]
    result, _ = await run(response(paragraph("Delivered `observed.png`.", kind="delivery", source="verified_files", quote="observed.png")), files=files)
    assert result.metadata["reason"] == "unverified_file_reference"
    assert result.metadata["citation_diagnostics"]["initial"] == {"file_reference_ambiguous_basename": 1}
    assert result.metadata["citation_diagnostics"]["correction"] == {"correction_schema_root": 1}
    accepted, _ = await run(response(paragraph("Delivered `/home/ubuntu/output/first/observed.png`.",
        kind="delivery", source="verified_files", quote="observed.png")), files=files)
    assert accepted.status == "verified" and "/home/" not in accepted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["inputs/abcd/source.csv", "abcd/source.csv"])
async def test_observed_upload_accepts_exact_unique_namespace_alias(alias):
    result, ask = await run(response(paragraph(f"Read `{alias}`; observed mean is 3 mg.")), files=[],
        events=[tool("file_read", args={"file": "/home/ubuntu/inputs/abcd/source.csv"})])
    assert result.status == "verified" and result.text == "Read `source.csv`; observed mean is 3 mg."
    ask.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["datasets/example/tables/source.csv", "example/tables/source.csv", "tables/source.csv"])
async def test_observed_dataset_accepts_only_its_exact_unique_relative_path(alias):
    result, _ = await run(response(paragraph(f"Read `{alias}`; observed mean is 3 mg.")), files=[],
        events=[tool("file_read", args={"file": "/home/ubuntu/datasets/example/tables/source.csv"})])
    assert result.status == "verified" and "/" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["output/plots/observed.png", "plots/observed.png"])
async def test_verified_delivery_accepts_exact_unique_namespace_alias(alias):
    result, _ = await run(response(paragraph(f"Delivered `{alias}`.", kind="delivery", source="verified_files", quote="observed.png")),
        files=[file("/home/ubuntu/output/plots/observed.png")])
    assert result.status == "verified" and result.text == "Delivered `observed.png`."


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [
    "other/abcd/source.csv", "inputs/other/source.csv", "uploads/abcd/source.csv", "inputs/../abcd/source.csv",
    "../inputs/abcd/source.csv", "inputs/abcd/source.csv.bak", "otherinputs/abcd/source.csv",
    "/private/inputs/abcd/source.csv", "input/abcd/source.csv", "cd/source.csv", "未知inputs/abcd/source.csv",
])
async def test_observed_alias_never_authorizes_unknown_parent_namespace_or_longer_identity(alias):
    result, _ = await run(response(paragraph(f"Read `{alias}`.")), files=[],
        events=[tool("file_read", args={"file": "/home/ubuntu/inputs/abcd/source.csv"})])
    assert result.status == "unavailable" and result.metadata["reason"] == "unverified_file_reference"


@pytest.mark.asyncio
async def test_same_relative_alias_in_two_datasets_remains_ambiguous():
    events = [tool("file_read", call=f"read-{name}", args={"file": f"/home/ubuntu/datasets/{name}/tables/source.csv"})
              for name in ("alpha", "beta")]
    rejected, _ = await run(response(paragraph("Read `tables/source.csv`.")), events=events, files=[])
    assert rejected.status == "unavailable" and rejected.metadata["reason"] == "unverified_file_reference"
    accepted, _ = await run(response(paragraph("Read `alpha/tables/source.csv`.")), events=events, files=[])
    assert accepted.status == "verified" and accepted.text == "Read `source.csv`."


@pytest.mark.asyncio
async def test_relative_alias_collision_between_delivery_and_input_remains_ambiguous():
    events = [tool("file_read", args={"file": "/home/ubuntu/datasets/example/plots/observed.png"})]
    result, _ = await run(response(paragraph("Read `plots/observed.png`.")), events=events,
        files=[file("/home/ubuntu/output/plots/observed.png")])
    assert result.status == "unavailable" and result.metadata["reason"] == "unverified_file_reference"


@pytest.mark.asyncio
async def test_known_mime_and_known_leaf_do_not_authorize_an_arbitrary_relative_path():
    result, _ = await run(response(paragraph("Read `text/csv source.csv`.")), files=[],
        events=[tool("file_read", args={"file": "/home/ubuntu/inputs/abcd/source.csv"})])
    assert result.status == "unavailable" and result.metadata["reason"] == "unverified_file_reference"
