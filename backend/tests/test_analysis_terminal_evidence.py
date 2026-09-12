"""Terminal prose is provisional; runner delivery receipts remain authoritative.

Reuse the real runner/PlanActFlow/ExecutionAgent harness. Only model, storage,
and sandbox I/O are synthetic; no scripts, real datasets, or models are run.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.dataset import MountedDataset
from app.domain.models.event import DoneEvent, MessageEvent, StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents.execution import ExecutionAgent
from test_analysis_delivery_flow import artifact, runner_for
from test_analysis_repair_flow import collect, output, scenario, terminal_messages


UNVERIFIED = "UNVERIFIED_CLAIM: the analysis executed and proved a 97.3 percent improvement."
CONFIRMED_DRAFT = "Observed values were compared; the result is limited to the inspected sample."


def use_drafts(flow, drafts):
    """Change only model-authored prose, keeping the actual execution parser."""
    executor = flow.executor
    original = executor._execute_with_tool_scope
    position = 0

    async def execute(prompt, **kwargs):
        nonlocal position
        current = position
        position += 1
        async for event in original(prompt, **kwargs):
            if isinstance(event, MessageEvent) and not (event.metadata or {}).get("analysis_progress"):
                payload = json.loads(event.message)
                payload["result"] = drafts[current]
                event = event.model_copy(update={"message": json.dumps(payload)})
            yield event

    executor._execute_with_tool_scope = execute


def public_payload(events):
    return json.dumps([event.model_dump(mode="json") for event in events], ensure_ascii=False)


def assert_unverified_notice(text):
    assert "分析" in text and "核验" in text, "The omitted draft must be explained, not silently erased"


@pytest.mark.asyncio
async def test_terminal_decoder_is_local_and_never_calls_the_legacy_llm_json_repair():
    agent = object.__new__(ExecutionAgent)
    agent._parse_json = AsyncMock(side_effect=AssertionError("Decoding must not request another model answer"))
    agent.ask_with_messages = AsyncMock(side_effect=AssertionError("Decoding must remain local"))
    result = await agent._decode_execution_result(json.dumps({
        "success": True, "result": CONFIRMED_DRAFT, "attachments": [],
    }))
    assert result is not None and result.result == CONFIRMED_DRAFT
    agent._parse_json.assert_not_awaited()
    agent.ask_with_messages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "null", "", "```python\nprint('not executed')\n```",
    '{"success": true, "attachments": ["/home/ubuntu/output/not-created.png"]}',
    '{"tool_calls": [{"name": "shell_run", "arguments": {"command": "print(1)"}}]}',
])
async def test_no_substantive_terminal_result_cannot_be_created_by_a_hidden_repair(raw):
    agent = object.__new__(ExecutionAgent)
    agent._parse_json = AsyncMock(side_effect=AssertionError("No implicit model repair"))
    agent.ask_with_messages = AsyncMock(side_effect=AssertionError("No invented result"))
    assert await agent._decode_execution_result(raw) is None
    agent._parse_json.assert_not_awaited()
    agent.ask_with_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_execution_cannot_publish_success_claims_even_with_a_verified_partial_file():
    chart = output("partial.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}], unknown=True)
    use_drafts(flow, [UNVERIFIED])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1
    assert step.success is False and step.outcome.reason_code == "tool_execution_unknown"
    assert UNVERIFIED not in public_payload(events)
    assert_unverified_notice(step.result)
    delivered = [item for event in terminal_messages(events) for item in event.attachments or []]
    assert chart[1].file_id in {item.file_id for item in delivered}
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_saved_script_alone_does_not_prove_its_claimed_chart_or_numerical_findings():
    script = output("analysis.py", "code")
    runner, flow, step, message, state = scenario([[script], [script]], [{"kind": "image"}])
    use_drafts(flow, [UNVERIFIED, UNVERIFIED])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2  # One local repair, then no actual progress.
    assert step.success is False and step.outcome.missing
    assert UNVERIFIED not in public_payload(events)
    assert_unverified_notice(step.result)
    delivered = [item for event in terminal_messages(events) for item in event.attachments or []]
    assert script[1].file_id in {item.file_id for item in delivered}
    assert all(not item.filename.endswith(".png") for item in delivered)


@pytest.mark.asyncio
async def test_failed_required_content_keeps_verified_attachments_but_not_unverified_step_claims():
    chart = output("kept.png", "image")
    bad = output("required.csv", "table", valid=False, reason="inconsistent_table_width")
    runner, flow, step, message, state = scenario([[chart, bad], [chart, bad]], [{"kind": "table"}])
    use_drafts(flow, [UNVERIFIED, UNVERIFIED])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2
    assert step.outcome.status == "partial" and step.success is False
    assert UNVERIFIED not in public_payload(events)
    assert_unverified_notice(step.result)
    delivered = [item for event in terminal_messages(events) for item in event.attachments or []]
    assert chart[1].file_id in {item.file_id for item in delivered}
    assert bad[1].file_id not in {item.file_id for item in delivered}


@pytest.mark.asyncio
async def test_successful_delivered_step_keeps_its_substantive_answer_unchanged():
    chart = output("verified.png", "image")
    runner, flow, step, message, _ = scenario([[chart]], [{"kind": "image"}])
    use_drafts(flow, [CONFIRMED_DRAFT])
    events = await collect(runner, message)
    assert step.success is True and step.outcome.status == "succeeded"
    assert step.result == CONFIRMED_DRAFT
    assert any(event.message == CONFIRMED_DRAFT for event in terminal_messages(events))


@pytest.mark.asyncio
async def test_repair_pending_draft_stays_private_including_the_next_started_step_event():
    bad = output("data.csv", "table", valid=False, reason="inconsistent_table_width")
    fixed = output("data.csv", "table", digest="b")
    runner, flow, step, message, state = scenario([[bad], [fixed]], [{"kind": "table"}])
    use_drafts(flow, [UNVERIFIED, CONFIRMED_DRAFT])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2 and step.success is True
    assert state["messages"][1]._artifact_repair_context["previous_analysis"] == UNVERIFIED
    public = public_payload(events)
    assert UNVERIFIED not in public
    assert "previous_analysis" not in public
    assert any(event.message == CONFIRMED_DRAFT for event in terminal_messages(events))
    assert len([event for event in events if isinstance(event, StepEvent)
                and event.status in {StepStatus.COMPLETED, StepStatus.FAILED}]) == 1


@pytest.mark.asyncio
async def test_a_later_failed_step_does_not_erase_an_earlier_successful_step_or_its_delivery():
    chart = output("first-step.png", "image")
    bad = output("later.csv", "table", valid=False, reason="inconsistent_table_width")
    runner, flow, first, message, state = scenario([[chart], [chart, bad]], [{"kind": "image"}])
    # This flow explicitly lacks autonomous local repair; both ordinary step
    # transitions and the final runner boundary still execute unmodified.
    flow.supports_artifact_repair = False
    plan = flow._session_repository.get_events.return_value[0].plan
    later = Step(id="later", description="Produce the requested table", agent="execution",
                 inputs={"dataset_intent": "analysis", "artifact_policy": "required"},
                 deliverables=[DeliverableRequirement(kind="table")])
    plan.steps.append(later)
    message.deliverables = [DeliverableRequirement(kind="image"), DeliverableRequirement(kind="table")]
    use_drafts(flow, [CONFIRMED_DRAFT, UNVERIFIED])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2
    assert first.status == ExecutionStatus.COMPLETED and first.result == CONFIRMED_DRAFT
    assert later.success is False and later.outcome.status == "partial"
    assert UNVERIFIED not in public_payload(events)
    assert any(event.message == CONFIRMED_DRAFT for event in terminal_messages(events))
    assert chart[1].file_id in {item.file_id for event in terminal_messages(events) for item in event.attachments or []}


@pytest.mark.asyncio
async def test_detached_terminal_event_cannot_republish_its_stale_unverified_prose_after_finalization():
    chart = output("partial-copy.png", "image")
    runner, flow, step, message, _ = scenario([[chart]], [{"kind": "image"}], unknown=True)
    use_drafts(flow, [UNVERIFIED])
    original_run = flow.run

    async def detached_run(current_message):
        pending_draft = None
        async for event in original_run(current_message):
            if isinstance(event, StepEvent) and event.status in {StepStatus.COMPLETED, StepStatus.FAILED}:
                pending_draft = event.step.result
                # A serialized/copied event cannot rely on shared Python object
                # mutation to change the following independently created prose.
                yield event.model_copy(deep=True)
            elif isinstance(event, MessageEvent) and pending_draft:
                yield event.model_copy(update={"message": pending_draft})
                pending_draft = None
            else:
                yield event

    flow.run = detached_run
    events = await collect(runner, message)
    assert step.success is False
    assert UNVERIFIED not in public_payload(events)
    assert_unverified_notice(step.result)


@pytest.mark.asyncio
async def test_checkpoint_serializes_no_unverified_draft_before_final_body_replacement(monkeypatch):
    step = Step(id="deliver", success=True, result=UNVERIFIED,
                deliverables=[DeliverableRequirement(kind="image")],
                outputs={"model_execution_success": True,
                         "execution_outcome": {"code": "completed", "side_effect_state": "confirmed_terminal",
                                               "execution_evidence": {"replay_safe": False}}})
    pair = artifact("computed.png", "image", uploaded=False)
    runner = runner_for(step, [pair])
    captured = []

    async def save(*args, **kwargs):
        # Snapshot inside the awaited boundary, not after the shared plan is
        # later rewritten; an AsyncMock call argument alone would hide this bug.
        captured.append(args[5].model_dump(mode="json"))
        return "a" * 32

    saved = AsyncMock(side_effect=save)
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", saved)
    message = Message(message="Deliver the already computed figure", datasets=[MountedDataset(
        data_center_id="fixture", data_center_name="Synthetic", name="Synthetic",
        sandbox_path="/home/ubuntu/datasets/synthetic")])
    await runner._finalize_analysis_step(
        StepEvent(status=StepStatus.COMPLETED, step=step), message, [], source_seq=7)

    saved.assert_awaited_once()
    assert captured[0]["steps"][0]["result"] is None
    assert UNVERIFIED not in json.dumps(captured)
    assert step.outcome.reason_code == "delivery_failed" and step.outcome.can_resume
    assert UNVERIFIED not in step.result
    assert_unverified_notice(step.result)


@pytest.mark.asyncio
async def test_upload_only_resume_started_clears_draft_at_the_actual_yield_boundary():
    step = Step(id="deliver", success=False, status=ExecutionStatus.FAILED, result=UNVERIFIED,
                attachments=["/home/ubuntu/output/stale.png"],
                outputs={"model_execution_success": True})
    checkpoint = {"reason_code": "delivery_failed", "plan": Plan(steps=[step]).model_dump(),
                  "progress": {"verified_files": ["/home/ubuntu/output/verified.png"]}}
    runner = object.__new__(AgentTaskRunner)
    runner._flow = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("No model execution during upload-only resume")))
    assert runner._is_delivery_only_continuation(checkpoint)
    events = runner._resume_delivery_only(checkpoint)
    try:
        started = await anext(events)
        assert isinstance(started, StepEvent) and started.status == StepStatus.STARTED
        assert started.step.result is None and started.step.attachments == [] and not started.step.success
        assert UNVERIFIED not in started.model_dump_json()
        runner._flow.run.assert_not_called()
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_ordinary_resume_started_clears_draft_before_any_execution():
    runner, flow, step, message, state = scenario([[]], [{"kind": "image"}])
    step.result, step.attachments = UNVERIFIED, ["/home/ubuntu/output/stale.png"]
    step.status = ExecutionStatus.FAILED
    message._resume_checkpoint = {"progress": {"unfinished_steps": [{"id": step.id, "evidence": UNVERIFIED}]}}
    plan = flow._session_repository.get_events.return_value[0].plan
    events = flow.executor.execute_step(plan, step, message)
    try:
        started = await anext(events)
        assert isinstance(started, StepEvent) and started.status == StepStatus.STARTED
        assert started.step.result is None and started.step.attachments == [] and not started.step.success
        assert UNVERIFIED not in started.model_dump_json()
        assert state["prompts"] == []  # STARTED is not proof that a script ran.
        assert message._resume_checkpoint["progress"]["unfinished_steps"][0]["evidence"] == UNVERIFIED
    finally:
        await events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_missing", [False, True])
async def test_missing_artifact_is_completed_in_one_local_repair_without_publishing_the_first_draft(declared_missing):
    chart = output("newly-generated.png", "image")
    missing = output("newly-generated.png", "image", valid=False, reason="missing_artifact", uploaded=False)
    missing[0].update(sha256=None, size=None, diagnostics={})
    runner, flow, step, message, state = scenario([[missing] if declared_missing else [], [chart]], [{"kind": "image"}])
    use_drafts(flow, [UNVERIFIED, CONFIRMED_DRAFT])
    events = await collect(runner, message)

    assert len(state["prompts"]) == 2 and state["drained"] == 2
    context = state["messages"][1]._artifact_repair_context
    assert context["previous_analysis"] == UNVERIFIED
    assert context["constraints"]["replay_original_step"] is False
    if declared_missing:
        assert context["failed_files"][0]["reason"] == "missing_artifact"
    assert step.success and step.outcome.status == "succeeded"
    assert UNVERIFIED not in public_payload(events)
    assert step.result == CONFIRMED_DRAFT
    assert chart[1].file_id in {item.file_id for event in terminal_messages(events)
                               for item in event.attachments or []}
    assert len([event for event in events if isinstance(event, StepEvent)
                and event.status in {StepStatus.COMPLETED, StepStatus.FAILED}]) == 1
