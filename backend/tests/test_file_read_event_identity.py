"""Completed read rendering must preserve the observation, not issue a new read."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import FileToolContent, ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.analysis_answer_review import AnswerEvidence
from app.domain.services.tools.file import FileToolkit
from app.interfaces.schemas.event import ToolSSEEvent


def runner(sandbox):
    value = AgentTaskRunner.__new__(AgentTaskRunner)
    value._agent_id = "read-fixture-agent"
    value._session_id = "read-fixture-session"
    value._sandbox = sandbox
    return value


def event(result, **args):
    return ToolEvent(tool_call_id="read-1", tool_name="file", function_name="file_read",
                     function_args={"file": "/home/ubuntu/input.csv", **args},
                     status=ToolStatus.CALLED, function_result=result)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,range_args", [
    ("row0,row1\n1,2\n", {}),
    ("range-only-v1", {"start_line": 3, "end_line": 5}),
    ("source-prefix(truncated)", {}),
    ("", {}),
])
async def test_real_tool_pipeline_and_event_display_perform_one_read(content, range_args):
    class VersionedSandbox:
        def __init__(self):
            self.calls = []

        async def file_read(self, file, **kwargs):
            self.calls.append({"file": file, **kwargs})
            return ToolResult(success=True, data={"file": file, "content":
                content if len(self.calls) == 1 else "DIFFERENT_FULL_FILE_V2"})

    sandbox = VersionedSandbox()
    args = {"file": "/home/ubuntu/input.csv", **range_args}
    result = await FileToolkit(sandbox).get_tool("file_read").ainvoke({"id": "read-1", "args": args})
    observed = event(result.artifact, **range_args)
    before = deepcopy(observed.function_result)
    evidence = AnswerEvidence(); evidence.begin_step("read-step"); evidence.observe(observed)
    evidence_before = evidence.render_sources()
    await runner(sandbox)._handle_tool_event(observed)
    assert len(sandbox.calls) == 1
    assert sandbox.calls[0]["start_line"] == range_args.get("start_line")
    assert sandbox.calls[0]["end_line"] == range_args.get("end_line")
    assert isinstance(observed.tool_content, FileToolContent)
    assert observed.tool_content.content == content
    assert observed.function_result == before
    assert evidence.render_sources() == evidence_before


@pytest.mark.asyncio
async def test_failed_original_read_cannot_be_replaced_by_later_success():
    class Sandbox:
        def __init__(self): self.calls = 0
        async def file_read(self, file, **kwargs):
            self.calls += 1
            return (ToolResult(success=False, message="fixture read failed", data=None)
                    if self.calls == 1 else ToolResult(success=True, data={"content": "NEW_SUCCESS"}))
    sandbox = Sandbox()
    result = await FileToolkit(sandbox).get_tool("file_read").ainvoke({
        "id": "read-1", "args": {"file": "/home/ubuntu/input.csv"}})
    observed = event(result.artifact)
    await runner(sandbox)._handle_tool_event(observed)
    assert sandbox.calls == 1
    assert observed.function_result.success is False
    assert observed.tool_content.content == "(File read failed)"
    public = await ToolSSEEvent.from_event_async(observed)
    assert public.data.execution_status == "failed"
    assert "NEW_SUCCESS" not in public.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_typed_and_legacy_dict_read_envelopes_keep_only_observed_content(wrapped):
    original = {"success": True, "message": "not file content", "data": {
        "content": "same-version", "file": "/Users/private/source.csv", "secret": "not-display"}}
    result = ToolResult(**original) if wrapped else deepcopy(original)
    sandbox = type("Sandbox", (), {"file_read": AsyncMock()})()
    observed = event(result)
    await runner(sandbox)._handle_tool_event(observed)
    assert observed.tool_content.content == "same-version"
    sandbox.file_read.assert_not_awaited()
    assert observed.function_result == result
    public = (await ToolSSEEvent.from_event_async(observed)).model_dump_json()
    assert "/Users/private" not in public and "not-display" not in public


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    None, {}, {"data": {"content": "unconfirmed"}}, {"success": "true", "data": {"content": "unconfirmed"}},
    {"success": True, "data": None}, {"success": True, "data": "not an envelope"},
    {"success": True, "data": {"content": None}}, {"success": True, "data": {"content": 23}},
])
async def test_missing_or_malformed_legacy_observation_is_not_refetched(result):
    sandbox = type("Sandbox", (), {"file_read": AsyncMock(return_value=ToolResult(
        success=True, data={"content": "LATER_CONTENT"}))})()
    observed = event(result)
    await runner(sandbox)._handle_tool_event(observed)
    sandbox.file_read.assert_not_awaited()
    assert observed.tool_content.content == "(No Content)"
    assert observed.function_result == result


@pytest.mark.asyncio
async def test_failed_envelope_cannot_publish_spurious_content_or_raw_error_fields():
    sandbox = type("Sandbox", (), {"file_read": AsyncMock()})()
    observed = event({"success": False, "message": "private /Users/private/source.csv",
                      "data": {"content": "NOT_A_SUCCESSFUL_OBSERVATION"}})
    await runner(sandbox)._handle_tool_event(observed)
    sandbox.file_read.assert_not_awaited()
    assert observed.tool_content.content == "(File read failed)"
    public = (await ToolSSEEvent.from_event_async(observed)).model_dump_json()
    assert "NOT_A_SUCCESSFUL_OBSERVATION" not in public and "/Users/private" not in public


@pytest.mark.asyncio
async def test_read_content_passes_existing_public_privacy_boundary():
    sandbox = type("Sandbox", (), {"file_read": AsyncMock()})()
    content = "value /Users/private/secret.csv\nAuthorization: Bearer private-token\n1,2"
    observed = event(ToolResult(success=True, data={"content": content}))
    await runner(sandbox)._handle_tool_event(observed)
    assert observed.tool_content.content == content
    public = (await ToolSSEEvent.from_event_async(observed)).model_dump_json()
    assert "/Users/private" not in public and "private-token" not in public
    assert "1,2" in public
    sandbox.file_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_read_still_uses_existing_event_and_public_limits():
    sandbox = type("Sandbox", (), {"file_read": AsyncMock()})()
    content = "界" * (1024 * 1024)
    observed = event(ToolResult(success=True, data={"content": content}))
    task = runner(sandbox)
    await task._handle_tool_event(observed)
    assert observed.tool_content.content == content
    bounded = task._bound_event_payload(observed)
    assert task._event_payload_size(bounded) <= task.MAX_EVENT_PAYLOAD_BYTES
    public = await ToolSSEEvent.from_event_async(bounded)
    assert len(public.model_dump_json().encode()) < 256 * 1024
    assert "truncated" in bounded.tool_content.content
    sandbox.file_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_file_write_display_behavior_is_unchanged():
    sandbox = type("Sandbox", (), {"file_read": AsyncMock(return_value=ToolResult(
        success=True, data={"content": "existing write display"}))})()
    observed = event(ToolResult(success=True, data={"bytes_written": 5}))
    observed.function_name = "file_write"
    await runner(sandbox)._handle_tool_event(observed)
    sandbox.file_read.assert_awaited_once_with("/home/ubuntu/input.csv")
    assert observed.tool_content.content == "existing write display"
