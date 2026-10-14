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
        assert agent.tool_choice == "none"
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
    assert agent.tool_choice is None
    agent._parse_json.assert_not_awaited()


def test_summary_correction_does_not_request_execution_result_or_new_work():
    correction = terminal_response_correction("invalid_summary_result")
    assert "message string" in correction
    assert "Do not repeat analysis" in correction
    assert "with success" not in correction


@pytest.mark.asyncio
@pytest.mark.parametrize('summary', [False, True])
async def test_narrated_complete_receipt_is_decoded_without_model_repair(summary):
    agent = object.__new__(ExecutionAgent)
    agent._summarizing = summary
    payload = ({'message': 'Checked four rows.', 'attachments': []} if summary else
               {'success': True, 'result': 'Checked four rows.', 'attachments': ['/home/ubuntu/output/report.md']})
    raw = 'The report exists and has been verified. Let me return the JSON object.\n\n' + json.dumps(payload)
    assert agent._terminal_response_problem(AIMessage(content=raw)) is None
    if not summary:
        decoded = await agent._decode_execution_result(raw)
        assert decoded.result == payload['result']
        assert decoded.attachments == payload['attachments']


@pytest.mark.parametrize('raw', [
    'Ready. {"success":true,"result":"done","attachments":[]',
    'Ready. {"success":true,"result":"done","attachments":[]} trailing instructions',
    'Example {} {"success":true,"result":"done","attachments":[]}',
    'Invoke {"tool_calls":[{"name":"shell_run"}]}',
    '<tool_call>{"success":true,"result":"done","attachments":[]}',
    'Ready. {"success":true,"result":"done","attachments":[],"tool_calls":[]}',
])
def test_ambiguous_or_tool_shaped_narration_is_not_a_result(raw):
    agent = object.__new__(ExecutionAgent)
    assert agent._terminal_response_problem(AIMessage(content=raw)) is not None
