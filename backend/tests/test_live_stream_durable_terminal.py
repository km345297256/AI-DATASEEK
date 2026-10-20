"""Read-only SSE settlement does not wait for Redis or task cleanup."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, WaitEvent
from test_input_stream_recovery import StreamInputs, StreamRepository, accepted, service_for


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["done", "error", "wait"])
async def test_silent_live_transport_delivers_durable_terminal_before_task_cleanup(kind):
    record = accepted(state="running", task_id="live-task")
    inputs = StreamInputs(record)
    terminal = {"done": DoneEvent(seq=5), "error": ErrorEvent(seq=5, error="request cancelled"),
                "wait": WaitEvent(seq=5)}[kind]
    repository = StreamRepository([record.event])
    inputs.matches_terminal = AsyncMock(return_value=True)

    async def silent_read(**_kwargs):
        assert read.await_count == 1, "durable terminal must close a stream while cleanup remains live"
        repository.events.extend([MessageEvent(seq=4, message="retained result"), terminal])
        return None, None

    read = AsyncMock(side_effect=silent_read)
    task = SimpleNamespace(id="live-task", done=False, output_stream=SimpleNamespace(get=read))
    service = service_for(repository, inputs, task)
    events = [event async for event in service.chat("session", "user", latest_event_seq=3)]

    assert [event.seq for event in events] == [4, 5]
    assert events[-1].type == kind
    assert not task.done, "observing a terminal is not proof of process cleanup"
    inputs.cancel_session.assert_not_awaited()
    inputs.matches_terminal.assert_awaited_once_with("session", record.key, 5, kind)


@pytest.mark.asyncio
async def test_live_durable_fallback_deduplicates_output_and_ignores_foreign_terminal():
    record = accepted(state="running", task_id="live-task")
    inputs = StreamInputs(record)
    inputs.matches_terminal = AsyncMock(side_effect=lambda _session, key, seq, kind:
        key == record.key and seq == 7 and kind == "error")
    repository = StreamRepository([record.event])
    first = MessageEvent(seq=4, message="already delivered")

    async def read_output(**_kwargs):
        if read.await_count == 1:
            repository.events.append(first)
            return "redis-1", first.model_dump_json()
        if read.await_count == 2:
            repository.events.extend([
                ErrorEvent(seq=5, error="older input cancelled"),
                MessageEvent(seq=6, message="current retained result"),
                ErrorEvent(seq=7, error="current request cancelled"),
            ])
            return None, None
        pytest.fail("exact durable terminal must end the live stream")

    read = AsyncMock(side_effect=read_output)
    task = SimpleNamespace(id="live-task", done=False, output_stream=SimpleNamespace(get=read))
    service = service_for(repository, inputs, task)

    events = [event async for event in service.chat("session", "user", latest_event_seq=3)]

    assert [event.seq for event in events] == [4, 6, 7]
    assert inputs.matches_terminal.await_count == 2
    assert read.await_count == 2


@pytest.mark.asyncio
async def test_durable_observation_outage_does_not_create_a_fake_task_failure():
    record = accepted(state="running", task_id="live-task")
    inputs = StreamInputs(record)
    repository = StreamRepository([record.event])
    terminal = DoneEvent(seq=4)
    repository.get_events_after = AsyncMock(side_effect=[[], ConnectionError("test outage")])
    repository.add_event = AsyncMock()
    read = AsyncMock(side_effect=[(None, None), ("redis-1", terminal.model_dump_json())])
    task = SimpleNamespace(id="live-task", done=False, output_stream=SimpleNamespace(get=read))
    service = service_for(repository, inputs, task)

    events = [event async for event in service.chat("session", "user", latest_event_seq=3)]

    assert [event.type for event in events] == ["done"]
    repository.add_event.assert_not_awaited()
    assert read.await_count == 2
