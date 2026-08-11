import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.application.services.agent_service import AgentService
from app.domain.models.event import MessageEvent
from app.domain.models.session import Session, SessionStatus
from app.domain.services.agent_domain_service import AgentDomainService
from app.infrastructure.models.documents import SessionDocument
from app.infrastructure.repositories.mongo_session_repository import (
    CLIENT_MESSAGE_ID_HISTORY_LIMIT,
    MongoSessionRepository,
)
from app.interfaces.schemas.session import ChatRequest
from app.domain.models.safety import SafetyReview
from app.application.services.dataset_request_resolver import (
    ExecutionDecision,
    FrontControllerResolution,
    RequestDecision,
)


class SandboxResolver:
    async def resolve(self, **_kwargs):
        return FrontControllerResolution(
            decision=RequestDecision(
                safety=SafetyReview(decision="allow", risk_level="low"),
                execution=ExecutionDecision(mode="sandbox", required_evidence="file_content"),
            ),
            answer="",
            controller_metadata={"prompt_version": "test", "execution_mode": "sandbox"},
        )


class FakeQueue:
    def __init__(self):
        self.messages = []
        self.message_ids = []
        self.next_id = 1
        self.fail_delete = False

    async def put(self, message):
        message_id = f"message-{self.next_id}"
        self.next_id += 1
        self.messages.append(message)
        self.message_ids.append(message_id)
        return message_id

    async def delete_message(self, message_id):
        if self.fail_delete or message_id not in self.message_ids:
            return False
        index = self.message_ids.index(message_id)
        self.message_ids.pop(index)
        self.messages.pop(index)
        return True

    async def is_empty(self):
        return not self.messages


class FakeTask:
    current = None

    def __init__(self):
        self.id = "task-1"
        self.done = False
        self.input_stream = FakeQueue()
        self.run_calls = 0
        self.fail_run_once = False

    async def run(self):
        self.run_calls += 1
        if self.fail_run_once:
            self.fail_run_once = False
            self.done = True
            raise RuntimeError("task start unavailable")
        self.done = False

    @classmethod
    def get(cls, task_id):
        if cls.current and cls.current.id == task_id:
            return cls.current
        return None


class FakeSessionRepository:
    def __init__(self):
        self.session = Session(
            id="session-1",
            user_id="user-1",
            agent_id="agent-1",
            task_id="task-1",
            status=SessionStatus.RUNNING,
        )
        self.claims = set()
        self.events = []
        self.latest_messages = []
        self.fail_latest_message_update = False
        self.fail_add_event_once = False

    async def claim_client_message_id(self, session_id, client_message_id):
        key = (session_id, client_message_id)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def release_client_message_id(self, session_id, client_message_id):
        self.claims.discard((session_id, client_message_id))

    async def find_by_id_and_user_id(self, session_id, user_id):
        if self.session.id == session_id and self.session.user_id == user_id:
            return self.session
        return None

    async def update_status(self, session_id, status):
        self.session.status = status

    async def update_latest_message(self, session_id, message, timestamp):
        if self.fail_latest_message_update:
            raise RuntimeError("latest message unavailable")
        self.latest_messages.append(message)

    async def add_event(self, session_id, event):
        if self.fail_add_event_once:
            self.fail_add_event_once = False
            raise RuntimeError("event persistence unavailable")
        if not any(existing.id == event.id for existing in self.events):
            self.events.append(event)

    async def save(self, session):
        self.session = session


def make_service(repository):
    service = AgentDomainService(
        agent_repository=object(),
        session_repository=repository,
        sandbox_cls=object(),
        task_cls=FakeTask,
        file_storage=object(),
        mcp_repository=object(),
        sandbox_runtime=object(),
    )
    service._dataset_request_resolver = SandboxResolver()
    return service


async def bootstrap(service, repository, client_message_id):
    return await service._bootstrap_chat_task(
        session=repository.session,
        user_id="user-1",
        message="visualize this dataset",
        timestamp=None,
        attachments=None,
        skills=None,
        mcp_servers=None,
        dataset_ids=None,
        mcp_access_all=False,
        client_message_id=client_message_id,
    )


@pytest.mark.asyncio
async def test_same_client_message_id_is_enqueued_once():
    repository = FakeSessionRepository()
    task = FakeTask()
    FakeTask.current = task
    service = make_service(repository)

    first = await bootstrap(service, repository, "client-message-1")
    second = await bootstrap(service, repository, "client-message-1")

    assert first is task
    assert second is task
    assert len(task.input_stream.messages) == 1
    assert task.run_calls == 1
    assert repository.latest_messages == ["visualize this dataset"]
    user_events = [event for event in repository.events if event.type == "message"]
    assert len(user_events) == 1
    payload = json.loads(task.input_stream.messages[0])
    assert payload["metadata"]["client_message_id"] == "client-message-1"


@pytest.mark.asyncio
async def test_missing_client_message_id_preserves_legacy_enqueue_behavior():
    repository = FakeSessionRepository()
    task = FakeTask()
    FakeTask.current = task
    service = make_service(repository)

    await bootstrap(service, repository, None)
    await bootstrap(service, repository, None)

    assert len(task.input_stream.messages) == 2
    assert task.run_calls == 2


@pytest.mark.asyncio
async def test_unqueued_message_releases_client_message_claim():
    repository = FakeSessionRepository()
    repository.fail_latest_message_update = True
    task = FakeTask()
    FakeTask.current = task
    service = make_service(repository)

    with pytest.raises(RuntimeError, match="latest message unavailable"):
        await bootstrap(service, repository, "retryable-message")

    assert repository.claims == set()
    assert task.input_stream.messages == []

    repository.fail_latest_message_update = False
    await bootstrap(service, repository, "retryable-message")
    assert len(task.input_stream.messages) == 1


@pytest.mark.asyncio
async def test_event_persistence_failure_happens_before_enqueue_and_is_retryable():
    repository = FakeSessionRepository()
    repository.fail_add_event_once = True
    task = FakeTask()
    FakeTask.current = task
    service = make_service(repository)

    with pytest.raises(RuntimeError, match="event persistence unavailable"):
        await bootstrap(service, repository, "persist-before-queue")

    assert repository.claims == set()
    assert task.input_stream.messages == []
    assert task.run_calls == 0

    await bootstrap(service, repository, "persist-before-queue")

    user_events = [event for event in repository.events if event.type == "message"]
    assert len(user_events) == 1
    assert len(task.input_stream.messages) == 1
    assert task.run_calls == 1


@pytest.mark.asyncio
async def test_task_start_failure_keeps_one_queue_entry_and_duplicate_resumes_it():
    repository = FakeSessionRepository()
    task = FakeTask()
    task.fail_run_once = True
    task.input_stream.fail_delete = True
    FakeTask.current = task
    service = make_service(repository)

    with pytest.raises(RuntimeError, match="task start unavailable"):
        await bootstrap(service, repository, "resume-queued-message")

    assert repository.claims == {("session-1", "resume-queued-message")}
    assert len(task.input_stream.messages) == 1
    assert task.run_calls == 1

    resumed = await bootstrap(service, repository, "resume-queued-message")

    assert resumed is task
    assert len(task.input_stream.messages) == 1
    assert task.run_calls == 2
    user_events = [event for event in repository.events if event.type == "message"]
    assert len(user_events) == 1


@pytest.mark.asyncio
async def test_persisted_claim_is_recovered_after_task_registry_loss():
    client_message_id = "restart-recovery"
    repository = FakeSessionRepository()
    repository.session.status = SessionStatus.RUNNING
    repository.session.task_id = "missing-task"
    repository.claims.add(("session-1", client_message_id))
    service = make_service(repository)
    repository.events.append(MessageEvent(
        id=service._client_message_event_id("session-1", client_message_id),
        role="user",
        message="visualize this dataset",
        metadata={"client_message_id": client_message_id},
    ))
    FakeTask.current = None
    replacement = FakeTask()
    replacement.id = "replacement-task"

    async def create_replacement_task(session, _dataset_ids=None, **_kwargs):
        session.task_id = replacement.id
        repository.session = session
        FakeTask.current = replacement
        return replacement

    service._create_task = create_replacement_task

    recovered = await bootstrap(service, repository, client_message_id)

    assert recovered is replacement
    assert repository.claims == {("session-1", client_message_id)}
    assert len(replacement.input_stream.messages) == 1
    assert replacement.run_calls == 1
    user_events = [event for event in repository.events if event.type == "message"]
    assert len(user_events) == 1


def test_chat_request_client_message_id_is_optional_and_bounded():
    assert ChatRequest(message="hello").client_message_id is None
    assert ChatRequest(
        message="hello",
        client_message_id="client-message-1",
    ).client_message_id == "client-message-1"

    with pytest.raises(ValidationError):
        ChatRequest(message="hello", client_message_id="")
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", client_message_id="x" * 129)


@pytest.mark.asyncio
async def test_mongo_claim_is_atomic_and_bounds_message_id_history(monkeypatch):
    class FakeCollection:
        def __init__(self):
            self.calls = []

        async def update_one(self, query, update):
            self.calls.append((query, update))
            return SimpleNamespace(matched_count=1)

    collection = FakeCollection()
    monkeypatch.setattr(
        SessionDocument,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )

    claimed = await MongoSessionRepository().claim_client_message_id(
        "session-1",
        "client-message-1",
    )

    assert claimed is True
    query, update = collection.calls[0]
    assert query == {
        "session_id": "session-1",
        "client_message_ids": {"$ne": "client-message-1"},
    }
    assert update["$push"]["client_message_ids"] == {
        "$each": ["client-message-1"],
        "$slice": -CLIENT_MESSAGE_ID_HISTORY_LIMIT,
    }


@pytest.mark.asyncio
async def test_agent_service_allows_empty_message_for_sse_resume():
    class FakeDomainService:
        async def chat(self, **kwargs):
            assert kwargs["message"] is None
            if False:
                yield None

    service = AgentService.__new__(AgentService)
    service._agent_domain_service = FakeDomainService()

    events = [
        event
        async for event in service.chat(
            session_id="session-1",
            user_id="user-1",
            message=None,
        )
    ]

    assert events == []
