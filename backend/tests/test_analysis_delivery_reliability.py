"""Failures around a requested user result, using the real execution/repair flow."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.services.agent_task_runner import AgentTaskRunner
from test_analysis_repair_flow import collect, output, scenario, terminal_messages


@pytest.mark.asyncio
async def test_analysis_does_not_deliver_its_unrequested_implementation_script():
    chart, table = output("iris.png", "image"), output("iris.csv", "table")
    script = output("iris_analysis.py", "code")
    runner, _, step, message, state = scenario([[chart, table, script]], [
        {"kind": "image", "formats": ["png"]}, {"kind": "table", "formats": ["csv"]}])

    events = await collect(runner, message)

    assert step.success and len(state["prompts"]) == 1
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert {info.file_path for info in delivered} == {chart[0]["path"], table[0]["path"]}
    assert script[0]["path"] not in step.attachments
    assert not any(item["path"] == script[0]["path"]
                   for call in runner._sandbox.validate_artifacts.await_args_list for item in call.args[0])


@pytest.mark.asyncio
async def test_requested_reproduction_code_is_still_a_deliverable():
    script = output("reproduce.py", "code")
    runner, _, step, message, _ = scenario([[script]], [{"kind": "code", "formats": ["py"]}])
    events = await collect(runner, message)
    assert step.success
    assert any(info.file_path == script[0]["path"] for event in terminal_messages(events)
               for info in event.attachments or [])


@pytest.mark.asyncio
async def test_preserved_verified_chart_is_partial_when_followup_validation_is_unavailable():
    chart = output("kept.png", "image")
    bad = output("table.csv", "table", valid=False, reason="invalid_csv_syntax")
    fixed = output("table.csv", "table", digest="b")
    runner, _, step, message, state = scenario([[chart, bad], [chart, fixed]],
        [{"kind": "image"}, {"kind": "table"}])
    original = runner._sandbox.validate_artifacts.side_effect

    async def validate(items):
        if len(state["prompts"]) == 2:
            return SimpleNamespace(success=False, data={"version": 1, "files": []})
        return await original(items)

    runner._sandbox.validate_artifacts = AsyncMock(side_effect=validate)
    events = await collect(runner, message)

    assert len(state["prompts"]) == 2 and not step.success
    assert step.outcome.status == "partial"
    assert step.outcome.reason_code == "validation_unavailable"
    assert [item.kind for item in step.outcome.missing] == ["table"]
    assert all(issue.kind != "image" or not issue.blocking for issue in step.outcome.issues)
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert {info.file_id for info in delivered} == {chart[1].file_id}
    assert "本次分析部分完成" in step.result


@pytest.mark.asyncio
async def test_stale_auxiliary_tool_error_does_not_override_complete_verified_delivery():
    chart = output("figure.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}])
    original = flow.executor._execute_with_tool_scope

    async def execute(prompt, **kwargs):
        async for event in original(prompt, **kwargs):
            if isinstance(event, MessageEvent):
                flow.executor.last_execution_outcome["last_tool_error_code"] = "tool_execution_failed"
            yield event

    flow.executor._execute_with_tool_scope = execute
    await collect(runner, message)
    assert step.success and step.outcome.status == "succeeded"
    assert len(state["prompts"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["explicit", "discovered", "staged", "message"])
async def test_unrequested_script_is_withheld_before_storage_or_nonstep_publication(boundary):
    script = output("internal.py", "code")[1]
    runner = object.__new__(AgentTaskRunner)
    runner._requested_code_deliverables = []
    runner._generated_files = []
    runner._artifact_baseline_paths = set()
    runner._artifact_fingerprints = {}
    runner._sync_file_to_storage = AsyncMock(side_effect=AssertionError("Internal script must not be uploaded"))
    runner._read_artifact_with_fingerprint = AsyncMock(side_effect=AssertionError("Internal script must not be read"))
    runner._list_sandbox_artifacts = AsyncMock(return_value=[script.file_path])
    runner._sandbox_artifact_fingerprints = AsyncMock(return_value=({}, set()))
    if boundary == "explicit":
        result = await runner._sync_explicit_paths_to_storage([script.file_path])
    elif boundary == "discovered":
        result = await runner._sync_discovered_artifacts_to_storage()
    elif boundary == "staged":
        result = await runner._validate_staged_artifact_files([script])
    else:
        event = MessageEvent(message="done", attachments=[script])
        await runner._sync_message_attachments_to_storage(event)
        result = event.attachments
    assert result == []
    runner._sync_file_to_storage.assert_not_awaited()
    runner._read_artifact_with_fingerprint.assert_not_awaited()


@pytest.mark.parametrize("requirements,expected", [
    ([], False),
    ([{"kind": "code", "formats": ["py"]}], True),
    ([{"kind": "code", "formats": ["ipynb"]}], False),
    ([{"kind": "code", "output_paths": ["/home/ubuntu/output/deliver.py"]}], False),
    ([{"kind": "code", "output_paths": ["/home/ubuntu/output/helper.py"]}], True),
])
def test_code_delivery_respects_required_format_and_exact_identity(requirements, expected):
    runner = object.__new__(AgentTaskRunner)
    runner._requested_code_deliverables = [DeliverableRequirement.model_validate(item) for item in requirements]
    assert runner._is_deliverable_artifact("/home/ubuntu/output/helper.py") is expected
    assert runner._is_deliverable_artifact("/home/ubuntu/output/chart.png")


@pytest.mark.asyncio
async def test_deleted_protected_working_copy_keeps_original_upload_and_stops_repairs():
    kept = output("kept.png", "image")
    missing = output("kept.png", "image", valid=False, reason="missing_artifact", uploaded=False)
    missing[0].update(sha256=None, size=None)
    bad = output("data.csv", "table", valid=False, reason="invalid_csv_syntax")
    fixed = output("data.csv", "table", digest="b")
    runner, _, step, message, state = scenario([[kept, bad], [missing, fixed]],
        [{"kind": "image"}, {"kind": "table"}])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2 and not step.success
    assert step.outcome.status == "partial" and step.outcome.reason_code == "execution_failed"
    assert step.outcome.missing == []  # Both immutable uploaded deliverables remain available.
    assert runner._analysis_verified_receipts[step.id][kept[0]["path"]]["sha256"] == kept[0]["sha256"]
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert {info.file_id for info in delivered} == {kept[1].file_id, fixed[1].file_id}
    assert not step.outcome.can_resume and not runner._flow._artifact_repair_requests


@pytest.mark.asyncio
async def test_upload_failure_after_local_repair_keeps_only_already_delivered_receipts():
    kept = output("kept.png", "image")
    bad = output("data.csv", "table", valid=False, reason="invalid_csv_syntax")
    undelivered = output("data.csv", "table", digest="b", uploaded=False)
    runner, _, step, message, state = scenario([[kept, bad], [kept, undelivered]],
        [{"kind": "image"}, {"kind": "table"}])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2 and not step.success
    assert step.outcome.status == "partial" and step.outcome.reason_code == "delivery_failed"
    assert [item.kind for item in step.outcome.missing] == ["table"]
    assert set(runner._analysis_verified_receipts[step.id]) == {kept[0]["path"]}
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert {info.file_id for info in delivered} == {kept[1].file_id}
    runner._sync_file_to_storage.assert_awaited_once_with(undelivered[0]["path"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_followup_validation_exception_or_user_cancellation_never_loses_pinned_upload(cancelled):
    kept = output("kept.png", "image")
    bad = output("data.csv", "table", valid=False, reason="invalid_csv_syntax")
    fixed = output("data.csv", "table", digest="b")
    runner, _, step, message, state = scenario([[kept, bad], [kept, fixed]],
        [{"kind": "image"}, {"kind": "table"}])
    original = runner._sandbox.validate_artifacts.side_effect

    async def validate(items):
        if len(state["prompts"]) == 2:
            raise asyncio.CancelledError() if cancelled else OSError("validator connection closed")
        return await original(items)

    runner._sandbox.validate_artifacts.side_effect = validate
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await collect(runner, message)
    else:
        events = await collect(runner, message)
        assert step.outcome.status == "partial" and step.outcome.reason_code == "validation_unavailable"
        assert [item.kind for item in step.outcome.missing] == ["table"]
        assert kept[1].file_id in {info.file_id for event in terminal_messages(events) for info in event.attachments or []}
    assert len(state["prompts"]) == 2
    assert runner._analysis_verified_files[step.id][kept[0]["path"]].file_id == kept[1].file_id
    assert runner._analysis_verified_receipts[step.id][kept[0]["path"]]["sha256"] == kept[0]["sha256"]
