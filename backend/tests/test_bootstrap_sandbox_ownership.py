"""Pool/runtime handoff belongs to bootstrap until a Task has registered."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.models.session import Session
from app.domain.services import agent_domain_service as module
from app.infrastructure.external.sandbox.sandbox_pool import SandboxPool
from app.infrastructure.repositories import mongo_session_repository as mongo_module


class Repository:
    def __init__(self, session):
        self.current = session.model_copy(deep=True)
        self.clear_calls = []
        self.save = AsyncMock(side_effect=self._save)

    async def _save(self, session):
        self.current = session.model_copy(deep=True)

    async def find_by_id(self, _session_id):
        return self.current.model_copy(deep=True)

    async def compare_and_clear_sandbox(self, session_id, **expected):
        self.clear_calls.append((session_id, expected))
        if (self.current.sandbox_id != expected["expected_sandbox_id"]
                or self.current.task_id != expected["expected_task_id"]):
            return False
        self.current.sandbox_id = None
        self.current.sandbox_dataset_ids = []
        return True


@pytest.fixture
def bootstrap(monkeypatch):
    session = Session(id="session", user_id="owner", agent_id="agent", task_id="previous")
    repository = Repository(session)
    sandbox = SimpleNamespace(
        id="new-sandbox", is_available=AsyncMock(return_value=True),
        get_browser=AsyncMock(return_value=object()),
        destroy=AsyncMock(return_value=True), pause=AsyncMock(return_value=True),
    )
    pool = SandboxPool(1)
    pool._pool.put_nowait(sandbox)
    monkeypatch.setattr(pool, "schedule_replenish", lambda: None)
    monkeypatch.setattr(module, "get_sandbox_pool", lambda: pool)
    runtime = SimpleNamespace(
        assign=AsyncMock(), allocate=AsyncMock(return_value=sandbox),
        restore=AsyncMock(return_value=sandbox),
    )
    task = SimpleNamespace(id="registered-task")
    task_cls = SimpleNamespace(create=Mock(return_value=task), destroy=AsyncMock())
    service = object.__new__(module.AgentDomainService)
    for name in (
        "_repository", "_file_storage", "_search_engine", "_mcp_repository",
        "_plugin_runtime", "_spill_artifact_store", "_analysis_job_service",
        "_tool_approval_service", "_credential_service", "_input_delivery",
    ):
        setattr(service, name, None)
    service._task_cls = task_cls
    service._session_repository = repository
    service._sandbox_runtime = runtime
    service._ensure_plugin_runtime_ready = AsyncMock()
    service._finish_task_preparation = AsyncMock(return_value=task)
    service._prewarm_jupyter = Mock()
    service._chat_bootstrap_tasks = set()
    service._chat_bootstrap_sessions = {}
    service._jupyter_prewarm_tasks = set()
    monkeypatch.setattr(module, "AgentTaskRunner", Mock(return_value=object()))
    return SimpleNamespace(
        service=service, session=session, repository=repository, sandbox=sandbox,
        runtime=runtime, pool=pool, task_cls=task_cls, task=task,
    )


def start(state, dataset_ids=None):
    return asyncio.create_task(state.service._create_task(state.session, dataset_ids))


def error_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in error_leaves(child)]
    return [error]


@pytest.mark.asyncio
async def test_pool_handoff_shutdown_waits_for_assignment_before_destroy(bootstrap):
    s = bootstrap
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def assign(*_args):
        entered.set()
        await release.wait()
        completed.append("assigned")

    async def destroy():
        assert completed == ["assigned"]
        return True

    s.runtime.assign.side_effect = assign
    s.sandbox.destroy.side_effect = destroy
    creating = s.service._track_chat_bootstrap(start(s), s.session.id)
    await asyncio.wait_for(entered.wait(), 1)
    # Actual main shutdown order: the pool has already relinquished the handle.
    await s.pool.shutdown()
    assert not s.pool._foreground_tasks and not s.pool.warm_count
    closing = asyncio.create_task(s.service.shutdown())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    assert not closing.done()
    s.sandbox.destroy.assert_not_awaited()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 1)
    assert creating.cancelled()
    s.sandbox.destroy.assert_awaited_once()
    s.task_cls.create.assert_not_called()
    s.task_cls.destroy.assert_awaited_once()
    assert not s.repository.clear_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["assign", "save-first", "browser", "save-second", "runner", "task"])
async def test_failed_new_bootstrap_retires_exactly_once(bootstrap, monkeypatch, stage):
    s = bootstrap
    failure = RuntimeError("bootstrap failed")
    if stage == "assign":
        s.runtime.assign.side_effect = failure
    elif stage.startswith("save"):
        async def save(session):
            await s.repository._save(session)  # Simulate a write before the error.
            if s.repository.save.await_count == (1 if stage == "save-first" else 2):
                raise failure
        s.repository.save.side_effect = save
    elif stage == "browser":
        s.sandbox.get_browser.side_effect = failure
    elif stage == "runner":
        monkeypatch.setattr(module, "AgentTaskRunner", Mock(side_effect=failure))
    else:
        s.task_cls.create.side_effect = failure
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        await s.service._create_task(s.session)
    s.sandbox.destroy.assert_awaited_once()
    s.sandbox.pause.assert_not_awaited()
    assert s.repository.current.sandbox_id is None
    assert s.session.sandbox_id is None
    assert s.repository.current.task_id == "previous"
    s.service._finish_task_preparation.assert_not_awaited()
    s.service._prewarm_jupyter.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["save-first", "save-second"])
async def test_cancelled_save_settles_before_clearing_pointer(bootstrap, stage):
    s = bootstrap
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(session):
        if s.repository.save.await_count == (1 if stage == "save-first" else 2):
            entered.set()
            await release.wait()
        await s.repository._save(session)

    s.repository.save.side_effect = save
    creating = start(s)
    await asyncio.wait_for(entered.wait(), 1)
    creating.cancel()
    await asyncio.sleep(0)
    creating.cancel()
    assert not creating.done()
    s.sandbox.destroy.assert_not_awaited()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(creating, 1)
    assert s.repository.current.sandbox_id is None
    assert s.repository.current.sandbox_dataset_ids == []
    assert len(s.repository.clear_calls) == 1
    s.sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["assign", "save-first"])
async def test_cancelled_external_write_failure_survives_gather(bootstrap, stage):
    s = bootstrap
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(*_args):
        entered.set()
        await release.wait()
        raise RuntimeError("accepted write failed")

    if stage == "assign":
        s.runtime.assign.side_effect = write
    else:
        s.repository.save.side_effect = write
    creating = start(s)
    await asyncio.wait_for(entered.wait(), 1)
    creating.cancel()
    await asyncio.sleep(0)
    release.set()
    [error] = await asyncio.gather(creating, return_exceptions=True)
    assert any(isinstance(leaf, RuntimeError) and str(leaf) == "accepted write failed"
               for leaf in error_leaves(error))
    s.sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_sandbox", [False, True])
async def test_cancelled_bootstrap_preserves_successor(bootstrap, same_sandbox):
    s = bootstrap
    entered, release = asyncio.Event(), asyncio.Event()

    async def browser():
        entered.set()
        await release.wait()
        return object()

    s.sandbox.get_browser.side_effect = browser
    creating = start(s)
    await asyncio.wait_for(entered.wait(), 1)
    s.repository.current.task_id = "successor-task"
    s.repository.current.sandbox_id = s.sandbox.id if same_sandbox else "successor-sandbox"
    s.repository.current.sandbox_dataset_ids = ["successor-dataset"]
    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(creating, 1)
    assert s.repository.current.task_id == "successor-task"
    assert s.repository.current.sandbox_id == (s.sandbox.id if same_sandbox else "successor-sandbox")
    assert s.repository.current.sandbox_dataset_ids == ["successor-dataset"]
    assert s.sandbox.destroy.await_count == (not same_sandbox)
    assert s.repository.clear_calls[0][1] == {
        "expected_sandbox_id": "new-sandbox", "expected_task_id": "previous",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("retirement failed")])
async def test_destroy_failure_is_aggregated_once_despite_repeated_cancel(bootstrap, failure):
    s = bootstrap
    entered, release = asyncio.Event(), asyncio.Event()
    s.sandbox.get_browser.side_effect = ValueError("browser failed")

    async def destroy():
        entered.set()
        await release.wait()
        if isinstance(failure, BaseException):
            raise failure
        return failure

    s.sandbox.destroy.side_effect = destroy
    creating = start(s)
    await asyncio.wait_for(entered.wait(), 1)
    creating.cancel()
    await asyncio.sleep(0)
    creating.cancel()
    release.set()
    [error] = await asyncio.gather(creating, return_exceptions=True)
    leaves = error_leaves(error)
    assert any(isinstance(leaf, ValueError) for leaf in leaves)
    assert any(isinstance(leaf, RuntimeError) for leaf in leaves)
    s.sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_cleanup_failure_visible_without_destroying_unknown_owner(bootstrap):
    s = bootstrap
    s.sandbox.get_browser.side_effect = ValueError("browser failed")
    s.repository.compare_and_clear_sandbox = AsyncMock(side_effect=RuntimeError("cleanup storage unavailable"))
    with pytest.raises(ExceptionGroup) as caught:
        await s.service._create_task(s.session)
    assert {str(leaf) for leaf in error_leaves(caught.value)} == {
        "browser failed", "cleanup storage unavailable",
    }
    s.sandbox.destroy.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_allocated_handle_is_also_bootstrap_owned(bootstrap):
    s = bootstrap
    s.sandbox.get_browser.side_effect = ValueError("browser failed")
    with pytest.raises(ValueError):
        await s.service._create_task(s.session, ["dataset"])
    s.runtime.allocate.assert_awaited_once_with(s.session, dataset_ids=["dataset"])
    s.runtime.assign.assert_not_awaited()
    s.sandbox.destroy.assert_awaited_once()
    assert s.repository.current.sandbox_id is None
    assert s.repository.current.sandbox_dataset_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_restored_environment_is_never_bootstrap_retired(bootstrap, cancel):
    s = bootstrap
    s.session.sandbox_id = s.sandbox.id
    s.repository.current.sandbox_id = s.sandbox.id
    s.sandbox.get_browser.side_effect = asyncio.CancelledError() if cancel else ValueError("browser failed")
    with pytest.raises(asyncio.CancelledError if cancel else ValueError):
        await s.service._create_task(s.session)
    s.sandbox.destroy.assert_not_awaited()
    s.sandbox.pause.assert_not_awaited()
    assert not s.repository.clear_calls


@pytest.mark.asyncio
async def test_task_registration_transfers_ownership_even_if_preparation_fails(bootstrap):
    s = bootstrap
    s.service._finish_task_preparation.side_effect = RuntimeError("registered task failed")
    with pytest.raises(RuntimeError, match="registered task failed"):
        await s.service._create_task(s.session)
    s.task_cls.create.assert_called_once()
    s.sandbox.destroy.assert_not_awaited()
    assert not s.repository.clear_calls
    assert s.repository.current.sandbox_id == s.sandbox.id


@pytest.mark.asyncio
async def test_success_moves_prewarm_after_registered_task_preparation(bootstrap):
    s = bootstrap
    async def prepared(*_args, **_kwargs):
        s.service._prewarm_jupyter.assert_not_called()
        return s.task
    s.service._finish_task_preparation.side_effect = prepared
    assert await s.service._create_task(s.session, ["dataset"]) is s.task
    s.service._prewarm_jupyter.assert_called_once_with(s.session, dataset_ids=["dataset"])
    s.sandbox.destroy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [0, 1])
@pytest.mark.parametrize("task_id", [None, "previous-task"])
async def test_mongo_clear_sandbox_cas_does_not_rewrite_task_or_dataset_selection(monkeypatch, matched, task_id):
    collection = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=matched)))
    monkeypatch.setattr(mongo_module, "SessionDocument", SimpleNamespace(get_pymongo_collection=lambda: collection))
    result = await mongo_module.MongoSessionRepository().compare_and_clear_sandbox(
        "session", expected_sandbox_id="owned-sandbox", expected_task_id=task_id,
    )
    assert result is bool(matched)
    query, update = collection.update_one.await_args.args
    assert query == {"session_id": "session", "sandbox_id": "owned-sandbox", "task_id": task_id}
    assert set(update["$set"]) == {"sandbox_id", "sandbox_dataset_ids", "updated_at"}
    assert update["$set"]["sandbox_id"] is None
    assert update["$set"]["sandbox_dataset_ids"] == []
