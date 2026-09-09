"""Non-step exits must obey the same exact-byte content delivery boundary."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services.dataset_request_resolver import ExecutionDecision, FrontControllerResolution, RequestDecision
from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, StepEvent, StepStatus, ToolEvent, ToolStatus, WaitEvent
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.models.safety import SafetyReview
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.flows.plan_act import AgentStatus


def artifact(name="observations.csv", kind="table", *, valid=True, digest="a", file_id=None):
    path = "/home/ubuntu/output/staged/" + name
    record = {"path": path, "kind": kind, "expected_kind": kind, "valid": valid,
              "sha256": digest * 64, "size": 20,
              "reason": "validated" if valid else "invalid_content"}
    info = FileInfo(file_id=file_id or name, filename=name, file_path=path, size=20,
                    metadata={"artifact_sha256": digest * 64})
    return record, info


def runner_for(records_and_files, events=(), *, summary=False):
    records = {item["path"]: item for item, _ in records_and_files}
    files = [info for _, info in records_and_files]

    async def validate(items):
        return SimpleNamespace(success=True, data={"version": 1, "files": [deepcopy(records[item["path"]]) for item in items]})

    class Flow:
        status = AgentStatus.SUMMARIZING if summary else AgentStatus.EXECUTING

        async def run(self, _message):
            for event in events:
                yield event

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = "stage-agent", "stage-session", "stage-owner"
    runner._flow = Flow()
    runner._front_controller_resolution = FrontControllerResolution(
        decision=RequestDecision(safety=SafetyReview(decision="allow", risk_level="low"),
                                 execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")),
        answer="", controller_metadata={})
    runner._generated_files = files[:]
    runner._sandbox = SimpleNamespace(validate_artifacts=AsyncMock(side_effect=validate))
    runner._record_safety_audit = AsyncMock()
    runner._initialize_mcp_tool = AsyncMock()
    runner._open_analysis_runtime = AsyncMock()
    runner._handle_tool_event = AsyncMock()
    runner._sync_discovered_artifacts_to_storage = AsyncMock(return_value=files)
    runner._sync_message_attachments_to_storage = AsyncMock()
    runner._sync_file_to_storage = AsyncMock(side_effect=AssertionError("Staged validation must not upload or repair"))
    runner._finalize_analysis_step = AsyncMock(side_effect=AssertionError("Non-step output must not declare task completion"))
    runner._review_artifact_repair = AsyncMock(side_effect=AssertionError("Non-step output must not start model repair"))
    return runner


async def run(runner):
    return [event async for event in runner._run_flow(Message(message="Analyze the measurements"))]


@pytest.mark.asyncio
async def test_progress_cannot_be_an_attachment_or_completion_bypass():
    bad = artifact("malformed.csv", valid=False)
    progress = MessageEvent(message="Working", attachments=[bad[1]], metadata={
        "analysis_progress": {"stage": "completing_results"},
        "analysis_outcome": {"status": "succeeded", "reason_code": "completed"},
    })
    runner = runner_for([bad], [progress])
    events = await run(runner)
    assert events == [progress]
    assert not progress.attachments
    assert "analysis_outcome" not in progress.metadata
    runner._sandbox.validate_artifacts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,name", [("image", "visualization.png"), ("table", "results.xlsx"),
                                     ("report", "findings.json"), ("code", "reproduce.py")])
async def test_staged_verification_accepts_each_supported_actual_artifact_kind(kind, name):
    item = artifact(name, kind)
    runner = runner_for([item])
    assert await runner._validate_staged_artifact_files([item[1]]) == [item[1]]
    runner._sync_file_to_storage.assert_not_awaited()
    runner._finalize_analysis_step.assert_not_awaited()
    runner._review_artifact_repair.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["invalid_content", "sha", "size", "file_id", "blank_file_id", "unsupported_kind",
                                  "bad_receipt_hash", "bool_size", "invalid_valid_flag", "metadata"])
async def test_staged_files_require_valid_content_and_exact_uploaded_identity(fault):
    record, info = artifact()
    if fault == "invalid_content":
        record["valid"] = False
    elif fault == "sha":
        info.metadata["artifact_sha256"] = "b" * 64
    elif fault == "size":
        info.size += 1
    elif fault == "file_id":
        info.file_id = None
    elif fault == "blank_file_id":
        info.file_id = " "
    elif fault == "unsupported_kind":
        record["kind"] = "image"
    elif fault == "bad_receipt_hash":
        record["sha256"] = info.metadata["artifact_sha256"] = "not-a-digest"
    elif fault == "bool_size":
        record["size"], info.size = True, 1
    elif fault == "invalid_valid_flag":
        record["valid"] = "true"
    elif fault == "metadata":
        info.metadata = None
    runner = runner_for([(record, info)])
    assert await runner._validate_staged_artifact_files([info]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["exception", "unavailable", "version", "bool_version", "missing", "duplicate", "foreign_path", "unhashable_path"])
async def test_staged_unavailable_or_malformed_batch_is_withheld(fault):
    entries = [artifact("first.csv"), artifact("second.csv")]
    runner = runner_for(entries)
    records = [deepcopy(record) for record, _ in entries]
    response = SimpleNamespace(success=True, data={"version": 1, "files": records})
    if fault == "exception":
        runner._sandbox.validate_artifacts = AsyncMock(side_effect=OSError("unavailable"))
    else:
        if fault == "unavailable":
            response.success = False
        elif fault == "version":
            response.data["version"] = 2
        elif fault == "bool_version":
            response.data["version"] = True
        elif fault == "missing":
            response.data["files"] = records[:1]
        elif fault == "duplicate":
            response.data["files"] = [records[0], records[0]]
        elif fault == "foreign_path":
            records[0]["path"] = "/home/ubuntu/output/not-requested.csv"
        else:
            records[0]["path"] = []
        runner._sandbox.validate_artifacts = AsyncMock(return_value=response)
    assert await runner._validate_staged_artifact_files([info for _, info in entries]) == []


@pytest.mark.asyncio
async def test_staged_duplicate_path_or_object_cannot_smuggle_mismatched_upload():
    record, matching = artifact()
    mismatch = matching.model_copy(update={"file_id": "wrong-bytes", "metadata": {"artifact_sha256": "b" * 64}})
    other_record, reused_object = artifact("second.csv", file_id=matching.file_id)
    runner = runner_for([(record, matching), (other_record, reused_object)])
    checked = await runner._validate_staged_artifact_files([mismatch, matching, reused_object])
    assert checked == [matching]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/Users/person/private.csv", "/home/ubuntu/output/../private.csv",
                                 "/home/ubuntu/output//data.csv", "/home/ubuntu/output/private\\data.csv",
                                 "/home/ubuntu/output/private\n.csv", "/home/ubuntu/datasets/source.csv",
                                 "/home/ubuntu/output/unsupported.zip"])
async def test_staged_verifier_does_not_request_unsafe_or_unsupported_paths(path):
    record, info = artifact()
    info.file_path = path
    runner = runner_for([(record, info)])
    assert await runner._validate_staged_artifact_files([info]) == []
    runner._sandbox.validate_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_staged_validation_keeps_other_successful_bounded_batches():
    entries = [artifact(f"file-{index:03}.csv") for index in range(33)]
    runner = runner_for(entries)
    valid = runner._sandbox.validate_artifacts.side_effect

    async def validate(items):
        assert len(items) <= 32
        if items[0]["path"].endswith("file-032.csv"):
            raise OSError("one unavailable batch")
        return await valid(items)

    runner._sandbox.validate_artifacts = AsyncMock(side_effect=validate)
    checked = await runner._validate_staged_artifact_files([info for _, info in entries])
    assert checked == [info for _, info in entries[:32]]
    assert runner._sandbox.validate_artifacts.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["wait", "error", "done", "summary", "message"])
async def test_every_non_step_boundary_withholds_bad_files_but_preserves_verified_files_and_real_events(boundary):
    good, bad = artifact("usable.csv"), artifact("malformed.csv", valid=False)
    if boundary == "wait":
        terminal = WaitEvent()
    elif boundary == "error":
        terminal = ErrorEvent(error="An execution error")
    elif boundary == "done":
        terminal = DoneEvent()
    else:
        terminal = MessageEvent(message="Measured findings and limitations remain intact.", attachments=[good[1], bad[1]])
    runner = runner_for([good, bad], [terminal], summary=boundary == "summary")
    events = await run(runner)
    assert terminal in events
    messages = [event for event in events if isinstance(event, MessageEvent)]
    assert [info for event in messages for info in event.attachments or []] == [good[1]]
    assert not any((event.metadata or {}).get("analysis_outcome") for event in messages)
    assert runner._flow._artifact_repair_requests == {}
    runner._review_artifact_repair.assert_not_awaited()
    runner._finalize_analysis_step.assert_not_awaited()
    if boundary in {"message", "summary"}:
        assert terminal.message == "Measured findings and limitations remain intact."
    else:
        assert "已保存的阶段性文件" in messages[0].message
        assert "已生成" not in messages[0].message
        assert events[-1] is terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [WaitEvent, DoneEvent, lambda: ErrorEvent(error="execution error")])
async def test_missing_validator_does_not_hide_wait_error_or_done(boundary):
    item = artifact()
    terminal = boundary()
    runner = runner_for([item], [terminal])
    runner._sandbox = SimpleNamespace()
    assert await run(runner) == [terminal]


@pytest.mark.asyncio
async def test_validation_cancellation_propagates_without_file_delivery_or_repair():
    item = artifact()
    runner = runner_for([item], [WaitEvent()])
    runner._sandbox.validate_artifacts = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run(runner)
    runner._review_artifact_repair.assert_not_awaited()
    runner._finalize_analysis_step.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_discovery_after_verified_step_cannot_reintroduce_invalid_file():
    original, invalid = artifact("original.png", "image"), artifact("late.csv", valid=False)
    runner = runner_for([original, invalid])
    step = Step(description="Measurements", success=True, result="Original measured results.")

    async def finalize(event, _message, _files, **_kwargs):
        event.step.outcome = AnalysisOutcome(status="succeeded", reason_code="completed")
        return [original[1]]

    async def flow(_message):
        yield StepEvent(step=step, status=StepStatus.COMPLETED)
        yield ToolEvent(tool_call_id="late-write", tool_name="file", function_name="file_write", function_args={}, status=ToolStatus.CALLED)
        runner._flow.status = AgentStatus.SUMMARIZING
        yield MessageEvent(message="Summary must not attach invalid late output.")
        yield DoneEvent()

    runner._flow.run = flow
    runner._finalize_analysis_step = finalize
    runner._sync_step_attachments_to_storage = AsyncMock(return_value=[original[1]])
    runner._sync_discovered_artifacts_to_storage = AsyncMock(side_effect=[[], [invalid[1]]])
    events = await run(runner)
    files = [info for event in events if isinstance(event, MessageEvent) for info in event.attachments or []]
    assert files == [original[1]]
    runner._sandbox.validate_artifacts.assert_awaited_once_with([{"path": invalid[1].file_path, "kind": "table"}])
