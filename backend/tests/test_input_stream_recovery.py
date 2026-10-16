"""Durable input admission and the unchanged terminal SSE contract together.

These tests use only memory records and deterministic transport failures: no
Mongo/Redis service, model call, sandbox process or user data is needed.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, ToolEvent, ToolStatus, WaitEvent
from app.domain.models.input_admission import AcceptedInput, InputAdmission, input_key
from app.domain.models.session import Session, SessionStatus
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.input_delivery import InputDeliveryService, InputLeaseLost


class StreamRepository:
    def __init__(self, events):
        self.events = list(events)
        self.session = Session(id="session", user_id="user", agent_id="agent", status=SessionStatus.RUNNING)
        self.queries = []
        self.aliases = {}

    async def find_by_id_and_user_id(self, session_id, user_id):
        assert (session_id, user_id) == ("session", "user")
        return self.session

    async def find_by_id(self, session_id):
        assert session_id == "session"
        return self.session

    async def get_events_after(self, session_id, seq):
        assert session_id == "session"
        self.queries.append(seq)
        return [event for event in self.events if event.seq > seq]

    async def resolve_event_sequence(self, session_id, event_id):
        assert session_id == "session"
        return self.aliases.get(event_id)

    async def update_unread_message_count(self, _session_id, _count):
        pass

    async def update_status(self, _session_id, status):
        self.session.status = status

    async def add_event(self, _session_id, event):
        self.events.append(event)


class StreamInputs:
    def __init__(self, record=None):
        self.record = record
        self.cancel_session = AsyncMock()

    async def get(self, session_id, key):
        record = self.record
        return record if record and (record.session_id, record.key) == (session_id, key) else None

    async def latest(self, session_id):
        assert session_id == "session"
        return self.record

    async def unsettled(self, session_id):
        assert session_id == "session"
        record = self.record
        if record and (record.admission.state in {"pending", "claimed", "running"}
                       or (record.admission.state in {"cancelled", "interrupted"} and not record.admission.notified)):
            return record
        return None


def accepted(seq=3, state="pending", task_id=None):
    event = MessageEvent(id=AgentDomainService._client_message_event_id("session", "client"),
                         seq=seq, role="user", message="new question", metadata={"client_message_id": "client"})
    return AcceptedInput(session_id="session", key=input_key(event), event=event,
                         admission=InputAdmission(actor_user_id="user", state=state, task_id=task_id))


def service_for(repository, inputs=None, task=None):
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = repository
    service._input_delivery = SimpleNamespace(repository=inputs) if inputs else None
    service._chat_bootstrap_tasks = set()
    service._task_cls = SimpleNamespace(get=lambda task_id: task if task and task.id == task_id else None)
    service._get_task = AsyncMock(return_value=task)
    service._schedule_accepted_input = AsyncMock()
    service._resolve_message_attachments = AsyncMock(return_value=[])
    return service


@pytest.mark.asyncio
async def test_duplicate_accepted_input_attaches_without_waiting_for_running_task_cleanup():
    task = SimpleNamespace(id="running-task", done=False,
                           wait_closed=AsyncMock(side_effect=AssertionError("reconnect must not wait for completion")))
    inputs = StreamInputs(accepted(state="running", task_id=task.id))
    repository = StreamRepository([inputs.record.event])
    service = service_for(repository, inputs, task)
    result = await service._bootstrap_durable_input(repository.session, "user", "new question", None,
                                                    None, None, None, None, False, "client")
    assert result is task
    task.wait_closed.assert_not_awaited()
    service._resolve_message_attachments.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_terminal", [DoneEvent(seq=2), WaitEvent(seq=2), ErrorEvent(seq=2, error="old failure")])
async def test_old_terminal_cannot_end_the_new_input_for_a_stop_on_terminal_sse_client(old_terminal):
    record = accepted(state="completed")
    repository = StreamRepository([old_terminal, record.event,
                                   MessageEvent(seq=4, message="new answer"), DoneEvent(seq=5)])
    service = service_for(repository, StreamInputs(record))
    service._bootstrap_chat_task = AsyncMock(return_value=None)
    stream = service.chat("session", "user", message="new question", client_message_id="client", latest_event_seq=1)
    events = []
    try:
        async for event in stream:
            events.append(event)
            if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                break  # The frontend stops at the first terminal; do not hide this behind list().
    finally:
        await stream.aclose()
    assert [event.seq for event in events] == [4, 5]
    assert [event.type for event in events] == ["message", "done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "claimed", "running"])
async def test_no_local_task_keeps_durable_tail_open_until_recovered_work_finishes(state, monkeypatch):
    record = accepted(state=state)
    inputs = StreamInputs(record)
    repository = StreamRepository([record.event])
    service = service_for(repository, inputs)
    sleeps = []

    async def recover_on_wait(delay):
        sleeps.append(delay)
        assert len(sleeps) == 1, "recovered terminal must settle the stream"
        repository.events.extend([MessageEvent(seq=4, message="recovered answer"), DoneEvent(seq=5)])
        inputs.record = record.model_copy(update={"admission": record.admission.model_copy(update={"state": "completed", "notified": True})})

    monkeypatch.setattr("app.domain.services.agent_domain_service.asyncio.sleep", recover_on_wait)
    events = [event async for event in service.chat("session", "user", latest_event_seq=2)]
    assert [event.seq for event in events] == [4, 5]
    assert sleeps and 0 < sleeps[0] <= 1
    assert repository.queries[0] == 2
    assert repository.queries[-1] >= 3


@pytest.mark.asyncio
async def test_event_id_only_client_recovers_committed_events_without_a_redis_task():
    record = accepted(state="completed")
    repository = StreamRepository([record.event, MessageEvent(seq=4, message="answer"), DoneEvent(seq=5)])
    repository.aliases["1700000000000-1"] = 3
    service = service_for(repository, StreamInputs(record))
    events = [event async for event in service.chat("session", "user", latest_event_id="1700000000000-1")]
    assert [event.seq for event in events] == [4, 5]
    assert repository.queries[0] == 3


@pytest.mark.asyncio
async def test_closing_the_viewer_does_not_cancel_an_accepted_input():
    record = accepted(state="running")
    repository = StreamRepository([record.event, MessageEvent(seq=4, message="partial")])
    inputs = StreamInputs(record)
    service = service_for(repository, inputs)
    stream = service.chat("session", "user", latest_event_seq=3)
    assert (await anext(stream)).seq == 4
    await stream.aclose()
    assert inputs.record.admission.state == "running"
    inputs.cancel_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    MessageEvent(message="late answer"),
    ToolEvent(tool_call_id="call", tool_name="shell", function_name="shell_run", function_args={},
              status=ToolStatus.CALLED, function_result={"output": "late result"}),
    DoneEvent(),
])
async def test_expired_attempt_cannot_publish_any_late_output(output):
    record = accepted(state="running")
    inputs = StreamInputs(record)
    delivery = InputDeliveryService(inputs, StreamRepository([record.event]))
    record.admission.runtime_id = delivery.runtime_id
    record.admission.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(InputLeaseLost):
        await delivery.prepare_event("session", record.key, output)


@pytest.mark.asyncio
@pytest.mark.parametrize("paused_at", ["previous_runner_cleanup", "attachment_lookup"])
async def test_stop_cancels_a_bootstrap_before_it_can_accept_new_work(paused_at):
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def pause():
        entered.set()
        await blocked.wait()
        return []

    old_task = (SimpleNamespace(id="old-task", done=False, wait_closed=pause, cancel=lambda: None)
                if paused_at == "previous_runner_cleanup" else None)
    repository = StreamRepository([])
    inputs = StreamInputs()
    inputs.accept = AsyncMock(side_effect=AssertionError("a stopped bootstrap must not accept work"))
    service = service_for(repository, inputs, old_task)
    service._input_delivery = InputDeliveryService(inputs, repository)
    if paused_at == "attachment_lookup":
        async def lookup(*_args, **_kwargs):
            await pause()
            return [], []
        service._prepare_input_selection = AsyncMock(side_effect=lookup)

    bootstrap = service._track_chat_bootstrap(asyncio.create_task(service._bootstrap_durable_input(
        repository.session, "user", "new question", None, None, None, None, None, False, "client",
    )), "session")
    await asyncio.wait_for(entered.wait(), timeout=1)
    await service.stop_session("session")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(bootstrap, timeout=1)
    inputs.accept.assert_not_awaited()
    assert repository.session.status == SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_late_cancellation_notice_for_an_older_input_does_not_close_current_stream():
    record = accepted(state="completed")
    inputs = StreamInputs(record)
    inputs.matches_terminal = AsyncMock(side_effect=lambda _session, _key, seq, kind: seq == 6 and kind == "done")
    repository = StreamRepository([record.event, ErrorEvent(seq=4, error="older input cancelled"),
                                   MessageEvent(seq=5, message="current answer"), DoneEvent(seq=6)])
    service = service_for(repository, inputs)
    events = [event async for event in service.chat("session", "user", latest_event_seq=2)]
    assert [event.seq for event in events] == [5, 6]
    assert inputs.matches_terminal.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_state", ["cancelled", "interrupted"])
async def test_read_only_stop_replay_waits_for_delayed_durable_terminal_after_http_confirmation(stop_state, monkeypatch):
    """The stop acknowledgement/session status is not proof of event settlement."""
    record = accepted(seq=1, state=stop_state)
    inputs = StreamInputs(record)
    repository = StreamRepository([record.event])
    repository.session.status = SessionStatus.COMPLETED  # stop HTTP already returned
    service = service_for(repository, inputs)
    service._bootstrap_chat_task = AsyncMock(side_effect=AssertionError("observation must never dispatch"))
    sleeps = []

    async def finish_cancellation(delay):
        sleeps.append(delay)
        if len(sleeps) == 1:
            repository.events.append(ToolEvent(seq=2, tool_call_id="final-tool", tool_name="shell",
                function_name="shell_run", function_args={}, status=ToolStatus.CALLED,
                function_result={"success": False}))
        elif len(sleeps) == 2:
            repository.events.extend([
                MessageEvent(seq=3, message="Stopped; existing results are retained.", metadata={
                    "analysis_outcome": {"status": "partial", "reason_code": "request_cancelled",
                                         "missing": [], "can_resume": False}}),
                ErrorEvent(seq=4, error="Request cancelled"),
            ])
            record.admission.notified = True
        else:
            raise AssertionError("terminal must close the stream, not poll forever")

    monkeypatch.setattr("app.domain.services.agent_domain_service.asyncio.sleep", finish_cancellation)
    events = [event async for event in service.chat("session", "user", message="", latest_event_seq=1)]
    assert [event.seq for event in events] == [2, 3, 4]
    assert [event.type for event in events] == ["tool", "message", "error"]
    assert sleeps == [0.5, 0.5]
    service._bootstrap_chat_task.assert_not_awaited()
    inputs.cancel_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_observation_of_unaccepted_input_never_replays_previous_terminal_and_is_bounded(monkeypatch):
    old = accepted(state="completed")
    inputs = StreamInputs(old)
    repository = StreamRepository([old.event, DoneEvent(seq=4)])
    service = service_for(repository, inputs)
    sleeps = []
    async def wait(delay):
        sleeps.append(delay)
    monkeypatch.setattr("app.domain.services.agent_domain_service.asyncio.sleep", wait)
    events = [event async for event in service.chat("session", "user", latest_event_seq=0,
                                                   observe_client_message_id="not-yet-accepted")]
    assert events == []
    assert sleeps == [0.5] * 19
    assert repository.queries == [], "an absent input must not fall back to session history"
    service._get_task.assert_not_awaited()
    service._schedule_accepted_input.assert_not_awaited()
    inputs.cancel_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_observation_follows_its_input_if_admission_and_terminal_arrive_later(monkeypatch):
    record = accepted(seq=3, state="cancelled")
    inputs = StreamInputs()
    inputs.matches_terminal = AsyncMock(side_effect=lambda _session, key, seq, kind: key == record.key and seq == 5)
    repository = StreamRepository([MessageEvent(seq=1, role="user", message="old"), DoneEvent(seq=2)])
    service = service_for(repository, inputs)
    sleeps = []
    async def advance(delay):
        sleeps.append(delay)
        if len(sleeps) == 1:
            inputs.record = record
            repository.events.append(record.event)
        elif len(sleeps) == 2:
            repository.events.extend([MessageEvent(seq=4, message="cancelled"), ErrorEvent(seq=5, error="cancelled")])
        else:
            pytest.fail("observation must settle on its terminal")
    monkeypatch.setattr("app.domain.services.agent_domain_service.asyncio.sleep", advance)
    events = [event async for event in service.chat("session", "user", latest_event_seq=0,
                                                   observe_client_message_id="client")]
    assert [event.seq for event in events] == [4, 5]
    assert sleeps == [0.5, 0.5]
    service._get_task.assert_not_awaited()
    service._schedule_accepted_input.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [2, 4])
async def test_stop_observation_never_takes_another_turns_terminal_even_across_reconnect(cursor):
    record = accepted(seq=1, state="cancelled")
    inputs = StreamInputs(record)
    own_outcome = MessageEvent(seq=6, message="own cancellation", metadata={"analysis_outcome": {"status": "failed"}})
    inputs.matches_terminal = AsyncMock(side_effect=lambda _session, key, seq, kind: key == record.key and seq == 7)
    inputs.committed_outcome = AsyncMock(return_value=own_outcome)
    repository = StreamRepository([record.event, MessageEvent(seq=2, message="own partial"),
        MessageEvent(seq=3, role="user", message="another tab's question"), MessageEvent(seq=4, message="foreign answer"),
        DoneEvent(seq=5), own_outcome, ErrorEvent(seq=7, error="own cancelled")])
    service = service_for(repository, inputs)
    events = [event async for event in service.chat("session", "user", latest_event_seq=cursor,
                                                   observe_client_message_id="client")]
    assert [event.seq for event in events] == [6, 7]
    service._get_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_observation_failure_does_not_persist_a_fake_execution_error():
    inputs = StreamInputs(accepted())
    inputs.get = AsyncMock(side_effect=RuntimeError("repository unavailable"))
    repository = StreamRepository([])
    repository.add_event = AsyncMock()
    service = service_for(repository, inputs)
    with pytest.raises(RuntimeError, match="repository unavailable"):
        _ = [event async for event in service.chat("session", "user", observe_client_message_id="client")]
    repository.add_event.assert_not_awaited()


@pytest.mark.parametrize("extra", [{"message": "new work"}, {"resume_from": "a" * 32}, {"client_message_id": "new"},
    {"agent_profile_id": "model"}, {"attachments": [{"file_id": "file"}]}, {"skills": ["skill"]},
    {"mcp_servers": ["server"]}, {"dataset_ids": ["dataset"]}, {"input_file_ids": []}])
def test_observation_schema_rejects_submission_or_configuration_changes(extra):
    from pydantic import ValidationError
    from app.interfaces.schemas.session import ChatRequest
    with pytest.raises(ValidationError):
        ChatRequest(observe_client_message_id="client", **extra)
