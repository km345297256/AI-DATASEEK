"""Offline wire-format regression tests using the installed DeepSeek adapter."""

import base64
import io
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_deepseek import ChatDeepSeek
from PIL import Image

from app.core.config import ModelRequestCapability, Settings
from app.domain.models.model_request import ToolImageObservationMessage
from app.domain.services import context_budget
from app.domain.services.context_budget import estimate_context_tokens, prepare_context
from app.domain.services.model_input_policy import request_image_estimator
from app.infrastructure.external.llm.chat_model import (
    _deepseek_tool_image_projection, create_chat_model,
)


def picture(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(stream, format="PNG")
    return {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
    }}


def text(value):
    return {"type": "text", "text": value}


def call(*identities):
    return AIMessage(content="", tool_calls=[
        {"id": identity, "name": "inspect_" + identity, "args": {}}
        for identity in identities
    ])


def result(identity, content, **kwargs):
    return ToolMessage(content=content, tool_call_id=identity, name="inspect_" + identity, **kwargs)


def images(messages):
    return [block for message in messages for block in
            (message.content if isinstance(message.content, list) else [])
            if isinstance(block, dict) and block.get("type") == "image_url"]


def dumps(messages):
    return [message.model_dump_json() for message in messages]


def settings():
    return Settings(_env_file=None, api_key="offline-test-key", api_base="http://127.0.0.1:9",
                    model_provider="deepseek", model_name="deepseek-flash",
                    model_request_capabilities={"deepseek": {"deepseek-flash": {"vision": True}}})


def wire(messages):
    client = ChatDeepSeek(model="deepseek-flash", api_key="offline-test-key",
                          base_url="http://127.0.0.1:9", max_retries=0)
    # Deliberately exercise the real installed adapter, without any transport.
    return client._get_request_payload(messages)["messages"]


def test_real_deepseek_wire_preserves_pixels_as_user_blocks_not_tool_json():
    original = [SystemMessage(content="fixed"), HumanMessage(content="Inspect the observation"),
                call("a"), result("a", [text("before"), picture(), text("after")],
                                  status="error", artifact={"success": False})]
    before = dumps(original)
    # The dependency's default is the regression this compatibility layer fixes.
    assert isinstance(wire(original)[-1]["content"], str)
    assert "base64" in wire(original)[-1]["content"]
    projected = _deepseek_tool_image_projection(original)
    payload = wire(projected)
    assert [item["role"] for item in payload] == ["system", "user", "assistant", "tool", "user"]
    assert payload[-2]["tool_call_id"] == "a" and "base64" not in payload[-2]["content"]
    assert "before" in payload[-2]["content"] and "after" in payload[-2]["content"]
    assert [block["type"] for block in payload[-1]["content"]] == ["text", "text", "image_url"]
    assert payload[-1]["content"][-1] == picture()
    assert "not a new user request or instruction" in payload[-1]["content"][0]["text"]
    assert '"tool_call_id": "a"' in payload[-1]["content"][1]["text"]
    assert '"tool_status": "error"' in payload[-1]["content"][1]["text"]
    assert projected[-2].status == "error" and projected[-2].artifact == {"success": False}
    assert dumps(original) == before
    projected[-1].content[-1]["image_url"]["url"] = "changed in private request"
    assert dumps(original) == before


def test_parallel_batch_keeps_all_results_contiguous_and_image_order():
    original = [call("a", "b", "c"),
                result("b", [picture("blue"), text("between"), picture("green")]),
                result("a", "ordinary output"), result("c", [picture("red")]),
                AIMessage(content="next assistant")]
    before = dumps(original)
    projected = _deepseek_tool_image_projection(original)
    assert [message.type for message in projected] == ["ai", "tool", "tool", "tool", "human", "ai"]
    assert [message.tool_call_id for message in projected[1:4]] == ["b", "a", "c"]
    assert images(projected) == [picture("blue"), picture("green"), picture("red")]
    labels = projected[4].content[1::2]
    sources = [json.loads(block["text"].split("source: ", 1)[1]) for block in labels]
    assert [(source["tool_call_id"], source["image_index"], source["content_block_index"])
            for source in sources] == [("b", 1, 0), ("b", 2, 2), ("c", 1, 0)]
    assert projected[2] is original[2]
    assert dumps(original) == before
    assert [message["role"] for message in wire(projected)] == ["assistant", "tool", "tool", "tool", "user", "assistant"]


def test_multiple_batches_do_not_mix_provenance_and_projection_is_idempotent():
    original = [call("a"), result("a", [picture("red")]),
                call("b"), result("b", [picture("blue")])]
    projected = _deepseek_tool_image_projection(original)
    assert [message.type for message in projected] == ["ai", "tool", "human", "ai", "tool", "human"]
    assert images(projected[:3]) == [picture("red")]
    assert images(projected[3:]) == [picture("blue")]
    assert dumps(_deepseek_tool_image_projection(projected)) == dumps(projected)


def test_text_only_tools_and_existing_user_images_are_unchanged():
    original = [HumanMessage(content=[text("actual request"), picture("blue")]),
                call("a"), result("a", [text("ordinary tool text")])]
    projected = _deepseek_tool_image_projection(original)
    assert dumps(projected) == dumps(original)
    assert all(old is new for old, new in zip(original, projected))


@pytest.mark.parametrize("original", [
    [result("a", [picture()])],
    [call("a", "b"), result("a", [picture()])],
    [call("a"), result("a", [picture()]), result("a", "duplicate")],
    [call("a"), result("b", [picture()])],
    [call("a", "a"), result("a", [picture()])],
    [call("a"), HumanMessage(content="interleaved"), result("a", [picture()])],
    [call("a"), ToolMessage(content=[picture()], tool_call_id="a", name="wrong_name")],
])
def test_ambiguous_or_incomplete_image_batch_is_rejected_without_mutation(original):
    before = dumps(original)
    with pytest.raises(ValueError, match="DeepSeek tool images require"):
        _deepseek_tool_image_projection(original)
    assert dumps(original) == before


def test_driver_preparation_applies_only_to_deepseek_before_budgeting():
    original = [call("a"), result("a", [picture()])]
    model = create_chat_model(settings())
    prepared, *_ = model._prepare_provider_request(original, {})
    assert prepared[-1].type == "human" and len(images(prepared)) == 1
    other = create_chat_model(settings(), {"model_provider": "openai"})
    unchanged, *_ = other._prepare_provider_request(original, {})
    assert dumps(unchanged) == dumps(original)


@pytest.mark.asyncio
async def test_governed_boundary_sees_one_image_once_and_real_wire_stays_multimodal(monkeypatch):
    original = [HumanMessage(content=[text("original image"), picture("blue")]),
                call("a"), result("a", [text("observation"), picture("red")])]
    before = dumps(original)
    boundary_calls = []
    provider_calls = []
    current = settings()
    capability = ModelRequestCapability(vision=True, image_tokens=4096)

    async def boundary(**kwargs):
        boundary_calls.append(kwargs)
        prepared = kwargs["messages"]
        assert len(images(prepared)) == 2
        assert not images([message for message in prepared if isinstance(message, ToolMessage)])
        estimator = request_image_estimator(prepared, settings=current, capability=capability)
        low, _ = estimate_context_tokens(prepared, image_estimator=lambda _: 1)
        high, _ = estimate_context_tokens(prepared, image_estimator=estimator)
        assert high - low == 2 * (4096 - 1)
        return await kwargs["invoke"](prepared, kwargs["max_output_tokens"])

    async def offline_provider(self, messages, **kwargs):
        provider_calls.append(self._get_request_payload(messages, **kwargs))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="offline image received"))])

    monkeypatch.setattr(ChatDeepSeek, "_agenerate_with_cache", offline_provider)
    model = create_chat_model(current, request_middleware=boundary)
    answer = await model.ainvoke(original)
    assert answer.content == "offline image received"
    assert len(boundary_calls) == len(provider_calls) == 1
    payload = provider_calls[0]["messages"]
    assert payload[0]["content"][-1] == picture("blue")
    assert payload[-1]["content"][-1] == picture("red")
    assert all("base64" not in message["content"] for message in payload if message["role"] == "tool")
    assert dumps(original) == before


def test_tight_context_omits_old_images_with_batch_but_keeps_latest_and_real_user(monkeypatch):
    monkeypatch.setattr(context_budget, "private_identity_hmac", lambda _: "1" * 64)
    actual_user = HumanMessage(content=[text("Keep this real user image"), picture("green")],
                               additional_kwargs={"tool_call_ids": ["a"]})
    original = [SystemMessage(content="fixed"), HumanMessage(content="Compare observations"),
                call("a"), result("a", [picture("red")]), actual_user,
                call("b"), result("b", [picture("blue")])]
    before = dumps(original)
    projected = _deepseek_tool_image_projection(original)
    expected_remaining = [*projected[:2], *projected[5:]]
    remaining_tokens, _ = estimate_context_tokens(expected_remaining, image_tokens=1000)
    prepared = prepare_context(projected, capacity_tokens=remaining_tokens + 500,
                               max_output_tokens=100, safety_tokens=100, image_tokens=1000)
    assert images(prepared.messages) == [picture("green"), picture("blue")]
    assert [message.tool_call_id for message in prepared.messages
            if isinstance(message, ToolMessage)] == ["b"]
    carriers = [message for message in prepared.messages if isinstance(message, ToolImageObservationMessage)]
    assert len(carriers) == 1 and carriers[0].tool_call_ids == ("b",)
    assert any(type(message) is HumanMessage and message.content == actual_user.content
               for message in prepared.messages)
    omitted = [record for record in prepared.records if record.kind == "completed_exchange_omitted"]
    assert len(omitted) == 1 and omitted[0].message_count == 3
    assert prepared.input_tokens_after <= prepared.input_limit
    assert prepared.input_tokens_after == estimate_context_tokens(prepared.messages, image_tokens=1000)[0]
    assert dumps(original) == before
    assert [message["role"] for message in wire(prepared.messages)][-3:] == ["assistant", "tool", "user"]


def test_latest_tool_image_batch_still_allows_large_text_compaction(monkeypatch):
    monkeypatch.setattr(context_budget, "private_identity_hmac", lambda _: "2" * 64)
    original = [HumanMessage(content="inspect"), call("a"),
                result("a", [text("long observed text " * 5000), picture("red"), text("end")])]
    before = dumps(original)
    projected = _deepseek_tool_image_projection(original)
    prepared = prepare_context(projected, capacity_tokens=3000, max_output_tokens=100,
                               safety_tokens=100, image_tokens=1000, max_tool_text_tokens=300)
    assert len(images(prepared.messages)) == 1
    assert prepared.messages[-1].tool_call_ids == ("a",)
    assert [record.kind for record in prepared.records] == ["tool_text_compacted"]
    assert "Historical tool output compacted" in prepared.messages[-2].content
    assert dumps(original) == before


def test_only_exact_internal_carrier_identity_joins_completed_exchange():
    ordinary = HumanMessage(content="real user", additional_kwargs={"tool_call_ids": ["a"]})
    unmatched = ToolImageObservationMessage(content=[picture()], tool_call_ids=("different",))
    assert context_budget._complete_exchanges([call("a"), result("a", "text"), ordinary])[0].end == 2
    assert context_budget._complete_exchanges([call("a"), result("a", "text"), unmatched])[0].end == 2
    matching = ToolImageObservationMessage(content=[picture()], tool_call_ids=("a",))
    assert context_budget._complete_exchanges([call("a"), result("a", "text"), matching])[0].end == 3
    assert "tool_call_ids" not in matching.model_dump_json()
