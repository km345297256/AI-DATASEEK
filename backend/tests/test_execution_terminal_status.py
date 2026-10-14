from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import ErrorEvent, MessageEvent
from app.domain.models.session import SessionStatus
from app.domain.services.agent_task_runner import AgentTaskRunner


@pytest.mark.asyncio
async def test_terminal_error_closes_session_without_resuming_expired_input():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id = 'agent', 'session'
    runner._sandbox = SimpleNamespace(ensure_sandbox=AsyncMock())
    runner._session_repository = SimpleNamespace(add_event=AsyncMock(), update_status=AsyncMock())
    runner._capture_artifact_baseline = AsyncMock()
    runner._sync_message_attachments_to_sandbox = AsyncMock()
    runner._dataset_service = SimpleNamespace()
    runner._initialize_mcp_tool = AsyncMock()
    runner._artifact_baseline_paths = set()
    task = SimpleNamespace(
        input_stream=SimpleNamespace(is_empty=AsyncMock(return_value=False),
            pop=AsyncMock(return_value=('input', MessageEvent(role='user', message='Analyze').model_dump_json()))),
        output_stream=SimpleNamespace(put=AsyncMock(return_value='output')))
    advanced = []

    async def flow(_message):
        yield ErrorEvent(error='invalid_execution_result')
        advanced.append(True)
        raise AssertionError('Error has already closed the input lease')

    runner._run_flow = flow
    await runner.run(task)
    assert not advanced
    runner._session_repository.update_status.assert_awaited_with('session', SessionStatus.COMPLETED)
    assert runner._session_repository.add_event.await_count == 1
