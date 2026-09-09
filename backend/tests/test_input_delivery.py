import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, WaitEvent
from app.domain.models.input_admission import AcceptedInput, InputAdmission, input_key, terminal_event_id
from app.domain.models.session import Session, SessionStatus
from app.domain.services.input_delivery import InputDeliveryService, InputLeaseLost
from app.domain.services.agent_domain_service import AgentDomainService
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.infrastructure.models.documents import SessionDocument, SessionEventDocument


class MemoryInputs:
    def __init__(self):
        self.records = {}
        self.events = []
        self.allow = True
        self.session = Session(id="session", user_id="user", agent_id="agent")
        self.lock = asyncio.Lock()
        self.input_generation = 0
        self.history_reads = 0

    async def generation(self, session_id):
        assert session_id == self.session.id
        return self.input_generation

    async def accept(self, session_id, actor_user_id, event, *, generation=None):
        key = input_key(event)
        identity = (session_id, key)
        async with self.lock:
            old = self.records.get(identity)
            if old:
                assert old.event.message == event.message
                return old.model_copy(deep=True)
            event.seq = max((item.seq or 0 for item in self.events), default=0) + 1
            record = AcceptedInput(session_id=session_id, key=key, event=event,
                                   admission=InputAdmission(actor_user_id=actor_user_id,
                                       session_generation=self.input_generation if generation is None else generation))
            self.records[identity] = record
            self.events.append(event)
            return record.model_copy(deep=True)

    async def get(self, session_id, key):
        record = self.records.get((session_id, key))
        return record.model_copy(deep=True) if record else None

    async def claim(self, record, runtime_id, expires):
        if any(item.admission.state in {"claimed", "running"} and item.session_id == record.session_id
               for item in self.records.values()):
            return None
        return await self.transition(record, {"state": "claimed", "runtime_id": runtime_id,
            "lease_expires_at": expires, "attempts": record.admission.attempts + 1})

    async def transition(self, record, updates, require_live=False):
        async with self.lock:
            current = self.records.get((record.session_id, record.key))
            if current is None or current.admission.revision != record.admission.revision:
                return None
            if require_live and current.admission.lease_expires_at <= datetime.now(UTC):
                return None
            admission = current.admission.model_copy(update={**updates, "revision": current.admission.revision + 1})
            updated = current.model_copy(update={"admission": admission}, deep=True)
            self.records[(record.session_id, record.key)] = updated
            return updated.model_copy(deep=True)

    async def candidates(self, now, limit=100):
        return [item.model_copy(deep=True) for item in self.records.values() if
            (item.admission.state == "pending" and item.admission.retry_after <= now) or
            (item.admission.state in {"claimed", "running"} and item.admission.lease_expires_at <= now) or
            (item.admission.state in {"cancelled", "interrupted"} and not item.admission.notified)][:limit]

    async def terminal_kind(self, record):
        for event in self.events:
            if event.type in {"done", "wait", "error"} and event.bind_producer_event_id() == terminal_event_id(record.key, event.type):
                return event.type
        return None

    async def authorized(self, record):
        return self.allow and record.admission.session_generation == self.input_generation

    async def cancel_session(self, session_id):
        self.input_generation += 1
        for record in list(self.records.values()):
            if record.session_id == session_id and record.admission.state in {"pending", "claimed", "running"}:
                await self.transition(record, {"state": "cancelled", "notified": False})

    async def find_by_id(self, session_id):
        return self.session if self.session and self.session.id == session_id else None

    async def update_status(self, session_id, status):
        self.session.status = status

    async def find_by_id_and_user_id(self, session_id, user_id):
        return self.session if self.session.id == session_id and self.session.user_id == user_id else None

    async def save(self, session):
        self.session = session

    async def update_latest_message(self, session_id, message, timestamp):
        self.session.latest_message = message

    async def get_events(self, session_id):
        self.history_reads += 1
        return list(self.events)

    async def add_event(self, session_id, event):
        if not any(item.bind_producer_event_id() == event.bind_producer_event_id() for item in self.events):
            self.events.append(event)


class FakeTask:
    def __init__(self, task_id="task"):
        self.id = task_id
        self.done = False
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self.done = True


async def pending(repository):
    return await repository.accept("session", "user", MessageEvent(id="input", role="user", message="analyse"))


async def expired(repository, record):
    current = await repository.get(record.session_id, record.key)
    return await repository.transition(current, {"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_stage", ["after_accept", "after_claim", "after_enqueue", "after_pop"])
async def test_restart_only_redelivers_inputs_that_never_entered_execution(crash_stage):
    repository = MemoryInputs()
    old = InputDeliveryService(repository, repository)
    record = await pending(repository)
    if crash_stage != "after_accept":
        record = await old.claim(record)
        if crash_stage in {"after_enqueue", "after_pop"}:
            await old.bind(record, FakeTask("lost-task"))
        await expired(repository, record)
    restarted = InputDeliveryService(repository, repository)
    dispatched = []

    async def dispatch(item):
        claimed = await restarted.claim(item)
        if claimed:
            dispatched.append(claimed)

    await restarted.maintain(dispatch)
    await restarted.maintain(dispatch)
    assert len(dispatched) == 1
    assert dispatched[0].event.id == "input"
    assert len(repository.events) == 1
    assert (await repository.get("session", record.key)).admission.runtime_id == restarted.runtime_id


@pytest.mark.asyncio
async def test_running_expiry_interrupts_without_automatic_tool_replay():
    repository = MemoryInputs()
    original = InputDeliveryService(repository, repository)
    record = await original.claim(await pending(repository))
    task = FakeTask()
    repository.session.task_id = task.id
    await original.bind(record, task)
    await original.start("session", record.event, task)
    await expired(repository, record)
    restarted = InputDeliveryService(repository, repository)
    calls = []

    async def dispatch(item):
        calls.append(item)

    for _ in range(3):
        await restarted.maintain(dispatch)
    current = await repository.get("session", record.key)
    assert current.admission.state == "interrupted"
    assert current.admission.notified
    assert calls == []
    assert len([event for event in repository.events if isinstance(event, ErrorEvent)]) == 1
    with pytest.raises(InputLeaseLost):
        await original.prepare_event("session", record.key, DoneEvent())


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [DoneEvent, WaitEvent, lambda: ErrorEvent(error="safe error")])
async def test_committed_terminal_repairs_missing_completion_marker_after_restart(terminal):
    repository = MemoryInputs()
    original = InputDeliveryService(repository, repository)
    record = await original.claim(await pending(repository))
    task = FakeTask()
    repository.session.task_id = task.id
    await original.bind(record, task)
    await original.start("session", record.event, task)
    event = terminal()
    await original.prepare_event("session", record.key, event)
    await repository.add_event("session", event)
    await expired(repository, record)
    restarted = InputDeliveryService(repository, repository)
    await restarted.maintain(lambda _: pytest.fail("finished input must not dispatch"))
    assert (await repository.get("session", record.key)).admission.state == "completed"
    assert len(repository.events) == 2
    assert repository.session.status == (SessionStatus.WAITING if event.type == "wait" else SessionStatus.COMPLETED)


@pytest.mark.asyncio
async def test_duplicate_redis_delivery_cannot_start_running_input_twice():
    repository = MemoryInputs()
    service = InputDeliveryService(repository, repository)
    record = await service.claim(await pending(repository))
    task = FakeTask()
    await service.bind(record, task)
    assert await service.start("session", record.event, task)
    with pytest.raises(InputLeaseLost):
        await service.start("session", record.event, task)


@pytest.mark.asyncio
async def test_two_runtimes_have_one_claim_winner_and_stale_worker_cannot_start():
    repository = MemoryInputs()
    record = await pending(repository)
    a, b = InputDeliveryService(repository, repository), InputDeliveryService(repository, repository)
    first, second = await asyncio.gather(a.claim(record), b.claim(record))
    assert (first is None) != (second is None)
    winner, loser = (a, b) if first else (b, a)
    await winner.bind(first or second, FakeTask())
    with pytest.raises(InputLeaseLost):
        await loser.start("session", record.event, FakeTask())


@pytest.mark.asyncio
async def test_revoked_authority_cancels_pending_input_without_dispatch():
    repository = MemoryInputs()
    record = await pending(repository)
    repository.allow = False
    service = InputDeliveryService(repository, repository)
    assert await service.claim(record) is None
    assert (await repository.get("session", record.key)).admission.state == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_pending_input_is_not_recovered_as_new_work():
    repository = MemoryInputs()
    record = await pending(repository)
    service = InputDeliveryService(repository, repository)
    await service.cancel_session("session")
    await service.maintain(lambda _: pytest.fail("cancelled input must not dispatch"))
    assert (await repository.get("session", record.key)).admission.state == "cancelled"
    assert len(repository.events) == 2


def test_private_admission_and_request_snapshot_do_not_enter_event_or_message_schema():
    from app.domain.models.message import Message
    event = MessageEvent(role="user", message="hello")
    message = Message(message="hello")
    message._session_events_snapshot = [event]
    assert "input_admission" not in event.model_dump_json()
    assert "session_events_snapshot" not in message.model_dump_json()
    assert "session_events_snapshot" not in str(Message.model_json_schema())


@pytest.mark.asyncio
async def test_mongo_acceptance_writes_event_and_admission_in_one_atomic_upsert(monkeypatch):
    writes = []
    marker_write = AsyncMock(return_value=SimpleNamespace(matched_count=1))
    monkeypatch.setattr(SessionDocument, "get_pymongo_collection", classmethod(lambda cls: SimpleNamespace(update_one=marker_write)))

    class Collection:
        async def find_one(self, query, projection=None):
            return None

        async def find_one_and_update(self, query, update, **kwargs):
            writes.append(update)
            return update["$setOnInsert"]

    monkeypatch.setattr(SessionEventDocument, "get_pymongo_collection", classmethod(lambda cls: Collection()))
    repository = MongoSessionRepository()

    async def reserve(session_id, event):
        event.seq = 1
        return 1

    monkeypatch.setattr(repository, "reserve_event_sequence", reserve)
    event = MessageEvent(id="accepted", role="user", message="hello")
    await repository.add_input_event("session", event, InputAdmission(actor_user_id="user"))
    assert len(writes) == 1
    document = writes[0]["$setOnInsert"]
    assert document["event"]["message"] == "hello"
    assert document["input_admission"]["state"] == "pending"
    assert "input_admission" not in document["event"]
    assert [call.args[1] for call in marker_write.await_args_list] == [
        {"$max": {"latest_user_input_fence_seq": 1}}, {"$max": {"latest_user_event_seq": 1}},
    ]


@pytest.mark.asyncio
async def test_existing_legacy_event_is_never_promoted_to_pending_on_retry(monkeypatch):
    event = MessageEvent(id="legacy", role="user", message="already ran", seq=1)
    repository = MongoSessionRepository()
    monkeypatch.setattr(SessionDocument, "get_pymongo_collection", classmethod(lambda cls: SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))))
    digest = repository._event_payload_digest(event)

    class Collection:
        async def find_one(self, query, projection=None):
            return {"seq": 1, "event": event.model_dump(), "payload_digest": digest}

        async def find_one_and_update(self, *args, **kwargs):
            pytest.fail("a legacy event must never acquire pending admission")

    monkeypatch.setattr(SessionEventDocument, "get_pymongo_collection", classmethod(lambda cls: Collection()))
    async def reserve(session_id, event):
        return 1
    monkeypatch.setattr(repository, "reserve_event_sequence", reserve)
    await repository.add_input_event("session", event, InputAdmission(actor_user_id="user"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["enqueue_unknown", "started_unknown"])
async def test_durable_domain_reconciles_transport_failure_without_replaying_started_input(failure):
    from app.application.services.dataset_request_resolver import FrontControllerResolution, RequestDecision, ExecutionDecision
    from app.domain.models.safety import SafetyReview

    repository = MemoryInputs()
    tasks = {}

    class QueueTask(FakeTask):
        accepting_input = True

        @classmethod
        def get(cls, task_id):
            return tasks.get(task_id)

        async def wait_closed(self):
            return

        async def enqueue_input(self, payload):
            self.payload = payload
            if self.id == "task-0" and failure == "enqueue_unknown":
                raise ConnectionError("injected XADD committed but reply lost")

        async def run(self):
            event = MessageEvent.model_validate_json(self.payload)
            await service._input_delivery.start("session", event, self)
            if self.id == "task-0" and failure == "started_unknown":
                raise ConnectionError("injected failure after execution admission")

    service = AgentDomainService(agent_repository=object(), session_repository=repository, sandbox_cls=object,
        task_cls=QueueTask, file_storage=object(), mcp_repository=object(), sandbox_runtime=object(), input_repository=repository)
    resolution = FrontControllerResolution(decision=RequestDecision(safety=SafetyReview(decision="allow", risk_level="low"),
        execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")), answer="", controller_metadata={})
    histories = []

    async def resolve(**kwargs):
        histories.append(kwargs["events"])
        return resolution

    async def create(session, ids, **kwargs):
        task = QueueTask(f"task-{len(tasks)}")
        tasks[task.id] = task
        session.task_id = task.id
        assert kwargs["session_events_snapshot"][-1].role == "user"
        return task

    service._dataset_request_resolver = SimpleNamespace(resolve=resolve)
    service._create_task = create
    async def submit():
        return await service._bootstrap_chat_task(session=repository.session, user_id="user", message="analyse",
            timestamp=None, attachments=None, skills=None, mcp_servers=None, dataset_ids=None,
            mcp_access_all=False, client_message_id="stable-client-id")

    # Once admission commits, transient transport/preparation failure belongs
    # to recovery, not to a fabricated business-terminal SSE error.
    assert await submit() is None
    record = next(iter(repository.records.values()))
    assert len(repository.events) == 1
    assert histories == [[]]
    if failure == "enqueue_unknown":
        assert record.admission.state == "pending"
        await submit()
        await asyncio.gather(*tuple(service._chat_bootstrap_tasks))
        replacement = tasks["task-1"]
        assert replacement.id == "task-1"
        with pytest.raises(InputLeaseLost):
            await service._input_delivery.start("session", MessageEvent.model_validate_json(tasks["task-0"].payload), tasks["task-0"])
        assert (await repository.get("session", record.key)).admission.state == "running"
    else:
        assert record.admission.state == "running"
        assert await submit() is tasks["task-0"]
        assert len(tasks) == 1
    assert len(repository.events) == 1


@pytest.mark.asyncio
async def test_stop_generation_fences_input_accepted_after_remote_stop_and_allows_new_input():
    repository = MemoryInputs()
    first = InputDeliveryService(repository, repository)
    second = InputDeliveryService(repository, repository)
    before_stop = await repository.generation("session")
    await second.cancel_session("session")
    stale = await repository.accept("session", "user", MessageEvent(id="before-stop", role="user", message="old"),
                                    generation=before_stop)
    assert await first.claim(stale) is None
    assert (await repository.get("session", stale.key)).admission.state == "cancelled"
    fresh = await repository.accept("session", "user", MessageEvent(id="after-stop", role="user", message="new"))
    claimed = await first.claim(fresh)
    assert claimed is not None
    assert claimed.admission.session_generation == before_stop + 1


@pytest.mark.asyncio
async def test_durable_dispatch_restores_legacy_datasets_from_one_strict_history_snapshot():
    from app.application.services.dataset_request_resolver import FrontControllerResolution, RequestDecision, ExecutionDecision
    from app.domain.models.safety import SafetyReview
    from unittest.mock import AsyncMock

    repository = MemoryInputs()
    repository.events = [
        MessageEvent(seq=1, role="user", message="older", metadata={"dataset_ids": ["older"]}),
        MessageEvent(seq=2, role="user", message="prior", metadata={"dataset_ids": ["retained", "retained"]}),
    ]
    record = await repository.accept("session", "user", MessageEvent(id="current", role="user", message="continue",
                                                                      metadata={"dataset_ids": []}))
    repository.events.append(MessageEvent(seq=record.event.seq + 1, role="user", message="future",
                                         metadata={"dataset_ids": ["future"]}))
    service = AgentDomainService(agent_repository=object(), session_repository=repository, sandbox_cls=object,
        task_cls=SimpleNamespace(get=lambda _: None), file_storage=object(), mcp_repository=object(),
        sandbox_runtime=object(), input_repository=repository)
    resolution = FrontControllerResolution(decision=RequestDecision(safety=SafetyReview(decision="allow", risk_level="low"),
        execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")), answer="", controller_metadata={})
    service._dataset_service = SimpleNamespace(get_dataset=AsyncMock(return_value="dataset-summary"))
    service._dataset_request_resolver = SimpleNamespace(resolve=AsyncMock(return_value=resolution))
    task = FakeTask()
    task.enqueue_input = AsyncMock()
    task.run = AsyncMock()
    service._create_task = AsyncMock(return_value=task)
    claimed = await service._input_delivery.claim(record)
    assert await service._dispatch_claimed_input(claimed) is task
    service._dataset_service.get_dataset.assert_awaited_once_with("retained", user_id="user")
    assert repository.history_reads == 1
    assert service._dataset_request_resolver.resolve.await_args.kwargs["events"] == repository.events[:2]
    assert service._create_task.await_args.args[1] == ["retained"]
    assert service._create_task.await_args.kwargs["session_events_snapshot"] == repository.events[:3]
    # The derived sandbox binding must not alter the already accepted payload.
    assert (await repository.get("session", record.key)).event.metadata == {"dataset_ids": []}
    assert MessageEvent.model_validate_json(task.enqueue_input.await_args.args[0]).metadata == {"dataset_ids": []}


@pytest.mark.asyncio
async def test_terminal_is_durable_when_redis_publication_fails():
    from app.domain.services.lightweight_task_runner import LightweightTaskRunner
    repository = MemoryInputs()
    async def reserve(session_id, event):
        event.seq = 9
        return 9
    repository.reserve_event_sequence = reserve
    async def fail_put(payload):
        assert repository.events[-1].type == "done"
        raise ConnectionError("injected Redis outage")
    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id = "session"
    runner._session_repository = repository
    await runner._publish(SimpleNamespace(output_stream=SimpleNamespace(put=fail_put)), DoneEvent())
    assert len(repository.events) == 1
    assert repository.events[0].seq == 9


@pytest.mark.asyncio
async def test_event_id_only_client_resolves_producer_id_to_durable_seq():
    repository = MemoryInputs()
    calls = []
    async def resolve(session_id, event_id):
        calls.append((session_id, event_id))
        return 5
    async def after(session_id, seq):
        assert seq == 5
        return [MessageEvent(message="saved answer", seq=6), DoneEvent(seq=7)]
    async def unread(*args):
        return
    repository.resolve_event_sequence = resolve
    repository.get_events_after = after
    repository.update_unread_message_count = unread
    service = AgentDomainService(agent_repository=object(), session_repository=repository, sandbox_cls=object,
        task_cls=SimpleNamespace(get=lambda _: None), file_storage=object(), mcp_repository=object(), sandbox_runtime=object())
    events = [event async for event in service.chat(session_id="session", user_id="user",
        latest_event_id="producer-uuid-before-alias-was-written")]
    assert calls == [("session", "producer-uuid-before-alias-was-written")]
    assert [event.seq for event in events] == [6, 7]
