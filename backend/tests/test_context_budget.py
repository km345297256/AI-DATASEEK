from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.domain.services import context_budget
from app.domain.services.context_budget import (
    ContextBudgetExceeded,
    ContextBudgetFailure,
    TOKEN_COUNTS_ARE_ESTIMATES,
    TOKEN_ESTIMATOR_VERSION,
    estimate_context_tokens,
    prepare_context,
)


@pytest.fixture(autouse=True)
def stable_private_hmac(monkeypatch):
    key = b"context-budget-test-key"

    def digest(value):
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    monkeypatch.setattr(context_budget, "private_identity_hmac", digest)


def _call(call_id: str, *, argument: str = "bounded") -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "dataset_read",
        "args": {"value": argument},
        "id": call_id,
    }])


def _result(call_id: str, content: str) -> ToolMessage:
    return ToolMessage(
        tool_call_id=call_id,
        name="dataset_read",
        content=content,
    )


def _snapshot(messages):
    return [message.model_dump(mode="json") for message in messages]


@pytest.mark.parametrize("exchange_count", [10, 100, 1000])
def test_preparation_estimation_work_is_linear_and_exact(monkeypatch, exchange_count):
    messages = [SystemMessage(content="system"), HumanMessage(content="current request")]
    for index in range(exchange_count):
        messages.extend([_call(f"call-{index}"), _result(f"call-{index}", "数" * 1500)])
    before = _snapshot(messages)
    schema = {"name": "read", "description": "schema" * 1000, "parameters": {"type": "object"}}
    actual_estimate = context_budget._estimate_message
    actual_tool_estimate = context_budget.estimate_tool_tokens
    counts = {"message": 0, "schemas": 0}

    def estimate(message, **kwargs):
        counts["message"] += 1
        return actual_estimate(message, **kwargs)

    def estimate_tools(schemas):
        counts["schemas"] += 1
        return actual_tool_estimate(schemas)

    monkeypatch.setattr(context_budget, "_estimate_message", estimate)
    monkeypatch.setattr(context_budget, "estimate_tool_tokens", estimate_tools)
    prepared = prepare_context(messages, tool_schemas=[schema], response_format={"type": "json_object"},
                               capacity_tokens=exchange_count * 200 + 3000,
                               max_output_tokens=300, safety_tokens=100, max_tool_text_tokens=250)
    assert counts["schemas"] == 1
    assert counts["message"] <= len(messages) + 2 * exchange_count
    assert prepared.input_tokens_after == estimate_context_tokens(
        prepared.messages, tool_schemas=[schema], response_format={"type": "json_object"},
    )[0]
    assert prepared.records
    assert _snapshot(messages) == before


def test_estimator_is_versioned_local_and_handles_english_and_chinese():
    english, _ = estimate_context_tokens([HumanMessage(content="a" * 300)])
    chinese, _ = estimate_context_tokens([HumanMessage(content="数" * 300)])

    assert TOKEN_ESTIMATOR_VERSION == "utf8_bytes_div3_v1"
    assert TOKEN_COUNTS_ARE_ESTIMATES is True
    assert english > 0
    assert chinese > english
    prepared = prepare_context(
        [SystemMessage(content="系统"), HumanMessage(content="analyze 数据")],
        capacity_tokens=2_000,
        max_output_tokens=200,
        safety_tokens=100,
    )
    assert prepared.estimator_version == TOKEN_ESTIMATOR_VERSION
    assert prepared.is_estimate is True
    assert prepared.input_tokens_before == prepared.input_tokens_after


def test_tool_schemas_and_response_format_are_part_of_input_estimate():
    messages = [SystemMessage(content="system"), HumanMessage(content="request")]
    bare, bare_tools = estimate_context_tokens(messages)
    with_tools, tool_tokens = estimate_context_tokens(
        messages,
        tool_schemas=[{
            "name": "inspect_data",
            "description": "Inspect a dataset",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }],
        response_format={"type": "json_object"},
    )

    assert bare_tools == 0
    assert tool_tokens > 0
    assert with_tools > bare + tool_tokens


def test_giant_fixed_tool_schema_fails_instead_of_deleting_current_request():
    messages = [SystemMessage(content="system"), HumanMessage(content="keep exactly")]
    before = _snapshot(messages)
    with pytest.raises(ContextBudgetExceeded) as raised:
        prepare_context(
            messages,
            tool_schemas=[{
                "name": "huge_schema",
                "description": "x" * 30_000,
                "parameters": {"type": "object"},
            }],
            capacity_tokens=2_000,
            max_output_tokens=200,
            safety_tokens=100,
        )

    assert raised.value.retryable is False
    assert raised.value.reason == ContextBudgetFailure.FIXED_CONTEXT_TOO_LARGE
    assert _snapshot(messages) == before


def test_images_use_an_independent_reserve_not_embedded_payload_bytes():
    short = HumanMessage(content=[{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,a"},
    }])
    large = HumanMessage(content=[{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + "a" * 200_000},
    }])
    text_only = HumanMessage(content="")

    short_tokens, _ = estimate_context_tokens([short], image_tokens=1_500)
    large_tokens, _ = estimate_context_tokens([large], image_tokens=1_500)
    text_tokens, _ = estimate_context_tokens([text_only], image_tokens=1_500)
    assert short_tokens == large_tokens
    assert short_tokens - text_tokens >= 1_500


def test_large_unknown_content_block_fails_closed():
    message = HumanMessage(content=[{
        "type": "future_binary_block",
        "payload": "x" * 2_000,
    }])
    with pytest.raises(ContextBudgetExceeded) as raised:
        prepare_context(
            [message],
            capacity_tokens=10_000,
            max_output_tokens=500,
            safety_tokens=100,
        )
    assert raised.value.reason == ContextBudgetFailure.UNKNOWN_CONTENT_BLOCK


def test_completed_tool_text_is_bounded_and_spill_reference_is_preserved():
    locator = "spill://artifact/" + "a" * 32
    raw_tool_text = "head\n" + "x" * 6_000 + locator + "y" * 6_000 + "\ntail"
    messages = [
        SystemMessage(content="system instructions"),
        HumanMessage(content="latest request must remain exact"),
        _call("call-latest"),
        _result("call-latest", raw_tool_text),
    ]
    before = _snapshot(messages)

    prepared = prepare_context(
        messages,
        capacity_tokens=1_300,
        max_output_tokens=200,
        safety_tokens=100,
        max_tool_text_tokens=300,
    )

    assert prepared.input_tokens_after <= prepared.input_limit
    assert prepared.input_tokens_after < prepared.input_tokens_before
    assert [record.kind for record in prepared.records] == ["tool_text_compacted"]
    compacted = prepared.messages[-1]
    assert compacted.type == "tool"
    assert compacted.tool_call_id == "call-latest"
    assert locator in compacted.content
    assert "historical tool output omitted" in compacted.content
    assert prepared.messages[0].type == "system"
    assert prepared.messages[0].content == "system instructions"
    assert prepared.messages[1].content == "latest request must remain exact"
    assert _snapshot(messages) == before
    assert raw_tool_text not in json.dumps(
        [record.model_dump(mode="json") for record in prepared.records]
    )


def test_old_complete_exchanges_are_omitted_as_units_and_latest_is_retained():
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="current request"),
    ]
    for index in range(3):
        messages.extend([
            _call(f"call-{index}"),
            _result(f"call-{index}", f"result-{index}:" + "z" * 3_000),
        ])
    before = _snapshot(messages)

    prepared = prepare_context(
        messages,
        capacity_tokens=1_800,
        max_output_tokens=200,
        safety_tokens=100,
        max_tool_text_tokens=10_000,
    )

    assert prepared.input_tokens_after <= prepared.input_limit
    assert any(
        record.kind == "completed_exchange_omitted"
        for record in prepared.records
    )
    assert prepared.messages[0].type == "system"
    assert sum(message.type == "system" for message in prepared.messages) == 1
    assert any(
        message.type == "human" and message.content == "current request"
        for message in prepared.messages
    )
    assert any(
        message.type == "ai"
        and not message.tool_calls
        and "no system authority" in str(message.content)
        for message in prepared.messages
    )

    latest_ai_index = next(
        index
        for index, message in enumerate(prepared.messages)
        if message.type == "ai"
        and message.tool_calls
        and message.tool_calls[0]["id"] == "call-2"
    )
    latest_tool = prepared.messages[latest_ai_index + 1]
    assert latest_tool.type == "tool"
    assert latest_tool.tool_call_id == "call-2"
    assert "result-2:" in latest_tool.content
    assert _snapshot(messages) == before


def test_multi_tool_exchange_is_never_partially_removed():
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="request"),
        AIMessage(content="", tool_calls=[
            {"name": "one", "args": {}, "id": "one"},
            {"name": "two", "args": {}, "id": "two"},
        ]),
        ToolMessage(tool_call_id="two", name="two", content="a" * 3_000),
        ToolMessage(tool_call_id="one", name="one", content="b" * 3_000),
        _call("newest"),
        _result("newest", "c" * 3_000),
    ]
    prepared = prepare_context(
        messages,
        capacity_tokens=1_600,
        max_output_tokens=200,
        safety_tokens=100,
        max_tool_text_tokens=10_000,
    )

    retained_ids = {
        message.tool_call_id
        for message in prepared.messages
        if message.type == "tool"
    }
    assert not ({"one", "two"} & retained_ids)
    assert "newest" in retained_ids
    assert any(
        record.kind == "completed_exchange_omitted"
        and record.message_count == 3
        for record in prepared.records
    )


def test_pending_tool_call_and_latest_request_are_never_truncated_or_removed():
    pending = _call("pending", argument="x" * 12_000)
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="latest exact request"),
        pending,
    ]
    before = _snapshot(messages)

    with pytest.raises(ContextBudgetExceeded) as raised:
        prepare_context(
            messages,
            capacity_tokens=1_500,
            max_output_tokens=200,
            safety_tokens=100,
            max_tool_text_tokens=100,
        )
    assert raised.value.reason == ContextBudgetFailure.FIXED_CONTEXT_TOO_LARGE
    assert _snapshot(messages) == before


def test_huge_latest_user_request_raises_nonretryable_typed_error():
    request = "必须完整保留" + "数" * 8_000
    messages = [SystemMessage(content="system"), HumanMessage(content=request)]

    with pytest.raises(ContextBudgetExceeded) as raised:
        prepare_context(
            messages,
            capacity_tokens=2_000,
            max_output_tokens=200,
            safety_tokens=100,
        )
    assert raised.value.code == "context_budget_exceeded"
    assert raised.value.retryable is False
    assert messages[-1].content == request
