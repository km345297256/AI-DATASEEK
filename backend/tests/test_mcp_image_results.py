"""Synthetic MCP pixels only: no model, network, user files or live storage."""
import asyncio
import base64
import copy
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import ToolMessage
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

from app.core.config import Settings
from app.domain.models.spill import SpillArtifactOwner, SpillArtifactSource, SpillImageSaveRequest
from app.domain.services.tools import mcp as mcp_module
from app.domain.services.tools.mcp import MCPToolkit, _MCPToolWrapper
from app.domain.services.tools.mcp_images import (
    MCP_IMAGE_REFS_KEY, MCPImageContext, accepted_image_refs, bound_mcp_image_result,
    image_tool_message, mcp_image_memory_message,
)
from app.domain.services.tools.pipeline import ToolExecutionContext, ToolExecutionInterceptor, ToolExecutionPipeline
from app.domain.services.tools.spill import SpillArtifactInterceptor
from app.domain.services.tools.spill_projection import projected_tool_artifact
from app.infrastructure.external.file.spill import FileStorageSpillArtifactStore
from test_mcp_protocol_results import manager_with_result
from test_spill_artifact_store import InMemoryFileStorage, InMemorySpillRepository

OWNER = SpillArtifactOwner(user_id="owner", session_id="session")


def png(color="red", *, size=(32, 24), format="PNG", frames=False):
    buf = io.BytesIO()
    image = Image.new("RGB", size, color)
    kwargs = {"save_all": True, "append_images": [Image.new("RGB", size, "blue")]} if frames else {}
    image.save(buf, format=format, **kwargs)
    return buf.getvalue()


def image(raw=None, *, mime="image/png"):
    return ImageContent(type="image", data=base64.b64encode(raw or png()).decode(), mimeType=mime)


def settings(**kwargs):
    return Settings(_env_file=None, model_provider="test", model_name="vision",
        mcp_image_results_enabled=True,
        model_request_capabilities={"test": {"vision": {"vision": True, "image_tokens": 100}}}, **kwargs)


def environment(*, config=None, owner=OWNER):
    files, repository = InMemoryFileStorage(), InMemorySpillRepository()
    store = FileStorageSpillArtifactStore(files, repository, identity_key="synthetic-tests-only")
    context = MCPImageContext(store=store, owner=owner, provider="test", model_name="vision", settings=config or settings())
    return context, store, files, repository


async def result_with(context, blocks=None, *, is_error=False):
    manager, session = await manager_with_result(CallToolResult(isError=is_error,
        content=blocks if blocks is not None else [TextContent(type="text", text="before"), image(), TextContent(type="text", text="after")]))
    manager._image_context = context
    result = await manager.call_tool("mcp_demo_query", {})
    return result, session


@pytest.mark.asyncio
async def test_real_image_blocks_are_request_only_and_recover_across_instances():
    ctx, store, files, repository = environment()
    result, session = await result_with(ctx)
    message = image_tool_message(result, tool_call_id="call", name="mcp_demo_query")
    durable = mcp_image_memory_message(message)
    serialized = durable.model_dump_json()
    assert "base64" not in serialized and "internal-storage" not in serialized
    assert "base64" not in result.model_dump_json()
    fresh = MCPImageContext(store=store, owner=OWNER, provider="test", model_name="vision", settings=settings())
    restored = ToolMessage.model_validate_json(serialized)
    request = await fresh.expand_image_messages([restored], provider="test", model_name="vision")
    assert [block["type"] for block in request[0].content] == ["text", "text", "image_url", "text"]
    assert request[0].content[2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert MCP_IMAGE_REFS_KEY not in request[0].additional_kwargs
    assert restored.model_dump_json() == serialized
    assert session.call_tool.await_count == 1
    assert files.full_downloads == 0
    record = next(iter(repository.records.values()))
    assert record.storage_user_id != OWNER.user_id
    with pytest.raises(PermissionError):
        await files.download_file(record.storage_file_id, OWNER.user_id)


@pytest.mark.asyncio
async def test_mcp_error_with_an_image_stays_error():
    ctx, *_ = environment()
    result, _ = await result_with(ctx, is_error=True)
    message = image_tool_message(result, tool_call_id="c", name="mcp_demo_query")
    assert result.success is False and message.status == "error"
    assert mcp_image_memory_message(message).status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    ImageContent(type="image", mimeType="image/png", data="not!base64"),
    image(mime="image/jpeg"),
    image(b"not an image"),
    image(png(format="GIF", frames=True), mime="image/gif"),
    ImageContent(type="image", mimeType="image/svg+xml", data=base64.b64encode(b"<svg/>").decode()),
])
async def test_invalid_media_is_explicitly_omitted_and_never_stored(bad):
    ctx, _, files, _ = environment()
    result, session = await result_with(ctx, [bad])
    assert result.success is True  # Media refusal does not replay a completed MCP write.
    assert result.data["content"][0]["omitted"] is True
    assert files.uploads == 0
    assert bad.data not in result.model_dump_json()
    assert session.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_source_count_and_aggregate_bytes_are_bounded():
    ctx, _, files, _ = environment(config=settings(vision_max_images=1))
    result, _ = await result_with(ctx, [image(), image(png("blue"))])
    assert len(result._image_refs) == 1 and files.uploads == 1
    assert result.data["content"][1]["omitted"] is True
    ctx, _, files, _ = environment(config=settings(vision_max_request_bytes=1024))
    result, _ = await result_with(ctx, [ImageContent(type="image", mimeType="image/png", data="A" * 1028)])
    assert not result._image_refs and files.uploads == 0


@pytest.mark.asyncio
async def test_pixel_limit_and_exif_are_enforced():
    ctx, _, files, _ = environment(config=settings(vision_max_source_pixels=1024))
    result, _ = await result_with(ctx, [image(png(size=(64, 64)))])
    assert result.data["content"][0]["omitted"] is True and files.uploads == 0
    buf = io.BytesIO()
    source = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[270] = "/Users/private/location secret"
    exif[274] = 6
    source.save(buf, "JPEG", exif=exif)
    ctx, _, files, _ = environment()
    result, _ = await result_with(ctx, [image(buf.getvalue(), mime="image/jpeg")])
    stored, _ = next(iter(files.files.values()))
    assert b"/Users/private" not in stored
    with Image.open(io.BytesIO(stored)) as normalized:
        assert normalized.size == (20, 40) and not normalized.getexif()


@pytest.mark.asyncio
async def test_feature_and_exact_vision_capability_are_opt_in():
    toolkit = MCPToolkit()
    assert toolkit.get_tool("dataseek_mcp_image_read") is None
    assert "dataseek_mcp_image_read" not in [tool.name for tool in toolkit.get_tools()]
    ctx, _, files, _ = environment()
    ctx.model_name = "unlisted-model"
    result, _ = await result_with(ctx)
    assert not result._image_refs and files.uploads == 0
    assert "not enabled" in result.data["content"][1]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "session", "deleted", "expired", "corrupt", "metadata"])
async def test_every_restore_rechecks_owner_lifecycle_and_integrity(change):
    ctx, store, files, repo = environment()
    result, _ = await result_with(ctx)
    message = image_tool_message(result, tool_call_id="c", name="mcp_demo_query")
    record = next(iter(repo.records.values()))
    if change == "owner": ctx.owner = SpillArtifactOwner(user_id="different", session_id=OWNER.session_id)
    elif change == "session": ctx.owner = SpillArtifactOwner(user_id=OWNER.user_id, session_id="different")
    elif change == "deleted": await store.delete_owner(OWNER)
    elif change == "expired": record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "corrupt":
        raw, info = files.files[record.storage_file_id]
        files.files[record.storage_file_id] = b"!" + raw[1:], info
    else: files.files[record.storage_file_id][1].metadata["spill_sha256"] = "bad"
    restored = await ctx.expand_image_messages([message], provider="test", model_name="vision")
    assert all(block["type"] == "text" for block in restored[0].content)
    assert accepted_image_refs(message)  # Original references are retained, no mutation.


@pytest.mark.asyncio
async def test_binary_store_rejects_text_read_and_revocation_during_read():
    ctx, store, files, repo = environment()
    result, _ = await result_with(ctx)
    ref = result._image_refs[0].reference
    with pytest.raises(ValueError, match="dataseek_mcp_image_read"):
        await store.read_text(ref.locator, OWNER)
    original = files.download_file_range
    async def revoked(*args, **kwargs):
        reply = await original(*args, **kwargs)
        await store.delete_owner(OWNER)
        return reply
    files.download_file_range = revoked
    with pytest.raises(FileNotFoundError):
        await store.read_image(ref.locator, OWNER, max_bytes=1_000_000)


@pytest.mark.asyncio
async def test_policy_deletion_or_replacement_cannot_resurrect_image():
    ctx, *_ = environment()
    result, _ = await result_with(ctx)
    raw = image_tool_message(result, tool_call_id="c", name="mcp_demo_query")
    for change in (lambda m: m.content.pop(1), lambda m: m.content.__setitem__(1, {"type": "text", "text": "blocked by policy"})):
        message = raw.model_copy(deep=True)
        change(message)
        safe = mcp_image_memory_message(message)
        assert not accepted_image_refs(safe)
        ctx.store.read_image = AsyncMock(side_effect=AssertionError("policy must be final"))
        request = await ctx.expand_image_messages([safe], provider="test", model_name="vision")
        assert all(block["type"] == "text" for block in request[0].content)
        ctx.store.read_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_then_post_policy_then_token_budget_and_durable_projection(monkeypatch):
    config = settings(mcp_image_result_max_tokens=1024)
    monkeypatch.setattr("app.core.config.get_settings", lambda: config)
    ctx, store, *_ = environment(config=config)
    wire = CallToolResult(content=[TextContent(type="text", text="prefix " * 1200), image(), TextContent(type="text", text="suffix " * 1200)])
    manager, _ = await manager_with_result(wire)
    manager._image_context = ctx
    toolkit = MCPToolkit(image_context=ctx)
    seen = []
    class Policy(ToolExecutionInterceptor):
        async def post_execute(self, context, result):
            seen.append(bool(accepted_image_refs(result)))
            assert "base64" not in result.model_dump_json()
            result.content[1] = {"type": "text", "text": "POLICY_REMOVED_IMAGE"}
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        Policy(), SpillArtifactInterceptor(store, owner=OWNER, max_inline_bytes=5000, preview_bytes=1000),
    ])
    tool = _MCPToolWrapper("mcp_demo_query", manager, toolkit)
    result = await tool.ainvoke({"name": tool.name, "id": "c", "args": {}})
    assert seen == [True]
    assert not accepted_image_refs(result)
    assert "base64" not in projected_tool_artifact(result).model_dump_json()
    durable = mcp_image_memory_message(result)
    assert len(json.dumps(durable.content).encode()) < 5000
    request = await ctx.expand_image_messages([durable], provider="test", model_name="vision")
    assert all(block["type"] == "text" for block in request[0].content)


@pytest.mark.asyncio
async def test_all_images_omitted_have_complete_ordered_manifest_and_explicit_reread():
    config = settings()
    config.model_request_capabilities["test"]["vision"] = config.model_request_capabilities["test"]["vision"].model_copy(update={"image_tokens": 4096})
    ctx, store, files, _ = environment(config=config)
    result, _ = await result_with(ctx)
    message = image_tool_message(result, tool_call_id="c", name="mcp_demo_query")
    saved = []
    async def save(text):
        from app.domain.models.spill import SpillArtifactSaveRequest
        ref = await store.save_text(SpillArtifactSaveRequest(owner=OWNER, source=SpillArtifactSource(label="manifest"), content=text))
        saved.append(ref)
        return ref
    bounded = await bound_mcp_image_result(message, max_tokens=1024, max_text_bytes=5000, max_artifact_bytes=100_000, save_text=save)
    assert not accepted_image_refs(bounded)
    assert saved[0].locator in str(bounded.content)
    manifest = await store.read_text(saved[0].locator, OWNER)
    locator = result._image_refs[0].reference.locator
    assert manifest.content.index("before") < manifest.content.index(locator) < manifest.content.index("after")
    uploads = files.uploads
    reread = await ctx.read_result(locator)
    assert reread.success and files.uploads == uploads
    assert reread._image_refs[0].reference.locator == locator
    # Recovery is not a hidden model request; the actual model route remains
    # bound by its per-request vision limits when this result is expanded.
    request = await ctx.expand_image_messages([image_tool_message(reread, tool_call_id="r", name="dataseek_mcp_image_read")], provider="test", model_name="vision")
    assert any(block["type"] == "image_url" for block in request[0].content)


@pytest.mark.asyncio
async def test_current_request_image_count_does_not_become_cumulative_history_quota():
    ctx, *_ = environment(config=settings(vision_max_images=1))
    first, _ = await result_with(ctx, [image()])
    second, _ = await result_with(ctx, [image(png("blue"))])
    history = [image_tool_message(first, tool_call_id="a", name="mcp_demo_query"), image_tool_message(second, tool_call_id="b", name="mcp_demo_query")]
    request = await ctx.expand_image_messages(history, provider="test", model_name="vision")
    assert all(block["type"] == "text" for block in request[0].content)
    assert any(block["type"] == "image_url" for block in request[1].content)
    assert all(accepted_image_refs(message) for message in history)


@pytest.mark.asyncio
async def test_cancellation_propagates_without_mutating_durable_refs():
    ctx, *_ = environment()
    result, _ = await result_with(ctx)
    message = image_tool_message(result, tool_call_id="c", name="mcp_demo_query")
    before = message.model_dump_json()
    ctx.store.read_image = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ctx.expand_image_messages([message], provider="test", model_name="vision")
    assert message.model_dump_json() == before


@pytest.mark.asyncio
async def test_read_tool_is_in_pipeline_and_rejects_cross_session():
    ctx, *_ = environment()
    result, _ = await result_with(ctx)
    locator = result._image_refs[0].reference.locator
    toolkit = MCPToolkit(image_context=ctx)
    seen = []
    class Guard(ToolExecutionInterceptor):
        async def guard(self, context): seen.append(context.tool_name)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([Guard()])
    tool = toolkit.get_tool("dataseek_mcp_image_read")
    result = await tool.ainvoke({"name": tool.name, "id": "r", "args": {"locator": locator}})
    assert seen == ["dataseek_mcp_image_read"] and accepted_image_refs(result)
    ctx.owner = SpillArtifactOwner(user_id=OWNER.user_id, session_id="other-session")
    denied = await tool.ainvoke({"name": tool.name, "id": "r2", "args": {"locator": locator}})
    assert denied.status == "error" and not accepted_image_refs(denied)


@pytest.mark.asyncio
async def test_base_agent_request_expansion_retry_revocation_and_serialization():
    from langchain.messages import AIMessage, HumanMessage, SystemMessage
    from app.domain.models.memory import Memory
    from app.domain.models.event import ToolEvent, ToolStatus
    from app.domain.services.agents.base import BaseAgent
    from app.interfaces.schemas.event import ToolSSEEvent
    from test_model_retry_policy import failure
    ctx, store, *_ = environment()
    result, _ = await result_with(ctx)
    tool_result = image_tool_message(result, tool_call_id="call", name="mcp_demo_query")
    agent = object.__new__(BaseAgent)
    agent._agent_id, agent.name = "agent", "execution"
    durable = agent._tool_result_for_memory(tool_result, "call", "mcp_demo_query")
    agent.memory = Memory(messages=[SystemMessage(content="fixed"), HumanMessage(content="inspect"),
        AIMessage(content="", tool_calls=[{"id": "call", "name": "mcp_demo_query", "args": {}}]), durable])
    agent._repository = SimpleNamespace(save_memory=AsyncMock())
    agent.toolkits = [MCPToolkit(image_context=ctx)]
    agent.dynamic_system_prompt_provider = None
    agent._record_token_usage = AsyncMock()
    agent._llm_retry_attempts = 2
    agent._llm_retry_base_seconds = agent._llm_retry_max_seconds = 0
    requests = []
    async def invoke(messages):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            await store.delete_owner(OWNER)
            raise failure("0")
        return AIMessage(content="The image is no longer available; no new visual conclusion.")
    agent._model = SimpleNamespace(_llm_type="dataseek-model-driver",
        identity=SimpleNamespace(provider="test", model_name="vision"),
        bind=lambda **_: SimpleNamespace(ainvoke=invoke))
    # Deliberately wrong fallbacks prove Base uses actual bound identity.
    agent._model_provider, agent._model_name = "wrong", "text-only"
    await agent.ask_with_messages([], allow_tools=False)
    assert any(block.get("type") == "image_url" for m in requests[0] for block in m.content if isinstance(m.content, list))
    assert not any(block.get("type") == "image_url" for m in requests[1] for block in m.content if isinstance(m.content, list))
    assert "base64" not in agent.memory.model_dump_json()
    for call in agent._repository.save_memory.await_args_list:
        assert "base64" not in call.args[2].model_dump_json()
    event = ToolEvent(status=ToolStatus.CALLED, tool_call_id="call", tool_name="mcp", function_name="mcp_demo_query",
        function_args={}, function_result=projected_tool_artifact(tool_result))
    mapped = await ToolSSEEvent.from_event_async(event)
    for serialized in (event.model_dump_json(), mapped.model_dump_json()):
        assert "base64" not in serialized and "internal-storage" not in serialized


@pytest.mark.asyncio
async def test_control_characters_cannot_escape_public_byte_ceiling():
    ctx, store, *_ = environment()
    result, _ = await result_with(ctx, [TextContent(type="text", text="\u0000\"\\" * 5000), image()])
    message = image_tool_message(result, tool_call_id="c", name="mcp_demo_query")
    async def unavailable(_): raise RuntimeError("store unavailable")
    bounded = await bound_mcp_image_result(message, max_tokens=1024, max_text_bytes=5000,
        max_artifact_bytes=100_000, save_text=unavailable)
    assert len(projected_tool_artifact(bounded).model_dump_json().encode()) <= 5000
    assert "recovery is unavailable" in str(bounded.content)
    assert "base64" not in mcp_image_memory_message(bounded).model_dump_json()


@pytest.mark.asyncio
async def test_image_store_failure_never_falls_back_to_inline_or_replays_mcp():
    ctx, *_ = environment()
    ctx.store.save_image = AsyncMock(side_effect=RuntimeError("private secret /Users/operator"))
    result, session = await result_with(ctx)
    assert not result._image_refs and "private secret" not in result.model_dump_json()
    assert result.success and session.call_tool.await_count == 1
    assert "storage did not complete" in result.data["content"][1]["reason"]
    await ctx.drain()


@pytest.mark.asyncio
async def test_inline_image_echo_in_text_is_not_persisted_or_public():
    data = "data:image/png;base64," + base64.b64encode(png()).decode()
    ctx, *_ = environment()
    result, _ = await result_with(ctx, [TextContent(type="text", text=data), image()])
    assert data not in result.model_dump_json()
    assert "base64" not in image_tool_message(result, tool_call_id="c", name="mcp_demo_query").model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_recovery_name_never_shadows_remote_image_read(enabled):
    ctx, *_ = environment()
    toolkit = MCPToolkit(image_context=ctx if enabled else None)
    assert mcp_module._model_tool_name("image", "read") == "mcp_image_read"
    toolkit._tools = [{"type": "function", "function": {"name": "mcp_image_read", "parameters": {"type": "object"}}}]
    remote = toolkit.get_tool("mcp_image_read")
    assert isinstance(remote, _MCPToolWrapper)
    assert remote.name == "mcp_image_read"
    assert (toolkit.get_tool("dataseek_mcp_image_read") is not None) is enabled
    from app.domain.services.tools.tool_contract import resolved_tool_is_read_only
    assert not resolved_tool_is_read_only(remote)
    if enabled:
        assert resolved_tool_is_read_only(toolkit.get_tool("dataseek_mcp_image_read"))
