"""Host pre-dispatch decisions are not displayed as failed script executions."""
from datetime import UTC, datetime

import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.program_attempt import ProgramAttemptView
from app.domain.services.event_recording import create_event_recording, load_event_recording_jsonl
from app.interfaces.schemas.event import EventMapper, ToolSSEEvent


def event(**updates):
    return ToolEvent(tool_call_id="synthetic-call", tool_name="shell", function_name="program_run",
        function_args={"script_path": "/home/ubuntu/scripts/example.py"},
        function_result={"success": False, "blocked_by_policy": "program_prerequisites_not_ready"},
        status=ToolStatus.CALLED, **updates)


@pytest.mark.asyncio
async def test_host_block_survives_recording_and_sse_without_becoming_execution():
    source = event(dispatch_status="not_started")
    encoded = create_event_recording(session_id="synthetic-session", events=[source],
        captured_at=datetime(2026, 9, 30, tzinfo=UTC)).to_jsonl()
    restored = load_event_recording_jsonl(encoded)
    assert restored.to_jsonl() == encoded
    projected = await EventMapper.events_to_sse_events(list(restored.events))
    assert projected[0].data.execution_status == "not_started"
    assert projected[0].data.program_attempt is None
    assert "dispatch_status" not in projected[0].data.model_dump()


@pytest.mark.asyncio
async def test_tool_result_cannot_claim_host_no_dispatch():
    source = event()
    source.function_result.update(dispatch_status="not_started", side_effect_state="not_started")
    assert (await ToolSSEEvent.from_event_async(source)).data.execution_status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("success,returncode", [(True, 0), (False, 1)])
async def test_confirmed_exit_cannot_be_overwritten_by_stale_dispatch_metadata(success, returncode):
    state = "succeeded" if success else "failed"
    source = event(dispatch_status="not_started", program_attempt=ProgramAttemptView(
        identity="a" * 64, state=state, returncode=returncode))
    source.function_result = {"success": success}
    assert (await ToolSSEEvent.from_event_async(source)).data.execution_status == state


@pytest.mark.asyncio
async def test_calling_event_is_never_presented_as_completed_preflight():
    source = event(dispatch_status="not_started")
    source.status = ToolStatus.CALLING
    assert (await ToolSSEEvent.from_event_async(source)).data.execution_status is None
