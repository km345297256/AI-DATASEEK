"""Executable arguments must be observed in full, never inferred by a parser."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage

from app.domain.utils.robust_json_parser import RobustJsonParser, ToolCallParseError


def parser():
    value = object.__new__(RobustJsonParser)
    value._stage3_output_fixing = AsyncMock(return_value={"content": "invented completion"})
    return value


def invalid(raw, *, valid_calls=()):
    return AIMessage(content="", tool_calls=list(valid_calls), invalid_tool_calls=[{
        "name": "file_write", "args": raw, "id": "incomplete-write", "error": "private parser detail",
    }], response_metadata={"finish_reason": "length", "dataseek_model_usage_recorded": True},
       usage_metadata={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20})


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '{"file":"/home/ubuntu/output/report.md","content":"Interpretation depends on',
    '{"file":"/home/ubuntu/output/report.md","content":"complete value"',
    '{"content":"must not silently overwrite","append":',
    '{"rows":[1,2',
    '{"nested":{"rows":[{"value":1}',
    '{"content":"prefix","discarded',
    '```json\n{"content":"unfinished',
    '```json\n{"content":"unfinished}\n```',
    '```json\n{"content":"complete"}',
    '{"content":"one"} trailing private text',
    'prefix ```json\n{"content":"one"}\n```',
    '{"content":"one"} {"content":"two"}',
    '{"content":"first","content":"second"}',
    '{"nested":{"value":1,"value":2}}',
    '{"number":NaN}', '{"number":Infinity}', '{"number":-Infinity}', '{"number":1e999}',
    'null', '[{"content":"one"}]', '"content"',
])
async def test_incomplete_or_ambiguous_arguments_never_become_executable(raw):
    subject = parser()
    with pytest.raises(ToolCallParseError):
        await subject.ainvoke(invalid(raw))
    subject._stage3_output_fixing.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", [lambda s: s, lambda s: " \n" + s + "\t",
    lambda s: "```json\n" + s + "\n```", lambda s: "```\n" + s + "\n```"])
async def test_complete_json_is_preserved_losslessly_with_response_accounting(wrapper):
    arguments = {"file": "/home/ubuntu/output/report.md", "content": "No final punctuation |r\n```\n引号\"\\",
                 "nested": {"values": [0, False, None, ""]}, "append": False}
    original = invalid(wrapper(json.dumps(arguments, ensure_ascii=False)))
    subject = parser()
    result = await subject.ainvoke(original)
    assert result.tool_calls == [{"name": "file_write", "args": arguments,
                                 "id": "incomplete-write", "type": "tool_call"}]
    assert not result.invalid_tool_calls
    assert result.response_metadata == original.response_metadata
    assert result.usage_metadata == original.usage_metadata
    assert len(original.invalid_tool_calls) == 1 and not original.tool_calls
    subject._stage3_output_fixing.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_call_rejects_whole_batch_without_executing_valid_sibling():
    valid = {"name": "file_write", "args": {"file": "example.md", "content": "complete"}, "id": "valid"}
    subject = parser()
    with pytest.raises(ToolCallParseError) as caught:
        await subject.ainvoke(invalid('{"content":"secret body unfinished', valid_calls=[valid]))
    assert len(caught.value.invalid_message.tool_calls) == 1
    assert caught.value.invalid_message.tool_calls[0]["id"] == "valid"
    assert "secret body" not in str(caught.value)
    assert "private parser detail" not in str(caught.value)
    # A retry is still available to the existing bounded caller; no fake
    # tool result or inferred tool invocation is added by this parser.
    original_context = [HumanMessage(content='Write the requested report')]
    retry = caught.value.make_retry_context(original_context)
    assert retry[:-1] == original_context
    assert len(original_context) == 1
    assert all(isinstance(message, HumanMessage) for message in retry)
    assert "secret body" not in retry[-1].content
    assert "Never guess or fill in missing or truncated content" in retry[-1].content
    assert "smaller write/append calls" in retry[-1].content
    assert "Do not replay operations that already succeeded" in retry[-1].content


@pytest.mark.asyncio
async def test_cancellation_is_never_rewritten_as_json_failure():
    subject = parser()
    subject._stage1_partial_json = lambda _raw: (_ for _ in ()).throw(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await subject.ainvoke(invalid('{"content":"complete"}'))
    subject._stage3_output_fixing.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_empty_object_is_not_replaced_by_fallback():
    subject = parser()
    result = await subject.ainvoke(invalid('{}'))
    assert result.tool_calls[0]['args'] == {}
    subject._stage3_output_fixing.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_native_calls_keep_existing_metadata_and_identity():
    message = AIMessage(content='', tool_calls=[{'name': 'read', 'args': {}, 'id': 'native'}],
                        response_metadata={'dataseek_model_usage_recorded': True})
    subject = parser()
    assert await subject.ainvoke(message) is message
    subject._stage3_output_fixing.assert_not_awaited()
