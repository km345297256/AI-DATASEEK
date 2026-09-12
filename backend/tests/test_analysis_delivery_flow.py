"""Completion contracts must survive the real runner/event boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services.dataset_request_resolver import ExecutionDecision, FrontControllerResolution, RequestDecision
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.event import DoneEvent, MessageEvent, StepEvent, StepStatus, PlanEvent
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.safety import SafetyReview
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow


def artifact(name, kind, *, valid=True, uploaded=True, digest="a" * 64):
    path = "/home/ubuntu/output/" + name
    receipt = {"path": path, "kind": kind, "valid": valid, "sha256": digest, "size": 10}
    info = FileInfo(file_id=name if uploaded else None, filename=name, file_path=path, size=10,
                    metadata={"artifact_sha256": digest})
    return receipt, info


def runner_for(step, pairs):
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = "agent", "session", "owner"
    runner._artifact_baseline_paths = set()
    runner._generated_files = [item for _, item in pairs if item.file_id]
    receipts = {item["path"]: item for item, _ in pairs}
    runner._sandbox = SimpleNamespace(validate_artifacts=AsyncMock(side_effect=lambda items: SimpleNamespace(
        success=True, data={"version": 1, "files": [receipts[item["path"]] for item in items]})))
    runner._list_sandbox_artifacts = AsyncMock(return_value=list(receipts))
    runner._session_repository = SimpleNamespace()
    runner._flow = SimpleNamespace(plan=Plan(steps=[step]))
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("model_success", [True, False])
async def test_saved_script_cannot_complete_visualization(model_success):
    findings = "有效观测共 6 项，均值为 2.5，单位沿用输入元数据。可复现代码已保存。"
    step = Step(success=model_success, result=findings,
                inputs={"dataset_intent": "visualization", "artifact_policy": "required"},
                outputs={"execution_outcome": {"code": "tool_budget_exhausted"}})
    pair = artifact("analysis.py", "code")
    runner = runner_for(step, [pair])
    event = StepEvent(status=StepStatus.COMPLETED, step=step)
    files = await runner._finalize_analysis_step(event, Message(message="绘图"), [pair[1]])
    assert event.status == StepStatus.FAILED
    assert step.status == ExecutionStatus.FAILED and step.success is False
    assert step.outcome.status == "partial" and step.outcome.missing[0].kind == "image"
    assert step.outcome.can_resume is False  # no source snapshot
    assert step.result.startswith("本次分析部分完成。")
    # The script is verified, not the draft's measurement claims.
    assert findings not in step.result
    assert "分析说明尚未通过完整核验" in step.result
    assert not step.result.startswith("本次分析已完成")
    assert files == [pair[1]]  # actual script retained, never described as a plot


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["valid", "corrupt", "upload_failed", "hash_changed", "unknown"])
async def test_completion_requires_real_validated_uploaded_bytes_and_execution(mode):
    step = Step(success=True, deliverables=[DeliverableRequirement(kind="image", min_count=1)])
    pair = artifact("plot.png", "image", valid=mode != "corrupt", uploaded=mode != "upload_failed")
    if mode == "hash_changed":
        pair[1].metadata["artifact_sha256"] = "b" * 64
    if mode == "unknown":
        step.outputs["execution_outcome"] = {"has_unconfirmed_tool_execution": True}
    runner = runner_for(step, [pair])
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                         Message(message="图表"), [pair[1]] if pair[1].file_id else [])
    assert step.success is (mode == "valid")
    if mode == "upload_failed":
        assert step.outcome.reason_code == "delivery_failed"
    if mode == "unknown":
        assert not step.outcome.can_resume
        assert step.outcome.reason_code == "tool_execution_unknown"


@pytest.mark.asyncio
async def test_runner_never_repeats_success_prose_after_failed_delivery_contract():
    findings = "数据包含两个测量变量，观测值保持原始单位；可复现代码已保存。"
    step = Step(success=True, result=findings, deliverables=[DeliverableRequirement(kind="image")])
    pair = artifact("analysis.py", "code")
    runner = runner_for(step, [pair])

    class Flow:
        status = AgentStatus.EXECUTING
        plan = Plan(steps=[step])

        async def run(self, message):
            yield StepEvent(status=StepStatus.COMPLETED, step=step)
            yield MessageEvent(message=step.result)
            self.status = AgentStatus.SUMMARIZING
            yield MessageEvent(message="已全部完成，所有图表已交付。")
            yield DoneEvent()

    runner._flow = Flow()
    runner._front_controller_resolution = FrontControllerResolution(
        decision=RequestDecision(safety=SafetyReview(decision="allow", risk_level="low"),
                                 execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")), answer="", controller_metadata={})
    runner._record_safety_audit = AsyncMock()
    runner._initialize_mcp_tool = AsyncMock()
    runner._sync_step_attachments_to_storage = AsyncMock(return_value=[pair[1]])
    runner._sync_discovered_artifacts_to_storage = AsyncMock(return_value=[])
    runner._sync_message_attachments_to_storage = AsyncMock()
    events = [event async for event in runner._run_flow(Message(message="绘图"))]
    answers = [event for event in events if isinstance(event, MessageEvent)]
    assert len(answers) == 1
    assert answers[0].metadata["analysis_outcome"]["status"] == "partial"
    assert answers[0].message.startswith("本次分析部分完成。")
    assert findings not in answers[0].message
    assert "已全部完成，所有图表已交付。" not in answers[0].message
    assert answers[0].metadata["analysis_outcome"]["missing"][0]["kind"] == "image"
    assert answers[0].attachments == [pair[1]]


def test_task_checklist_is_bound_to_final_step_and_survives_replanning():
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.plan = Plan(steps=[Step(id="inspect"), Step(id="render")])
    message = Message(deliverables=[DeliverableRequirement(kind="image", min_count=3)])
    flow._bind_delivery_contract(message)
    assert not flow.plan.steps[0].deliverables
    assert flow.plan.steps[-1].deliverables[0].min_count == 3
    flow.plan.steps[-1] = Step(id="new", deliverables=[DeliverableRequirement(kind="image", min_count=1)])
    flow._bind_delivery_contract(message)
    assert flow.plan.steps[-1].deliverables[0].min_count == 3


@pytest.mark.asyncio
async def test_delivery_only_resume_does_not_call_executor_or_repeat_successful_steps():
    first = Step(id="read", success=True, status=ExecutionStatus.COMPLETED)
    second = Step(id="deliver", deliverables=[DeliverableRequirement(kind="image")],
                  outputs={"model_execution_success": True})
    checkpoint = {"reason_code": "delivery_failed", "plan": Plan(steps=[first, second]).model_dump(),
                  "progress": {"verified_files": ["/home/ubuntu/output/plot.png"]}}
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._flow = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("must not run model")))
    assert runner._is_delivery_only_continuation(checkpoint)
    events = [event async for event in runner._resume_delivery_only(checkpoint)]
    assert {event.step.id for event in events if isinstance(event, StepEvent)} == {"deliver"}
    assert any(isinstance(event, PlanEvent) for event in events)
    runner._flow.run.assert_not_called()
    checkpoint["plan"]["steps"].append(Step(id="not_started").model_dump())
    assert not runner._is_delivery_only_continuation(checkpoint)


@pytest.mark.asyncio
async def test_copied_step_delivery_failure_updates_authoritative_state_without_unsanitizing_inputs():
    step = Step(id="preview", success=True, status=ExecutionStatus.COMPLETED,
                result="The file is ready.", inputs={"target_file": "private/nested/image.png"},
                deliverables=[DeliverableRequirement(kind="image")])
    pair = artifact("image.png", "image", uploaded=False)
    runner = runner_for(step, [pair])
    public_step = step.model_copy(deep=True)
    public_step.inputs["target_file"] = "image.png"
    event = StepEvent(status=StepStatus.COMPLETED, step=public_step)

    files = await runner._finalize_analysis_step(event, Message(message="Preview the image"), [])

    assert files == []
    authoritative = runner._flow.plan.steps[0]
    assert authoritative is step and event.step is not authoritative
    for value in (authoritative, event.step):
        assert value.success is False and value.status == ExecutionStatus.FAILED
        assert value.outcome.reason_code == "delivery_failed"
        assert value.result != "The file is ready."
        assert value.error == "delivery_failed"
    assert event.status == StepStatus.FAILED
    assert authoritative.inputs["target_file"] == "private/nested/image.png"
    assert event.step.inputs["target_file"] == "image.png"


@pytest.mark.asyncio
async def test_unlimited_accounting_does_not_disable_safe_delivery_checkpoint(monkeypatch):
    from app.domain.models.dataset import MountedDataset
    from app.domain.services.model_runtime import analysis_budget_scope

    step = Step(id="deliver", success=True, deliverables=[DeliverableRequirement(kind="image")],
                outputs={"model_execution_success": True,
                         "execution_outcome": {"code": "completed", "side_effect_state": "confirmed_terminal",
                             "execution_evidence": {"replay_safe": False}}})
    pair = artifact("plot.png", "image", uploaded=False)
    runner = runner_for(step, [pair])
    handle = SimpleNamespace(snapshot=AsyncMock(return_value=SimpleNamespace(
        deadline_at=None, hard_limit=None, model_call_limit=None, model_token_limit=None,
        tool_batches_used=200, model_calls=300, charged_tokens=2_000_000)))
    save = AsyncMock(return_value="a" * 32)
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", save)
    message = Message(message="deliver the already computed image", datasets=[MountedDataset(
        data_center_id="test", data_center_name="Test", name="Synthetic",
        sandbox_path="/home/ubuntu/datasets/synthetic")])
    with analysis_budget_scope(handle):
        await runner._finalize_analysis_step(
            StepEvent(status=StepStatus.COMPLETED, step=step), message, [], source_seq=7)

    assert step.outcome.reason_code == "delivery_failed"
    assert step.outcome.can_resume is True
    assert step.outcome.resume_from == "a" * 32
    save.assert_awaited_once()
    handle.snapshot.assert_awaited_once()
