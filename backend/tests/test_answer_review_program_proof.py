"""Program/source joins require the current executor's private launch receipt."""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.domain.models.event import MessageEvent, ToolStatus
from app.domain.models.plan import Step
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.analysis_answer_review import AnswerEvidence, review_answer
from app.domain.services.analysis_checkpoint import prepare_continuation
from app.domain.services.execution_evidence import ShellExecutionAttempt, ToolExecutionLedger
from app.domain.services.program_execution import trusted_program_execution_feedback
from test_analysis_answer_review import tool
from test_analysis_checkpoint import checkpoint_fixture
from test_analysis_repair_flow import collect, output, scenario
from test_answer_review_execution_evidence import bound_program, program_evidence


def program_fixture():
    path = "/home/ubuntu/output/analysis.py"
    content = "ax.scatter(data['length'], data['width'])"
    call = {"id": "executed-call", "name": "program_run", "args": {
        "id": "shell-a", "exec_dir": "/home/ubuntu/output", "script_path": path, "argv": []}}
    receipt = {"version": 1, "script_path": path, "source_digest": hashlib.sha256(content.encode()).hexdigest(),
               "returncode": 0}
    core, ledger = bound_program(call, receipt)
    write = tool("file_write", call="save-code", args={"file": path, "content": content})
    event = tool("program_run", call=call["id"], args=call["args"], data={
        "status": "completed", "returncode": 0, "output": "rows=150", "program_execution": receipt})
    event.tool_name = core.toolkit.name
    return call, core, ledger, write, event


def observing_runner(core, ledger, *, agent_key="execution"):
    runner = object.__new__(AgentTaskRunner)
    executor = SimpleNamespace(get_tool=Mock(return_value=core), _tool_execution_ledger=ledger)
    runner._flow = SimpleNamespace(plan=SimpleNamespace(steps=[Step(id="analysis", agent=agent_key)]),
        enabled_subagents={agent_key: SimpleNamespace(handler_type="execution")},
        executor=executor if agent_key == "execution" else SimpleNamespace(),
        _domain_agents={} if agent_key == "execution" else {agent_key: executor})
    runner._analysis_answer_evidence = AnswerEvidence()
    runner._analysis_answer_evidence.begin_step("analysis")
    return runner, executor


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_key", ["execution", "statistics"])
async def test_real_runner_flow_injects_private_source_proof_for_actual_step_executor(agent_key):
    chart = output("scatter.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image",
        "objective": "Plot the observed length and width"}], agent_key=agent_key)
    call, core, ledger, write, event = program_fixture()
    executor = flow._domain_agents.get(agent_key, flow.executor)
    executor.get_tool = Mock(return_value=core)
    executor._tool_execution_ledger = ledger
    runner._handle_tool_event = AsyncMock()
    runner._remember_private_tool_output = Mock()
    original = executor._execute_with_tool_scope
    payloads = []

    async def execute(prompt, **kwargs):
        async for item in original(prompt, **kwargs):
            if isinstance(item, MessageEvent):
                # The real execute_step creates a fresh ledger before entering
                # its tool scope; synthetic adapter receipts belong there.
                executor._tool_execution_ledger = ledger
                yield write
                yield event
            yield item

    async def ask(messages):
        payload = json.loads(messages[-1].content)
        payloads.append(payload)
        actual = next(source for source in payload["sources"] if source.get("executed_source_id"))
        saved = next(source for source in payload["sources"] if source["source_id"] == actual["executed_source_id"])
        assert saved["function"] == "file_write" and "ax.scatter" in saved["text"]
        assert saved["step_id"] == actual["step_id"] == step.id
        return json.dumps({"unsupported_claims": True, "paragraphs": [{"kind": "analysis",
            "text": "The observed execution plotted length and width for 150 rows.", "evidence": [
                {"source_id": actual["source_id"], "quote": "rows=150"},
                {"source_id": saved["source_id"], "quote": "ax.scatter"}]}],
            "requirement_checks": [{"index": 0, "status": "met", "evidence": [
                {"source_id": actual["source_id"], "quote": "rows=150"}]}]})

    async def review(**arguments):
        return await review_answer(ask=ask, **arguments)

    executor._execute_with_tool_scope = execute
    executor.review_delivery_answer = review
    events = await collect(runner, message)
    assert step.success and step.outcome.status == "succeeded" and len(payloads) == 1
    assert len(state["prompts"]) == state["drained"] == 1
    attempt = next(iter(ledger._attempts.values()))
    public = json.dumps([item.model_dump(mode="json") for item in events])
    assert attempt.operation_id not in public
    assert "executed_source_id" not in public and "answer_evidence" not in public
    assert attempt.operation_id not in json.dumps(payloads)
    attempt.query.assert_not_awaited()


@pytest.mark.parametrize("fault", [
    "no_ledger", "different_call", "different_sandbox", "different_shell", "different_arguments",
    "unconfirmed", "shadow_tool", "wrong_toolkit", "no_active_step", "other_executor", "calling",
])
def test_public_receipt_cannot_substitute_for_bound_current_executor_proof(fault):
    call, core, ledger, write, event = program_fixture()
    runner, executor = observing_runner(core, ledger, agent_key="statistics")
    attempt = next(iter(ledger._attempts.values()))
    if fault == "no_ledger": executor._tool_execution_ledger = None
    elif fault == "different_call": event.tool_call_id = "different-call"
    elif fault == "different_sandbox": attempt.sandbox_id = "another-sandbox"
    elif fault == "different_shell": event.function_args["id"] = "another-shell"
    elif fault == "different_arguments": event.function_args["argv"] = ["--not-the-observed-run"]
    elif fault == "unconfirmed": attempt.receipt = None
    elif fault == "shadow_tool": executor.get_tool.return_value = SimpleNamespace(toolkit=core.toolkit)
    elif fault == "wrong_toolkit": event.tool_name = "untrusted-plugin"
    elif fault == "no_active_step": runner._analysis_answer_evidence.begin_step("not-in-plan")
    elif fault == "other_executor":
        runner._flow.executor = executor
        runner._flow._domain_agents["statistics"] = SimpleNamespace(_tool_execution_ledger=ToolExecutionLedger())
    else:
        event.status = ToolStatus.CALLING
        event.function_result = None
    runner._observe_answer_tool_event(write)
    runner._observe_answer_tool_event(event)
    assert not any("executed_source_id" in source for source in runner._analysis_answer_evidence.render_sources())
    assert "program_execution" not in runner._analysis_answer_evidence._calls[event.tool_call_id]
    attempt.query.assert_not_awaited()


@pytest.mark.parametrize("new_attempt", ["unknown", "different_arguments", "different_shell", "different_sandbox", "no_feedback"])
def test_latest_call_attempt_never_inherits_an_earlier_confirmed_source_digest(new_attempt):
    call, core, ledger, _, _ = program_fixture()
    old = next(iter(ledger._attempts.values()))
    newer = ShellExecutionAttempt(tool_call_id=old.tool_call_id, operation_id=uuid4().hex,
        sandbox_id=old.sandbox_id, shell_id=old.shell_id, command_digest=old.command_digest, query=AsyncMock())
    ledger.register(newer)
    if new_attempt != "unknown":
        newer.observe({**old.receipt.model_dump(), "operation_id": newer.operation_id})
        newer.program_execution = copy.deepcopy(old.program_execution)
    if new_attempt == "different_arguments": newer.command_digest = "f" * 64
    elif new_attempt == "different_shell": newer.shell_id = "other-shell"
    elif new_attempt == "different_sandbox": newer.sandbox_id = "other-sandbox"
    elif new_attempt == "no_feedback": newer.program_execution = None
    assert trusted_program_execution_feedback(core, call, {"program_execution": old.program_execution}, ledger) is None


def test_private_proof_is_copied_and_public_receipt_does_not_override_real_digest():
    call, core, ledger, write, event = program_fixture()
    proof = trusted_program_execution_feedback(core, call, None, ledger)
    event.function_result.data["program_execution"]["source_digest"] = "f" * 64
    evidence = AnswerEvidence()
    evidence.begin_step("analysis")
    evidence.observe(write)
    evidence.observe(event, trusted_program_execution=proof)
    proof["source_digest"] = "a" * 64
    assert evidence.render_sources()[-1]["executed_source_id"] == "tool_0001_request"
    snapshot = evidence.checkpoint_snapshot(step_ids={"analysis"})
    restored = AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"analysis"})
    snapshot["calls"][-1]["program_execution"]["source_digest"] = "b" * 64
    assert restored.render_sources() == evidence.render_sources()


@pytest.mark.parametrize("field,value", [
    ("version", True), ("returncode", False), ("returncode", 1), ("script_path", "/home/ubuntu/other.py"),
    ("script_path", "/Users/private/script.py"), ("source_digest", "not-a-digest"), ("operation_id", "not-an-operation"),
])
def test_malformed_private_proof_cannot_link_or_roundtrip_through_checkpoint(field, value):
    evidence = program_evidence()
    snapshot = evidence.checkpoint_snapshot(step_ids={"plot"})
    snapshot["calls"][-1]["program_execution"][field] = value
    with pytest.raises(ValueError, match="invalid_answer_evidence_checkpoint"):
        AnswerEvidence.from_checkpoint_snapshot(snapshot, step_ids={"plot"})


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [False, True])
async def test_delivery_checkpoint_authenticates_private_execution_link_before_restoration(tamper):
    evidence = program_evidence()
    repository, sandbox, message, token = await checkpoint_fixture(reason_code="delivery_failed", answer_evidence=evidence)
    assert token and "program_execution" in repository.checkpoint["answer_evidence"]["calls"][-1]
    repository.checkpoint["claimed_by"] = "resume-input"
    message.resume_from, message.client_message_id = token, "resume-input"
    message._accepted_event_seq = 2
    if tamper:
        repository.checkpoint["answer_evidence"]["calls"][-1]["program_execution"]["source_digest"] = "f" * 64
        before = len(sandbox.calls)
        with pytest.raises(ValueError):
            await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
        assert len(sandbox.calls) == before
    else:
        checkpoint = await prepare_continuation(repository, sandbox, "session-a", "owner-a", message)
        restored = AnswerEvidence.from_checkpoint_snapshot(checkpoint["answer_evidence"], step_ids={"inspect", "plot"})
        assert restored.render_sources() == evidence.render_sources()
        assert restored.render_sources()[-1]["executed_source_id"] == "tool_0001_request"
