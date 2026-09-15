"""Preparation owns unstarted tasks; no real Redis, Mongo or sandbox is used."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.external.task import TaskInputClosedError
from app.domain.models.session import Session
from app.domain.services import agent_domain_service as module
from app.infrastructure.external.task import redis_task as task_module
from app.infrastructure.repositories import mongo_session_repository as mongo_module


class PreparationRepository:
    def __init__(self, session):
        self.task_id = session.task_id
        self.dataset_ids = list(session.dataset_ids)
        self.title = "unchanged title"
        self.calls = []
        self.fail_publish = None
        self.fail_rollback = False
        self.entered = None
        self.release = None

    async def save(self, session):
        # Sandbox preparation still saves its mount metadata before a Task is
        # created. Task publication itself must use CAS, never this full save.
        assert session.task_id == self.task_id

    async def compare_and_set_task_id(
        self, session_id, *, expected_task_id, task_id, dataset_ids=None,
    ):
        self.calls.append((expected_task_id, task_id, dataset_ids))
        publishing = dataset_ids is not None
        if publishing and self.entered is not None:
            self.entered.set()
            await self.release.wait()
        if publishing and self.fail_publish == "before_write":
            raise RuntimeError("publication failed")
        if not publishing and self.fail_rollback:
            raise RuntimeError("rollback storage unavailable")
        if self.task_id != expected_task_id:
            return False
        self.task_id = task_id
        if publishing:
            self.dataset_ids = list(dataset_ids)
        if publishing and self.fail_publish == "after_write":
            raise RuntimeError("publication failed")
        return True


@pytest.fixture
def prepared_service(monkeypatch):
    # Actual RedisStreamTask lifecycle, with inert queues so no Redis client is
    # connected. Keep all tests isolated from any pre-existing task registry.
    monkeypatch.setattr(task_module.RedisStreamTask, "_task_registry", {})
    monkeypatch.setattr(task_module, "RedisStreamQueue", lambda _name: object())
    runner = SimpleNamespace(destroy=AsyncMock(), on_done=AsyncMock())
    monkeypatch.setattr(module, "AgentTaskRunner", lambda **_kwargs: runner)
    monkeypatch.setattr(module, "LightweightTaskRunner", lambda **_kwargs: runner)
    session = Session(
        id="session", user_id="owner", agent_id="agent", task_id="previous-task",
        sandbox_id="reusable-sandbox", dataset_ids=["selected-dataset"],
        sandbox_dataset_ids=["selected-dataset"],
    )
    repository = PreparationRepository(session)
    sandbox = SimpleNamespace(
        id=session.sandbox_id, get_browser=AsyncMock(return_value=object()),
        destroy=AsyncMock(), pause=AsyncMock(),
    )
    runtime = SimpleNamespace(restore=AsyncMock(return_value=sandbox), assign=AsyncMock())
    service = object.__new__(module.AgentDomainService)
    for name in (
        "_repository", "_file_storage", "_search_engine", "_mcp_repository",
        "_plugin_runtime", "_spill_artifact_store", "_analysis_job_service",
        "_tool_approval_service", "_credential_service", "_input_delivery",
    ):
        setattr(service, name, None)
    service._task_cls = task_module.RedisStreamTask
    service._session_repository = repository
    service._sandbox_runtime = runtime
    service._ensure_plugin_runtime_ready = AsyncMock()
    service._prewarm_jupyter = lambda *_args, **_kwargs: None
    yield service, session, repository, runtime, sandbox, runner
    for task in list(task_module.RedisStreamTask._task_registry.values()):
        task.cancel()


async def create(service, session, kind):
    if kind == "lightweight":
        return await service._create_lightweight_task(session, SimpleNamespace(mode="direct"))
    return await service._create_task(session, session.dataset_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lightweight", "sandbox"])
@pytest.mark.parametrize("failure", ["before_write", "after_write"])
async def test_failed_publication_retries_leave_no_registered_tasks(prepared_service, kind, failure):
    service, session, repository, runtime, sandbox, runner = prepared_service
    repository.fail_publish = failure
    for _ in range(3):
        with pytest.raises(RuntimeError, match="publication failed"):
            await create(service, session, kind)
        assert not task_module.RedisStreamTask._task_registry
        assert repository.task_id == session.task_id == "previous-task"
    assert len([call for call in repository.calls if call[2] is not None]) == 3
    runtime.assign.assert_not_awaited()
    sandbox.destroy.assert_not_awaited()
    sandbox.pause.assert_not_awaited()
    runner.destroy.assert_not_awaited()
    runner.on_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_failure_rolls_back_pointer_but_keeps_reusable_sandbox(prepared_service):
    service, session, repository, runtime, sandbox, runner = prepared_service
    runtime.assign.side_effect = RuntimeError("assignment failed")
    with pytest.raises(RuntimeError, match="assignment failed"):
        await create(service, session, "sandbox")
    assert not task_module.RedisStreamTask._task_registry
    assert repository.task_id == session.task_id == "previous-task"
    assert session.sandbox_id == "reusable-sandbox"
    assert repository.dataset_ids == ["selected-dataset"]
    sandbox.destroy.assert_not_awaited()
    sandbox.pause.assert_not_awaited()
    runner.destroy.assert_not_awaited()
    runner.on_done.assert_not_awaited()
    # Never issue a blind assign(None) that could erase a successor's link.
    assert runtime.assign.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lightweight", "sandbox"])
async def test_cancellation_waits_for_publication_then_rolls_back(prepared_service, kind):
    service, session, repository, runtime, _sandbox, _runner = prepared_service
    repository.entered, repository.release = asyncio.Event(), asyncio.Event()
    creating = asyncio.create_task(create(service, session, kind))
    await repository.entered.wait()
    task = next(iter(task_module.RedisStreamTask._task_registry.values()))
    task.wait_closed = AsyncMock(wraps=task.wait_closed)
    creating.cancel()
    await asyncio.sleep(0)
    assert not task_module.RedisStreamTask._task_registry
    assert not task.accepting_input
    assert not creating.done()
    creating.cancel()  # A second shutdown/stop must not detach rollback.
    await asyncio.sleep(0)
    repository.release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert repository.task_id == session.task_id == "previous-task"
    task.wait_closed.assert_awaited_once()
    runtime.assign.assert_not_awaited()
    await asyncio.wait_for(task.wait_closed(), timeout=1)


@pytest.mark.asyncio
async def test_cancelled_assignment_cannot_erase_successor_pointer(prepared_service):
    service, session, repository, runtime, sandbox, runner = prepared_service
    entered, release = asyncio.Event(), asyncio.Event()

    async def assign(*_args):
        entered.set()
        await release.wait()

    runtime.assign.side_effect = assign
    creating = asyncio.create_task(create(service, session, "sandbox"))
    await entered.wait()
    assert repository.task_id != "previous-task"
    repository.task_id = "successor-task"
    repository.dataset_ids = ["successor-dataset"]
    creating.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert not task_module.RedisStreamTask._task_registry
    assert repository.task_id == "successor-task"
    assert repository.dataset_ids == ["successor-dataset"]
    assert repository.title == "unchanged title"
    sandbox.destroy.assert_not_awaited()
    sandbox.pause.assert_not_awaited()
    runner.destroy.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_conflict_does_not_overwrite_successor(prepared_service):
    service, session, repository, _runtime, _sandbox, _runner = prepared_service
    repository.task_id = "successor-task"
    with pytest.raises(TaskInputClosedError, match="changed during preparation"):
        await create(service, session, "lightweight")
    assert not task_module.RedisStreamTask._task_registry
    assert repository.task_id == "successor-task"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lightweight", "sandbox"])
async def test_success_publishes_selection_and_retains_unstarted_task(prepared_service, kind):
    service, session, repository, runtime, _sandbox, _runner = prepared_service
    session.dataset_ids = ["new-selection"]
    if kind == "sandbox":
        session.sandbox_dataset_ids = list(session.dataset_ids)
    task = await create(service, session, kind)
    assert repository.task_id == session.task_id == task.id
    assert repository.dataset_ids == ["new-selection"]
    assert task_module.RedisStreamTask.get(task.id) is task
    assert task.accepting_input
    assert task._execution_task is None
    assert runtime.assign.await_count == (kind == "sandbox")


@pytest.mark.asyncio
async def test_storage_cleanup_error_does_not_mask_original_or_leak_task(prepared_service):
    service, session, repository, _runtime, _sandbox, _runner = prepared_service
    repository.fail_publish = "after_write"
    repository.fail_rollback = True
    with pytest.raises(RuntimeError, match="publication failed"):
        await create(service, session, "lightweight")
    assert not task_module.RedisStreamTask._task_registry
    assert session.task_id == "previous-task"


@pytest.mark.asyncio
async def test_sandbox_save_failure_occurs_before_task_registration(prepared_service):
    service, session, repository, runtime, sandbox, _runner = prepared_service
    repository.save = AsyncMock(side_effect=RuntimeError("session save failed"))
    with pytest.raises(RuntimeError, match="session save failed"):
        await create(service, session, "sandbox")
    assert not task_module.RedisStreamTask._task_registry
    assert repository.calls == []
    runtime.assign.assert_not_awaited()
    sandbox.destroy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [0, 1])
async def test_mongo_pointer_cas_updates_only_owned_fields(monkeypatch, matched):
    collection = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=matched)))
    monkeypatch.setattr(mongo_module, "SessionDocument", SimpleNamespace(
        get_pymongo_collection=lambda: collection,
    ))
    repository = mongo_module.MongoSessionRepository()
    result = await repository.compare_and_set_task_id(
        "session", expected_task_id=None, task_id="prepared", dataset_ids=["dataset"],
    )
    assert result is bool(matched)
    query, update = collection.update_one.await_args.args
    assert query == {"session_id": "session", "task_id": None}
    assert update["$set"]["task_id"] == "prepared"
    assert update["$set"]["dataset_ids"] == ["dataset"]
    assert set(update["$set"]) == {"task_id", "dataset_ids", "updated_at"}
    await repository.compare_and_set_task_id(
        "session", expected_task_id="prepared", task_id=None,
    )
    query, update = collection.update_one.await_args.args
    assert query == {"session_id": "session", "task_id": "prepared"}
    assert set(update["$set"]) == {"task_id", "updated_at"}
