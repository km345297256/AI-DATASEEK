import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.interfaces.schemas.event import ToolSSEEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("result,expected", [
    (ToolResult(success=True), "succeeded"),
    (ToolResult(success=False, message="private failure"), "failed"),
    ({"success": False, "data": {"secret": "hidden"}}, "failed"),
    ({"data": {"success": True}}, None),
    ({"success": "true"}, None),
    (None, None),
])
async def test_public_tool_status_is_exact_result_not_terminal_event(result, expected):
    event = ToolEvent(tool_call_id="revision", tool_name="file", function_name="file_write",
                      function_args={"file": "/home/ubuntu/output/test.py"},
                      status=ToolStatus.CALLED, function_result=result)
    public = await ToolSSEEvent.from_event_async(event)
    assert public.data.execution_status == expected
    assert "private failure" not in public.model_dump_json()
    assert "hidden" not in public.model_dump_json()


@pytest.mark.asyncio
async def test_calling_event_does_not_publish_terminal_success():
    event = ToolEvent(tool_call_id="revision", tool_name="file", function_name="file_write",
                      function_args={}, status=ToolStatus.CALLING,
                      function_result=ToolResult(success=True))
    assert (await ToolSSEEvent.from_event_async(event)).data.execution_status is None
