import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.event import MessageEvent
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.utils.tool_response_protocol import terminal_response_correction


def test_summary_and_step_have_distinct_terminal_contracts():
    agent = object.__new__(ExecutionAgent)
    response = AIMessage(content=json.dumps({"message": "4 rows checked; report saved.", "attachments": []}))
    assert agent._terminal_response_problem(response) == "invalid_execution_result"
    agent._summarizing = True
    assert agent._terminal_response_problem(response) is None
    for payload in [{"success": True, "result": "4 rows checked", "attachments": []},
                    {"message": "", "attachments": []}, {"message": "Result", "attachments": "file"}]:
        assert agent._terminal_response_problem(AIMessage(content=json.dumps(payload))) == "invalid_summary_result"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_summarize_uses_summary_contract_and_restores_step_mode(fail):
    agent = object.__new__(ExecutionAgent)
    agent._current_plan = None
    agent.reset_context = AsyncMock()
    agent._parse_json = AsyncMock(side_effect=AssertionError("No model repair after execution"))

    async def execute(request):
        assert agent._summarizing is True
        if fail:
            raise RuntimeError("Transport failure")
        response = '{"message":"Checked four rows.","attachments":["/home/ubuntu/output/report.json"]}'
        assert agent._terminal_response_problem(AIMessage(content=response)) is None
        yield MessageEvent(message=response)

    agent.execute = execute
    if fail:
        with pytest.raises(RuntimeError):
            _ = [event async for event in agent.summarize()]
    else:
        events = [event async for event in agent.summarize()]
        assert events[0].message == "Checked four rows."
        assert events[0].attachments[0].file_path == "/home/ubuntu/output/report.json"
    assert agent._summarizing is False
    agent._parse_json.assert_not_awaited()


def test_summary_correction_does_not_request_execution_result_or_new_work():
    correction = terminal_response_correction("invalid_summary_result")
    assert "message string" in correction
    assert "Do not repeat analysis" in correction
    assert "with success" not in correction
