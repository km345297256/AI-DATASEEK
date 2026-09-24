"""The complete delivered report is a separate draft, never self-evidence."""
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.models.file import FileInfo
from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import StepEvent, StepStatus
from app.domain.services import analysis_answer_review as review
from app.domain.services.analysis_report_review import ReportTarget
from test_analysis_answer_review import paragraph, response, tool


PATH = "/home/ubuntu/output/report.md"
GOOD = "Observed mean is 3 mg."
BODY = "# Measured results\n\nmean=3; unit=mg\n"


def target(text=BODY, **changes):
    data = text.encode()
    values = dict(report_id="report_0001", size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                  text=text, reason="ready", read_complete=True, file_path=PATH, file_id="uploaded-version")
    return ReportTarget(**(values | changes))


def answer(messages, *, status="verified", mutation=None, quote="mean=3; unit=mg", source="tool_0001_result"):
    payload = json.loads(messages[-1].content)
    result = json.loads(response(paragraph(GOOD)).content)
    checks = []
    for item in payload["report_targets"]:
        if item["coverage"] != "full":
            continue
        checks.append({key: item[key] for key in ("report_id", "size", "sha256")} | {"blocks": [
            {"block_id": block["block_id"], "status": status,
             "evidence": [{"source_id": source, "quote": quote}] if status == "verified" else []}
            for block in item["blocks"]]})
    result["report_checks"] = checks
    if mutation:
        mutation(result)
    return json.dumps(result)


async def run(ask, *, reports=None, evidence=None, draft=GOOD):
    evidence = evidence or review.AnswerEvidence()
    if not evidence.render_sources():
        evidence.observe(tool())
    reports = (target(),) if reports is None else reports
    return await review.review_answer(ask=ask, question="Explain the observed data", draft=draft,
        files=[FileInfo(file_id="uploaded-version", file_path=PATH, filename="report.md", size=len(BODY.encode()))],
        evidence=evidence, report_targets=reports)


@pytest.mark.asyncio
async def test_whole_report_after_tool_source_limit_is_in_same_request_and_rejection_keeps_chat():
    body = BODY + ("Method context. " * 460) + "Unsupported lower bound at the end |r"
    ask = AsyncMock(side_effect=lambda messages: answer(messages, status="rejected"))
    result = await run(ask, reports=(target(body),))
    ask.assert_awaited_once()
    payload = json.loads(ask.await_args.args[0][-1].content)
    submitted = payload["report_targets"][0]
    assert "".join(block["text"] for block in submitted["blocks"]) == body
    assert submitted["blocks"][-1]["byte_end"] == len(body.encode())
    assert all("Unsupported lower bound" not in source["text"] for source in payload["sources"])
    assert result.status == "unavailable" and result.metadata["chat_review_status"] == "verified"
    assert result.metadata["reason"] == "report_validation_rejected" and result.text.startswith(GOOD)
    assert "Unsupported lower bound" not in result.text + json.dumps(result.metadata)
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["|r", "x / y", "no punctuation", "中文单位 mg/L"])
async def test_complete_report_ending_is_not_a_syntax_heuristic(ending):
    result = await run(AsyncMock(side_effect=answer), reports=(target(BODY + ending),))
    assert result.status == "verified" and result.metadata["report_review"]["status"] == "verified"
    record = result.metadata["report_review"]["reports"][0]
    assert record["unverified_ranges"] == [] and record["input_complete"]
    assert PATH not in json.dumps(result.metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("report_checks"),
    lambda value: value.update(report_checks=[]),
    lambda value: value["report_checks"].append(value["report_checks"][0]),
    lambda value: value["report_checks"][0].update(sha256="b" * 64),
    lambda value: value["report_checks"][0].update(size=True),
    lambda value: value["report_checks"][0]["blocks"].clear(),
    lambda value: value["report_checks"][0]["blocks"][0].update(block_id=True),
    lambda value: value["report_checks"][0]["blocks"][0].update(status=[]),
    lambda value: value["report_checks"][0]["blocks"][0].update(evidence=[]),
    lambda value: value["report_checks"][0]["blocks"][0].update(evidence="private"),
    lambda value: value["report_checks"][0].update(unknown_private_field="secret"),
])
async def test_missing_wrong_or_uncited_report_checks_fail_independently_without_new_call(mutation):
    ask = AsyncMock(side_effect=lambda messages: answer(messages, mutation=mutation))
    result = await run(ask)
    ask.assert_awaited_once()
    assert result.status == "unavailable" and result.text.startswith(GOOD)
    assert result.metadata["reason"] == "report_validation_unavailable"
    assert result.metadata["report_review"]["reports"][0]["unverified_ranges"]
    assert "secret" not in json.dumps(result.metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["request", "inventory", "same_report", "same_report_alias", "unknown_read"])
async def test_report_cannot_be_proven_by_itself_or_its_delivery(source_kind):
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    quote, source = "mean=3; unit=mg", "tool_0001_request"
    if source_kind == "request":
        evidence.observe(tool("file_write", call="save", args={"file": PATH, "content": BODY}))
        source = "tool_0002_request"
    elif source_kind == "inventory":
        source, quote = "verified_files", "report.md"
    else:
        path = {"same_report": PATH, "same_report_alias": "report.md", "unknown_read": None}[source_kind]
        evidence.observe(tool("file_read", call="read", args={"file": path}, data={"content": BODY}))
        source = "tool_0002_result"
    result = await run(AsyncMock(side_effect=lambda messages: answer(messages, source=source, quote=quote)), evidence=evidence)
    assert result.metadata["report_review"]["status"] == "unavailable"
    assert result.text.startswith(GOOD)


@pytest.mark.asyncio
async def test_byte_and_context_limits_never_send_a_prefix_or_accept_fabricated_coverage():
    body = "z" * (review.MAX_DRAFT_CHARS + review.MAX_EVIDENCE_CHARS)
    ask = AsyncMock(side_effect=answer)
    result = await run(ask, reports=(target(body),), draft="one character too many")
    submitted = json.loads(ask.await_args.args[0][-1].content)["report_targets"][0]
    assert submitted["blocks"] == [] and submitted["coverage"] == "unverified"
    assert result.metadata["report_review"]["reports"][0]["reason"] == "report_context_limit"
    assert result.metadata["report_review"]["reports"][0]["unverified_ranges"] == [[0, len(body)]]
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_exact_context_boundary_and_multiple_reports_keep_independent_states():
    body = "z" * (review.MAX_DRAFT_CHARS - len(GOOD))
    first = target(body)
    second = target("z" * (review.MAX_DRAFT_CHARS + review.MAX_EVIDENCE_CHARS), report_id="report_0002")
    ask = AsyncMock(side_effect=answer)
    result = await run(ask, reports=(first, second))
    states = result.metadata["report_review"]["reports"]
    assert states[0]["status"] == "verified" and states[1]["status"] == "unavailable"
    assert states[1]["reason"] == "report_context_limit"
    assert ask.await_count == 1


@pytest.mark.asyncio
async def test_schema_recovery_reuses_frozen_complete_report_and_does_not_add_a_report_call():
    initial = True
    def respond(messages):
        nonlocal initial
        if initial:
            initial = False
            return '{"bad":true}'
        return answer(messages, mutation=lambda result: result.update(answer_complete=True))
    ask = AsyncMock(side_effect=respond)
    result = await run(ask)
    assert result.status == "corrected" and ask.await_count == 2
    assert ask.await_args_list[0].args[0][-1] is ask.await_args_list[1].args[0][-1]
    assert "exactly five fields" in ask.await_args_list[1].args[0][0].content
    assert "exactly four fields" not in ask.await_args_list[1].args[0][0].content
    assert result.metadata["report_review"]["status"] == "verified"


@pytest.mark.asyncio
async def test_chat_citation_recovery_cannot_upgrade_rejected_report():
    def initial(messages):
        return answer(messages, status="rejected", mutation=lambda result: result["paragraphs"][0].update(
            evidence=[{"source_id": "tool_0001_result", "quote": "wrong quote"}]))
    def corrected(messages):
        payload = json.loads(messages[-1].content)
        assert "".join(item["text"] for item in payload["report_targets"][0]["blocks"]) == BODY
        excerpt = next(source for source in payload["sources"] if source["source_id"] == "tool_0001_result")["excerpts"][0]["evidence_id"]
        return json.dumps({"answer_complete": True, "paragraph_corrections": [{"index": 0, "paragraph": {
            "text": GOOD, "kind": "analysis", "evidence": [excerpt]}}], "requirement_corrections": []})
    calls = 0
    def respond(messages):
        nonlocal calls
        calls += 1
        return initial(messages) if calls == 1 else corrected(messages)
    result = await run(AsyncMock(side_effect=respond))
    assert calls == 2 and result.text.startswith(GOOD)
    assert result.metadata["reason"] == "report_validation_rejected"


@pytest.mark.asyncio
async def test_review_cancellation_propagates_without_another_request():
    ask = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run(ask)
    ask.assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_context_preparation_rejects_without_silently_dropping_report_blocks():
    from app.domain.services.context_budget import prepare_context, ContextBudgetExceeded
    submitted, provider_calls = [], []
    async def ask(messages):
        submitted.append(messages)
        prepare_context(messages, capacity_tokens=2048, max_output_tokens=512, safety_tokens=512)
        provider_calls.append(True)
        return answer(messages)
    result = await run(ask, reports=(target("完整研究正文 " * 1000),))
    assert len(submitted) == 1 and provider_calls == []
    assert result.status == "unavailable" and result.metadata["report_review"]["status"] == "unavailable"
    with pytest.raises(ContextBudgetExceeded):
        prepare_context(submitted[0], capacity_tokens=2048, max_output_tokens=512, safety_tokens=512)
    prepared = prepare_context(submitted[0], capacity_tokens=65536, max_output_tokens=4096, safety_tokens=2048)
    assert prepared.records == () and prepared.messages[-1].content == submitted[0][-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("report_status", ["rejected", "unclear"])
async def test_real_runner_binds_downloaded_version_preserves_chat_and_cannot_execute_again(report_status):
    from test_analysis_repair_flow import scenario, output, collect, terminal_messages
    from test_analysis_answer_review_flow import with_observed_results, real_reviewer
    from types import SimpleNamespace
    data = BODY.encode()
    digest = hashlib.sha256(data).hexdigest()
    record, file = output("report.md", "report", digest=digest)
    record["size"] = file.size = len(data)
    file.user_id = "fixture-user"
    file.metadata.update(source="sandbox_artifact", session_id="fixture-session", artifact_size=len(data))
    runner, flow, step, message, state = scenario([[(record, file)]], [{"kind": "report"}])
    runner._user_id = "fixture-user"
    runner._file_storage = SimpleNamespace(get_file_info=AsyncMock(return_value=file),
        download_file_range=AsyncMock(return_value=(data, file)))
    agent = with_observed_results(runner, flow, [GOOD], facts=["mean=3; unit=mg"])
    ask = AsyncMock(side_effect=lambda messages: answer(messages, status=report_status))
    real_reviewer(agent, ask)
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1 and ask.await_count == 1
    assert step.outcome.status == "partial"
    assert step.outcome.reason_code == ("report_validation_rejected" if report_status == "rejected" else "report_validation_unavailable")
    assert not step.outcome.can_resume and not flow._artifact_repair_requests
    assert GOOD in step.result and file.file_id in {item.file_id for item in runner._generated_files}
    assert "# Measured results" not in json.dumps([event.model_dump(mode="json") for event in events])
    runner._file_storage.download_file_range.assert_awaited_once_with(file.file_id, "fixture-user", offset=0, length=len(data))


@pytest.mark.asyncio
async def test_review_does_not_overwrite_an_existing_execution_failure():
    from test_analysis_repair_flow import scenario, output
    report = output("report.md", "report")
    runner, flow, step, message, _ = scenario([[report]], [{"kind": "report"}])
    flow.executor.review_delivery_answer = AsyncMock(return_value=review.AnswerReviewResult(
        GOOD, "unavailable", {"reason": "report_validation_unavailable"}))
    outcome = AnalysisOutcome(status="failed", reason_code="tool_execution_unknown")
    await runner._review_analysis_answer(step, message, [report[1]], step.deliverables, outcome)
    assert outcome.status == "failed" and outcome.reason_code == "tool_execution_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["file_id", "same_size_hash"])
async def test_runner_invalidates_review_if_final_attachment_changes_during_review(change):
    from test_analysis_repair_flow import scenario, output
    from types import SimpleNamespace
    data = BODY.encode()
    digest = hashlib.sha256(data).hexdigest()
    record, file = output("report.md", "report", digest=digest)
    record["size"] = file.size = len(data)
    file.user_id = "fixture-user"
    file.metadata.update(source="sandbox_artifact", session_id="fixture-session", artifact_size=len(data))
    runner, flow, step, message, _ = scenario([[(record, file)]], [{"kind": "report"}])
    runner._user_id = "fixture-user"
    runner._file_storage = SimpleNamespace(get_file_info=AsyncMock(return_value=file),
        download_file_range=AsyncMock(return_value=(data, file)))
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    def changed_response(messages):
        result = answer(messages)
        if change == "file_id":
            file.file_id = "different-upload-at-same-path"
        else:
            file.metadata["artifact_sha256"] = "b" * 64
        return result
    ask = AsyncMock(side_effect=changed_response)
    async def reviewed(**arguments):
        return await review.review_answer(ask=ask, **arguments)
    flow.executor.review_delivery_answer = reviewed
    outcome = AnalysisOutcome(status="succeeded", reason_code="completed")
    await runner._review_analysis_answer(step, message, [file], (), outcome, evidence=evidence)
    assert outcome.status == "partial" and outcome.reason_code == "report_validation_unavailable"
    assert step.outputs["answer_review"]["report_review"]["reports"][0]["reason"] == "delivery_version_changed"
    assert GOOD in step.result and ask.await_count == 1
