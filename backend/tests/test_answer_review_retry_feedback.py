"""Review syntax feedback changes delivery, never frozen scientific scope."""
import asyncio
import copy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.domain.services.agents import execution as execution_module
from test_answer_review_json_protocol import PRIVATE_BODY, setup, transcript
from test_answer_review_transport_timeout import review
from test_model_retry_policy import failure


@pytest.fixture
def frozen_request(setup, monkeypatch):
    agent, driver, provider = setup
    execution_module.get_settings().llm_retry_attempts = 4
    payload = {"question": "Explain every authorized result", "sources": [{"id": "observed", "text": "frozen evidence"}],
               "paragraphs": ["required explanation"], "requirement_checks": [{"index": 0}],
               "report_targets": [{"report_id": "report_0001", "sha256": "a" * 64,
                                   "blocks": [{"block_id": 0, "text": "complete report tail"}]}],
               "accepted_paragraphs": [{"index": 0, "paragraph": "locked text"}]}
    messages = [SystemMessage(content="Return complete JSON under this immutable schema."),
                HumanMessage(content=json.dumps(payload))]
    original = copy.deepcopy(messages)

    async def invoke(*, ask, **_kwargs):
        return await ask(messages)

    monkeypatch.setattr("app.domain.services.analysis_answer_review.review_answer", invoke)
    return agent, driver, provider, messages, original


def assert_feedback(request, original):
    messages = request["messages"]
    assert messages[:2] == original and len(messages) == 3
    assert messages[-1].type == "human"
    text = messages[-1].content
    assert "complete, compact JSON object" in text
    assert "paragraph" in text and "requirement" in text and "report_checks" in text
    assert "locked" in text and "evidence" in text and "schema" in text
    assert "Do not truncate" in text and "Do not use tools" in text
    assert PRIVATE_BODY not in text and "secret-provider-key" not in text
    assert all(not getattr(message, "tool_calls", None) for message in messages)
    assert request["max_tokens"] == 4096 and request["response_format"] == {"type": "json_object"}
    assert not any(key in request for key in ("tools", "functions", "tool_choice"))


@pytest.mark.asyncio
async def test_four_bad_json_candidates_get_one_fixed_feedback_without_scope_loss(frozen_request):
    agent, _, provider, messages, original = frozen_request
    provider._responses = [AIMessage(content='{"private":"' + PRIVATE_BODY)] * 4
    with pytest.raises(json.JSONDecodeError):
        await review(agent)
    assert len(provider._requests) == 4 and provider._responses == []
    assert provider._requests[0]["messages"] == original
    for request in provider._requests[1:]:
        assert_feedback(request, original)
    assert transcript(provider._requests[1]) == transcript(provider._requests[2]) == transcript(provider._requests[3])
    assert messages == original
    agent.execute.assert_not_called(); agent._parse_json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), failure("0")])
async def test_transport_only_retry_does_not_invent_parse_feedback(frozen_request, error):
    agent, _, provider, messages, original = frozen_request
    provider._responses = [error, AIMessage(content='{"accepted":true}')]
    assert (await review(agent)).content == '{"accepted":true}'
    assert len(provider._requests) == 2
    assert all(request["messages"] == original for request in provider._requests)
    assert messages == original


@pytest.mark.asyncio
async def test_feedback_survives_transport_retry_but_never_accumulates(frozen_request):
    agent, _, provider, messages, original = frozen_request
    provider._responses = [AIMessage(content="{"), TimeoutError(), AIMessage(content="{"), AIMessage(content='{"accepted":true}')]
    assert (await review(agent)).content == '{"accepted":true}'
    assert len(provider._requests) == 4
    for request in provider._requests[1:]:
        assert_feedback(request, original)
    assert len({tuple(transcript(request)) for request in provider._requests[1:]}) == 1
    assert messages == original


@pytest.mark.asyncio
async def test_cancel_after_syntax_error_propagates_without_third_candidate(frozen_request):
    agent, _, provider, _, original = frozen_request
    provider._responses = [AIMessage(content="{"), asyncio.CancelledError(), AIMessage(content="{}")]
    with pytest.raises(asyncio.CancelledError):
        await review(agent)
    assert len(provider._requests) == 2 and len(provider._responses) == 1
    assert_feedback(provider._requests[1], original)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["[]", "true", '{"x":1,"x":2}', '{"x":NaN}'])
async def test_complete_but_invalid_response_shape_is_not_a_transport_retry(frozen_request, content):
    agent, _, provider, _, original = frozen_request
    provider._responses = [AIMessage(content=content), AIMessage(content="{}")]
    with pytest.raises(ValueError):
        await review(agent)
    assert len(provider._requests) == 1 and len(provider._responses) == 1
    assert provider._requests[0]["messages"] == original
