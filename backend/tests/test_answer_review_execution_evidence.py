"""Observed program versions and bounded correction of source selection."""
import copy
import hashlib
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain.messages import AIMessage

from app.domain.services import analysis_answer_review as review
from test_analysis_answer_review import tool, paragraph, response
from app.domain.models.tool_result import ToolResult
from app.domain.services.execution_evidence import ShellExecutionAttempt, ToolExecutionLedger, shell_command_digest
from app.domain.services.program_execution import consume_program_feedback, program_command, trusted_program_execution_feedback
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def bound_program(call, receipt):
    """Use the real adapter feedback parser, core tool identity and private ledger."""
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "answer-proof-sandbox"
    core = ShellToolkit(sandbox).get_tool("program_run")
    ledger = ToolExecutionLedger()
    args = call["args"]
    attempt = ShellExecutionAttempt(tool_call_id=call["id"], operation_id=uuid4().hex,
        sandbox_id=sandbox.id, shell_id=args["id"],
        command_digest=shell_command_digest(args["exec_dir"], program_command(args["script_path"], args["argv"])),
        query=AsyncMock())
    ledger.register(attempt)
    attempt.observe({"version": 1, "operation_id": attempt.operation_id,
        "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
        "state": "exited", "returncode": receipt["returncode"], "process_tree_quiescent": True})
    consume_program_feedback(ToolResult(success=True, data={"program_execution": {
        **receipt, "failure_fingerprint": None, "diagnostic": None, "output_truncated": False}}), attempt)
    return core, ledger


def program_evidence(*, newline=False, fault=None, trusted=True):
    evidence = review.AnswerEvidence()
    evidence.begin_step("plot")
    path = "/home/ubuntu/chart.py"
    content = "ax.scatter(data['length'], data['width'])\nax.set_xlabel('Length')"
    raw = content + ("\n" if newline else "")
    evidence.observe(tool("file_write", call="write", args={"file": path, "content": content,
        "trailing_newline": newline, "append": fault == "append"}, success=fault != "write_failed"))
    if fault == "different_step":
        evidence.begin_step("other")
    receipt = {"version": 1, "script_path": path, "source_digest": hashlib.sha256(raw.encode()).hexdigest(), "returncode": 0}
    if fault == "different_bytes":
        receipt["source_digest"] = "a" * 64
    if fault == "different_path":
        receipt["script_path"] = "/home/ubuntu/other.py"
    if fault == "failed":
        receipt["returncode"] = 1
    data = {"status": "running" if fault == "pending" else "completed", "returncode": receipt["returncode"],
            "output": "rows=150", "program_execution": receipt}
    call = {"name": "program_run", "id": "run", "args": {
        "script_path": path, "exec_dir": "/home/ubuntu", "id": "shell", "argv": []}}
    core, ledger = bound_program(call, receipt)
    proof = trusted_program_execution_feedback(core, call, None, ledger) if trusted else None
    event = tool("program_run", call="run", args=call["args"], data=data)
    if proof is None:
        evidence.observe(event)
    else:
        evidence.observe(event, trusted_program_execution=proof)
    return evidence


@pytest.mark.parametrize("newline", [False, True])
def test_only_exact_executed_source_version_is_linked_without_promoting_code(newline):
    evidence = program_evidence(newline=newline)
    sources = evidence.render_sources()
    assert sources[3]["executed_source_id"] == sources[0]["source_id"]
    assert sources[0]["kind"] == "tool_request" and sources[0]["write_only"]
    assert sources[1]["write_only"]
    # Derived links do not enter or enlarge checkpoint storage; restoration
    # independently reconstructs them from sealed observations.
    snapshot = evidence.checkpoint_snapshot(step_ids={"plot"})
    assert "executed_source_id" not in json.dumps(snapshot)
    assert review.AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"plot"}).render_sources() == sources


@pytest.mark.parametrize("fault", ["append", "write_failed", "different_step", "different_bytes", "different_path", "failed", "pending"])
def test_unconfirmed_changed_or_other_step_programs_never_link_saved_code(fault):
    assert not any("executed_source_id" in item for item in program_evidence(fault=fault).render_sources())


def test_source_annotations_are_private_copies_and_do_not_mutate_observations():
    evidence = program_evidence()
    before = copy.deepcopy(evidence._calls)
    sources = evidence.render_sources()
    sources[3]["executed_source_id"] = "invented"
    assert evidence._calls == before
    assert evidence.render_sources()[3]["executed_source_id"] == "tool_0001_request"


@pytest.mark.parametrize("field,value", [("source_digest", {}), ("script_path", []), ("version", True), ("returncode", False)])
def test_malformed_execution_receipt_neither_links_code_nor_crashes_publication(field, value):
    evidence = program_evidence(trusted=False)
    result = evidence._calls["run"]["sources"][1]
    data = json.loads(result["text"])
    data["data"]["program_execution"][field] = value
    result["text"] = json.dumps(data)
    assert not any("executed_source_id" in item for item in evidence.render_sources())


def test_public_receipt_cannot_attest_execution_or_gain_trust_through_checkpoint():
    evidence = program_evidence(trusted=False)
    assert not any("executed_source_id" in item for item in evidence.render_sources())
    snapshot = evidence.checkpoint_snapshot(step_ids={"plot"})
    assert "program_execution" not in snapshot["calls"][1]
    restored = review.AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"plot"})
    assert not any("executed_source_id" in item for item in restored.render_sources())


@pytest.mark.asyncio
async def test_failed_inspection_citation_can_use_other_successful_observation_without_new_execution():
    evidence = review.AnswerEvidence()
    evidence.observe(tool(call="failed", success=False, data={"output": "mean=7; later CLI absent", "returncode": 127}))
    evidence.observe(tool(call="measured", data={"output": "count=6 mean=7 min=2 max=12", "returncode": 0}))
    first = response(paragraph("Observed mean is 7.", quote="mean=7"))
    correction = AIMessage(content=json.dumps({"answer_complete": True, "paragraph_corrections": [{"index": 0, "paragraph": {
        "kind": "analysis", "text": "Observed mean is 7.", "evidence": ["tool_0002_result:excerpt_0001"]}}],
        "requirement_corrections": []}))
    ask = AsyncMock(side_effect=[first, correction])
    result = await review.review_answer(ask=ask, question="mean", draft="unverified", files=[], evidence=evidence)
    assert result.status == "corrected" and result.text == "Observed mean is 7."
    assert ask.await_count == 2
    assert result.metadata["citation_diagnostics"]["initial"] == {"unsupported_analysis": 1}
    payload = json.loads(ask.await_args.args[0][-1].content)
    assert "draft" not in payload and len(payload["failed_paragraphs"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("correction_kind,source", [("analysis", "tool_0001_result"), ("context", "tool_0002_result")])
async def test_correction_cannot_bless_failed_results_or_relabel_an_unsupported_calculation(correction_kind, source):
    evidence = review.AnswerEvidence()
    evidence.observe(tool(call="failed", success=False, data={"output": "mean=999"}))
    evidence.observe(tool(call="good", data={"output": "mean=7"}))
    first = response(paragraph("Observed mean is 999.", quote="mean=999"))
    correction = AIMessage(content=json.dumps({"paragraph_corrections": [{"index": 0, "paragraph": {
        "kind": correction_kind, "text": "Observed mean is 999.", "evidence": [source + ":excerpt_0001"]}}],
        "requirement_corrections": []}))
    ask = AsyncMock(side_effect=[first, correction])
    result = await review.review_answer(ask=ask, question="mean", draft="", files=[], evidence=evidence)
    assert result.status == "unavailable" and "999" not in result.text
    assert ask.await_count == 2
