"""Synthetic shutdown owners; no Redis, Mongo, sandbox or model connections."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.external.task import TaskInputClosedError
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.tools.mcp import MCPToolkit
from app.domain.utils.shutdown import join_cleanup
from app.infrastructure.external.task import redis_task


@pytest.fixture
def registry(monkeypatch):
    cls = redis_task.RedisStreamTask
    monkeypatch.setattr(cls, "_task_registry", {})
    monkeypatch.setattr(cls, "_destroy_task", None)
    monkeypatch.setattr(cls, "_shutting_down", False)
    monkeypatch.setattr(redis_task, "RedisStreamQueue", lambda _: object())
    return cls


class Runner:
    def __init__(self, name, log, *, fail=None, release=None):
        self.name, self.log, self.fail, self.release = name, log, fail, release
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()

    async def run(self, task):
        self.started.set()
        await asyncio.Event().wait()

    async def on_done(self, task):
        self.cleaning.set()
        if self.release is not None:
            await self.release.wait()
        self.log.append(self.name + ".on_done")
        if self.fail == "on_done":
            raise OSError("synthetic on_done failure")

    async def destroy(self):
        self.log.append(self.name + ".destroy")
        if self.fail == "destroy":
            raise OSError("synthetic destroy failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_index", [0, 1, 2])
@pytest.mark.parametrize("failure", ["on_done", "destroy"])
async def test_one_failed_cleanup_does_not_skip_any_runner(registry, failed_index, failure):
    log = []
    runners = [Runner(str(i), log, fail=failure if i == failed_index else None) for i in range(3)]
    tasks = [registry(runner) for runner in runners]
    for task in tasks:
        await task.run()
    await asyncio.gather(*(runner.started.wait() for runner in runners))
    with pytest.raises(OSError):
        await registry.destroy()
    assert {name for name in log if name.endswith(".destroy")} == {"0.destroy", "1.destroy", "2.destroy"}
    assert not registry._task_registry
    assert all(task._closed.is_set() for task in tasks)
    await registry.destroy()
    assert len(log) == 6  # No rerun after failure or repeated teardown.


@pytest.mark.asyncio
async def test_multiple_failures_are_reported_after_all_owners_settle(registry):
    log = []
    runners = [Runner("one", log, fail="on_done"), Runner("two", log, fail="destroy")]
    tasks = [registry(runner) for runner in runners]
    for task in tasks:
        await task.run()
    await asyncio.gather(*(runner.started.wait() for runner in runners))
    with pytest.raises(ExceptionGroup) as raised:
        await registry.destroy()
    assert len(raised.value.exceptions) == 2
    assert log[-2:] == ["one.destroy", "two.destroy"]


@pytest.mark.asyncio
async def test_repeated_cancel_and_concurrent_destroy_join_the_same_cleanup(registry):
    release, log = asyncio.Event(), []
    runner = Runner("one", log, release=release)
    task = registry(runner)
    await task.run()
    await runner.started.wait()
    task.cancel()  # User cancellation precedes application shutdown.
    await runner.cleaning.wait()
    first = asyncio.create_task(registry.destroy())
    await asyncio.sleep(0)
    second = asyncio.create_task(registry.destroy())
    await asyncio.sleep(0)
    with pytest.raises(TaskInputClosedError):
        registry(Runner("late", log))
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    assert not task._closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert log == ["one.on_done", "one.destroy"]
    assert task._closed.is_set()


@pytest.mark.asyncio
async def test_domain_failure_still_joins_prewarm_and_does_not_retry():
    stopped, started = asyncio.Event(), asyncio.Event()

    async def prewarm():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(prewarm())
    await started.wait()
    service = object.__new__(AgentDomainService)
    service._chat_bootstrap_tasks = set()
    service._jupyter_prewarm_tasks = {task}
    service._task_cls = SimpleNamespace(destroy=AsyncMock(side_effect=OSError("synthetic registry failure")))
    for _ in range(2):
        with pytest.raises(OSError):
            await service.shutdown()
    assert stopped.is_set() and task.done()
    service._task_cls.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_cleanup_error_still_closes_registry_then_prewarm():
    started = asyncio.Event()
    events = []

    async def bootstrap():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("bootstrap")
            raise OSError("synthetic bootstrap cleanup failure")

    async def prewarm():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("prewarm")

    async def destroy():
        events.append("registry")

    bootstrap_task = asyncio.create_task(bootstrap())
    prewarm_task = asyncio.create_task(prewarm())
    await started.wait()
    await asyncio.sleep(0)
    service = object.__new__(AgentDomainService)
    service._chat_bootstrap_tasks = {bootstrap_task}
    service._jupyter_prewarm_tasks = {prewarm_task}
    service._task_cls = SimpleNamespace(destroy=destroy)
    with pytest.raises(OSError):
        await service.shutdown()
    assert events == ["bootstrap", "registry", "prewarm"]


@pytest.mark.asyncio
async def test_unstarted_task_is_closed_without_running_it(registry):
    log = []
    runner = Runner("unstarted", log)
    task = registry(runner)
    await registry.destroy()
    assert log == ["unstarted.destroy"]
    assert not runner.started.is_set()
    assert task._closed.is_set()


@pytest.mark.asyncio
async def test_runner_sandbox_failure_cannot_skip_mcp_cleanup():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "synthetic"
    runner._sandbox = SimpleNamespace(destroy=AsyncMock(side_effect=OSError("synthetic sandbox failure")))
    runner._mcp_tool = SimpleNamespace(cleanup=AsyncMock())
    for _ in range(2):
        with pytest.raises(OSError):
            await runner.destroy()
    runner._sandbox.destroy.assert_awaited_once()
    runner._mcp_tool.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_is_preserved_when_owned_cleanup_also_fails():
    release = asyncio.Event()

    async def cleanup():
        await release.wait()
        raise OSError("synthetic cleanup failure")

    owned = asyncio.create_task(cleanup())
    caller = asyncio.create_task(join_cleanup(owned))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    assert isinstance(raised.value.__cause__, OSError)
    assert owned.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True], ids=["shutdown", "user-then-shutdown"])
async def test_natural_completion_finalizer_survives_first_late_cancellation(registry, cancel_first):
    cleaning, release = asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(closed=False, owner=None)

    async def manager_cleanup():
        state.owner = asyncio.current_task()
        cleaning.set()
        await release.wait()
        state.closed = True

    toolkit = MCPToolkit()
    toolkit.manager = SimpleNamespace(cleanup=manager_cleanup)
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "synthetic"
    runner._browser = runner._sandbox = runner._flow = None
    runner._mcp_tool = toolkit
    runner.run = AsyncMock(return_value=None)  # Natural completion, not cancellation.
    task = registry(runner)
    await task.run()
    await cleaning.wait()
    shutdown = None
    try:
        if cancel_first:
            assert task.cancel() is True
            await asyncio.sleep(0)
            assert not task._closed.is_set()
        shutdown = asyncio.create_task(registry.destroy())
        for _ in range(3):
            await asyncio.sleep(0)
        assert not shutdown.done()
        assert not task._closed.is_set()
        assert toolkit.manager is not None
        assert not state.closed
        assert state.owner is task._execution_task
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        else:
            await task.wait_closed()
    assert state.closed
    assert toolkit.manager is None
    assert task._closed.is_set()
    runner.run.assert_awaited_once()
