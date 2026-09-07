from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.domain.models.event import FileToolContent, ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.models.memory import Memory
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents.base import BaseAgent
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.tools.spill import SPILL_READ_TOOL_NAME, SpillArtifactInterceptor
from app.domain.services.tools.spill_projection import (
    projected_tool_artifact,
    sanitize_spill_public_data,
    sanitize_spill_public_text,
    spill_notice_from_result,
)
from app.infrastructure.external.file.spill import FileStorageSpillArtifactStore
from app.interfaces.schemas.event import ToolSSEEvent
from test_spill_artifact_store import (
    OWNER,
    CapturingStore,
    InMemoryFileStorage,
    InMemorySpillRepository,
    _context,
    _message,
)
from langchain.messages import ToolMessage


@pytest.mark.parametrize("key", [
    "api_key", "client_secret", "password", "authorization",
    "aws_secret_access_key", "MY_SERVICE_API_KEY", "refreshToken",
])
def test_quoted_json_credentials_are_redacted_even_in_partial_preview(key):
    for text in (json.dumps({key: 'secret-value"escaped'}), f'{{"{key}":"secret-value'):
        assert "secret-value" not in sanitize_spill_public_text(text)


def test_spill_sanitizer_bounds_deep_cyclic_and_wide_values():
    nested = {"api_key": "private-value"}
    for _ in range(1200):
        nested = {"next": nested}
    cycle = {}
    cycle["self"] = cycle
    for data in (nested, cycle, {str(i): i for i in range(10000)}):
        safe = sanitize_spill_public_data(data)
        text = json.dumps(safe)
        assert "omitted" in text
        assert len(text) < 50_000
        assert "private-value" not in text


async def persist(event):
    payloads = []

    async def put(payload):
        payloads.append(payload)
        return "redis-event-1"

    async def add_event(session_id, durable_event):
        assert session_id == OWNER.session_id
        payloads.append(durable_event.model_dump_json())

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = OWNER.session_id
    runner._session_repository = SimpleNamespace(add_event=add_event)
    await runner._put_and_add_event(
        SimpleNamespace(output_stream=SimpleNamespace(put=put)), event
    )
    assert len(payloads) == 2
    return payloads


@pytest.mark.asyncio
async def test_json_preview_is_redacted_in_durable_events_and_direct_sse():
    raw = '{"api_key":"json-secret","client_secret":"client-value","password":"pass-value"}'
    transformed = await SpillArtifactInterceptor(
        CapturingStore(), owner=OWNER, max_inline_bytes=2048, preview_bytes=700
    ).result(_context(), _message(raw * 100))
    nested = {}
    for _ in range(1200):
        nested = {"next": nested}
    event = ToolEvent(
        status=ToolStatus.CALLED, tool_call_id="call-1", tool_name="plugin",
        function_name="large_tool", function_args=nested,
        function_result=projected_tool_artifact(transformed),
    )
    for serialized in await persist(event):
        for secret in ("json-secret", "client-value", "pass-value"):
            assert secret not in serialized
        assert "spill://artifact/" in serialized
    # Also cover the mapper in isolation, before a durable projection exists.
    mapped = await ToolSSEEvent.from_event_async(event.model_copy(update={"function_args": {}}))
    for secret in ("json-secret", "client-value", "pass-value"):
        assert secret not in mapped.model_dump_json()
    assert "json-secret" in spill_notice_from_result(transformed).preview


@pytest.mark.asyncio
async def test_read_page_keeps_raw_text_in_memory_but_only_metadata_in_history():
    raw = '{"api_key":"raw-secret","path":"/Users/alice/data.csv"}'
    locator = "spill://artifact/0123456789abcdef0123456789abcdef"
    event = ToolEvent(
        status=ToolStatus.CALLED, tool_call_id="call-read", tool_name="spill",
        function_name=SPILL_READ_TOOL_NAME, function_args={"locator": locator},
        function_result=ToolResult(success=True, data={
            "schema_version": 1, "locator": locator, "content": raw,
            "start_byte": 0, "next_byte": len(raw), "total_bytes": len(raw),
            "eof": True, "sha256": "a" * 64, "media_type": "text/plain",
        }),
        tool_content=FileToolContent(content=raw),
        presentation={"kind": "log", "description": raw},
    )
    for serialized in await persist(event):
        assert "raw-secret" not in serialized
        assert "/Users/alice" not in serialized
        assert locator in serialized
        assert "next_byte" in serialized
    assert event.function_result.data["content"] == raw
    assert event.tool_content.content == raw


@pytest.mark.asyncio
async def test_model_memory_save_omits_read_bytes_and_redacts_spill_preview():
    raw = '{"api_key":"memory-secret","path":"/Users/alice/data.csv"}'
    locator = "spill://artifact/0123456789abcdef0123456789abcdef"
    read = ToolResult(success=True, data={
        "schema_version": 1, "locator": locator, "content": raw,
        "start_byte": 0, "next_byte": len(raw), "total_bytes": len(raw),
        "eof": True, "sha256": "a" * 64, "media_type": "text/plain",
    })
    page = ToolMessage(tool_call_id="read", name=SPILL_READ_TOOL_NAME,
                       content=read.model_dump_json(), artifact=read)
    spill = await SpillArtifactInterceptor(
        CapturingStore(), owner=OWNER, max_inline_bytes=2048, preview_bytes=700
    ).result(_context(), _message(raw * 100))
    saved = []

    async def save_memory(agent_id, name, memory):
        saved.append(memory.model_dump_json())

    agent = BaseAgent.__new__(BaseAgent)
    agent._agent_id = "agent-1"
    agent._repository = SimpleNamespace(save_memory=save_memory)
    agent.memory = Memory(messages=[
        agent._tool_result_for_memory(page, "read", SPILL_READ_TOOL_NAME),
        agent._tool_result_for_memory(spill, "call-1", "large_tool"),
    ])
    await agent._persist_memory()
    assert "memory-secret" not in saved[0]
    assert "/Users/alice" not in saved[0]
    assert locator in saved[0]
    assert "next_byte" in saved[0]
    assert "memory-secret" in agent.memory.messages[0].content
    assert "memory-secret" in agent.memory.messages[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_cleanup", [False, True])
async def test_runner_close_joins_timed_out_saves_before_owner_deletion(cancel_cleanup):
    class SlowRepository(InMemorySpillRepository):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def save(self, record):
            self.started.set()
            await self.release.wait()
            await super().save(record)

    repository = SlowRepository()
    files = InMemoryFileStorage()
    store = FileStorageSpillArtifactStore(files, repository, identity_key="test-key")
    interceptor = SpillArtifactInterceptor(
        store, owner=OWNER, max_inline_bytes=1024, preview_bytes=200,
        store_timeout_seconds=0.01,
    )
    result = await interceptor.result(_context(), _message("large output" * 200))
    assert spill_notice_from_result(result).status == "unavailable"
    assert repository.started.is_set()

    flow = PlanActFlow.__new__(PlanActFlow)
    # Simulate a replaced interceptor followed by the current empty bundle.
    flow._spill_interceptors = [interceptor, SpillArtifactInterceptor(store, owner=OWNER)]
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._browser = runner._sandbox = runner._mcp_tool = None
    runner._flow = flow
    closed = asyncio.create_task(runner.on_done(None))
    await asyncio.sleep(0)
    if cancel_cleanup:
        closed.cancel()
        await asyncio.sleep(0)
    assert not closed.done()
    repository.release.set()
    if cancel_cleanup:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closed, timeout=1)
    else:
        await asyncio.wait_for(closed, timeout=1)
    assert not interceptor._pending_saves
    assert not flow._spill_interceptors
    assert len(repository.records) == 1
    assert await store.delete_owner(OWNER) == 1
    assert not repository.records
    assert not files.files
