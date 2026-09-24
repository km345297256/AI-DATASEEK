"""A read-only reviewer cannot accept or retry malformed native tool requests."""
import json

import pytest
from langchain_core.messages import AIMessage

from app.domain.services.analysis_answer_review import _parse_response
from test_answer_review_json_protocol import PRIVATE_BODY, setup
from test_answer_review_transport_timeout import review


def invalid_call():
    return {"id": "bad-review-call", "name": "shell_run", "args": "{", "error": PRIVATE_BODY}


@pytest.mark.parametrize("content", ["{}", "{"])
@pytest.mark.parametrize("envelope", ["message", "mapping"])
def test_invalid_native_call_rejected_before_parsing_even_when_json_is_valid(content, envelope):
    value = (AIMessage(content=content, invalid_tool_calls=[invalid_call()]) if envelope == "message"
             else {"content": content, "invalid_tool_calls": [invalid_call()]})
    with pytest.raises(ValueError, match="^review_requested_tools$") as caught:
        _parse_response(value)
    assert not isinstance(caught.value, json.JSONDecodeError)
    assert PRIVATE_BODY not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{}", "{"])
async def test_real_driver_does_not_retry_or_execute_invalid_native_review_calls(setup, content):
    agent, _, provider = setup
    provider._responses = [AIMessage(content=content, invalid_tool_calls=[invalid_call()]), AIMessage(content="{}")]
    result = await review(agent)
    assert result.status == "unavailable"
    assert result.metadata["reason"] == "review_requested_tools"
    assert len(provider._requests) == 1 and len(provider._responses) == 1
    assert len(provider._requests[0]["messages"]) == 2
    assert PRIVATE_BODY not in result.text + json.dumps(result.metadata)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_called()
