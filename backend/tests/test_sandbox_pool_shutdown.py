"""Pool lifecycle regressions using controlled tasks, never Docker or databases."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.external.sandbox.sandbox_pool import SandboxPool


async def ticks():
    for _ in range(5):
        await asyncio.sleep(0)


class Sandbox:
    id = "synthetic-warm"
    ip = "127.0.0.1"

    def __init__(self):
        self.destroy = AsyncMock(return_value=True)
        self.is_available = AsyncMock(return_value=True)
        self.ensure_api_ready = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_shutdown_joins_cancelled_replenishment_without_resurrection():
    pool = SandboxPool(1)
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def create(**kwargs):
        calls.append("create")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    pool._create_and_warm = create
    pool.schedule_replenish()
    await started.wait()
    first = asyncio.create_task(pool.shutdown())
    second = asyncio.create_task(pool.shutdown())
    await cleaning.wait()
    first.cancel()
    await ticks()
    first.cancel()
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    await ticks()
    assert calls == ["create"]
    assert not pool._background_tasks
    assert not pool._foreground_tasks
    pool.schedule_replenish()
    with pytest.raises(RuntimeError, match="shutting down"):
        pool.start_background_init()
    with pytest.raises(RuntimeError, match="shutting down"):
        await pool.acquire()
    with pytest.raises(RuntimeError, match="shutting down"):
        await pool.resize(5)
    await pool.shutdown()
    assert calls == ["create"]


@pytest.mark.asyncio
async def test_shutdown_drains_every_sandbox_and_remembers_failure():
    pool = SandboxPool(2)
    first, second = Sandbox(), Sandbox()
    first.destroy.side_effect = RuntimeError("first destroy failed")
    second.destroy.return_value = False
    pool._pool.put_nowait(first)
    pool._pool.put_nowait(second)
    for _ in range(2):
        with pytest.raises(ExceptionGroup) as caught:
            await pool.shutdown()
        assert len(caught.value.exceptions) == 2
    first.destroy.assert_awaited_once()
    second.destroy.assert_awaited_once()
    assert pool.warm_count == 0


@pytest.mark.asyncio
async def test_shutdown_waits_for_cancelled_checkout_before_releasing_pool():
    pool = SandboxPool(1)
    sandbox = Sandbox()
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def available():
        started.set()
        await asyncio.Event().wait()

    async def destroy():
        cleaning.set()
        await release.wait()
        return True

    sandbox.is_available.side_effect = available
    sandbox.destroy.side_effect = destroy
    pool._pool.put_nowait(sandbox)
    acquire = asyncio.create_task(pool.acquire())
    await started.wait()
    closing = asyncio.create_task(pool.shutdown())
    await cleaning.wait()
    assert not closing.done()
    acquire.cancel()
    await ticks()
    release.set()
    await closing
    with pytest.raises(asyncio.CancelledError):
        await acquire
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_at_successful_acquire_handoff_destroys_undelivered_handle():
    pool = SandboxPool(0)
    sandbox = Sandbox()
    caller = None

    async def create(**kwargs):
        asyncio.get_running_loop().call_soon(caller.cancel)
        return sandbox

    pool._create_and_warm = create
    caller = asyncio.create_task(pool.acquire())
    with pytest.raises(asyncio.CancelledError):
        await caller
    sandbox.destroy.assert_awaited_once()
    await pool.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_caller", [False, True])
@pytest.mark.parametrize("destroy_fails", [False, True])
async def test_shutdown_joins_completed_child_until_handoff_retires_handle(cancel_caller, destroy_fails):
    pool = SandboxPool(0)
    sandbox = Sandbox()
    cleaning, release = asyncio.Event(), asyncio.Event()
    closing_started = asyncio.get_running_loop().create_future()

    def begin_shutdown():
        closing_started.set_result(asyncio.create_task(pool.shutdown()))

    async def create(**kwargs):
        # Queue closure before shield wakes the receiver of this successful child.
        asyncio.get_running_loop().call_soon(begin_shutdown)
        return sandbox

    async def destroy():
        cleaning.set()
        await release.wait()
        if destroy_fails:
            raise RuntimeError("synthetic handoff cleanup failure")
        return True

    pool._create_and_warm = create
    sandbox.destroy.side_effect = destroy
    acquire = asyncio.create_task(pool.acquire())
    closing = await closing_started
    await cleaning.wait()
    assert not closing.done()
    if cancel_caller:
        acquire.cancel()
        await ticks()
        acquire.cancel()
    assert not closing.done()
    release.set()
    expected = RuntimeError if destroy_fails and not cancel_caller else asyncio.CancelledError
    with pytest.raises(expected):
        await acquire
    if destroy_fails:
        with pytest.raises(RuntimeError, match="handoff cleanup failure"):
            await closing
    else:
        await closing
    sandbox.destroy.assert_awaited_once()
    assert not pool._foreground_tasks and not pool._foreground_settled


@pytest.mark.asyncio
async def test_shutdown_joins_initialization_and_does_not_enter_replenishment():
    pool = SandboxPool(1)
    started, release, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def adopt():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    pool._adopt_or_retire_existing_warm_sandboxes = adopt
    pool._create_and_warm = AsyncMock()
    pool.start_background_init()
    await started.wait()
    closing = asyncio.create_task(pool.shutdown())
    await cleanup.wait()
    assert not closing.done()
    release.set()
    await closing
    pool._create_and_warm.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_resize_still_retires_all_removed_sandboxes():
    pool = SandboxPool(2)
    first, second = Sandbox(), Sandbox()
    started, release = asyncio.Event(), asyncio.Event()

    async def destroy():
        started.set()
        await release.wait()
        return True

    first.destroy.side_effect = destroy
    pool._pool.put_nowait(first)
    pool._pool.put_nowait(second)
    resize = asyncio.create_task(pool.resize(0))
    await started.wait()
    resize.cancel()
    closing = asyncio.create_task(pool.shutdown())
    await ticks()
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await resize
    await closing
    first.destroy.assert_awaited_once()
    second.destroy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["record", "ready", "save"])
@pytest.mark.parametrize("destroy_fails", [False, True])
async def test_warmup_cancellation_retires_handle_once(monkeypatch, stage, destroy_fails):
    from app.infrastructure.external.sandbox import runtime, docker_sandbox
    from app.infrastructure.models import documents
    sandbox = Sandbox()
    if destroy_fails:
        sandbox.destroy.side_effect = RuntimeError("synthetic cleanup failure")
    started = asyncio.Event()

    async def pause():
        started.set()
        await asyncio.Event().wait()

    async def record(*args, **kwargs):
        if stage == "record":
            await pause()

    async def ready():
        if stage == "ready":
            await pause()

    async def save():
        if stage == "save":
            await pause()

    sandbox.ensure_api_ready.side_effect = ready
    monkeypatch.setattr(runtime.LocalDockerRuntime, "allocate_local_warm", AsyncMock(return_value=sandbox))
    monkeypatch.setattr(docker_sandbox.DockerSandbox, "create_record", record)
    monkeypatch.setattr(documents, "SandboxRecordDocument", SimpleNamespace(
        container_name="container_name", find_one=AsyncMock(return_value=SimpleNamespace(save=save)),
    ))
    pool = SandboxPool(0)
    task = asyncio.create_task(pool._create_and_warm())
    await started.wait()
    task.cancel()
    if destroy_fails:
        with pytest.raises(BaseExceptionGroup) as caught:
            await task
        assert isinstance(caught.value.exceptions[0], asyncio.CancelledError)
        assert isinstance(caught.value.exceptions[1], RuntimeError)
    else:
        with pytest.raises(asyncio.CancelledError):
            await task
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["creation", "allocation_record"])
@pytest.mark.parametrize("destroy_fails", [False, True])
async def test_runtime_owns_allocated_handle_through_cancellation(monkeypatch, stage, destroy_fails):
    from app.infrastructure.external.sandbox import runtime
    sandbox = Sandbox()
    if destroy_fails:
        sandbox.destroy.side_effect = RuntimeError("synthetic cleanup failure")
    started, release = asyncio.Event(), asyncio.Event()
    node = SimpleNamespace(node_id="synthetic-node")

    @asynccontextmanager
    async def admit(*args, **kwargs):
        yield [node]

    async def create(node):
        if stage == "creation":
            started.set()
            await release.wait()
        return sandbox

    async def upsert(**kwargs):
        if stage == "allocation_record":
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_admit_execution_node_candidates", admit)
    monkeypatch.setattr(runtime, "_upsert_allocation", upsert)
    manager = runtime.LocalDockerRuntime()
    monkeypatch.setattr(manager, "_allocate_on_node", create)
    task = asyncio.create_task(manager.allocate_local_warm())
    await started.wait()
    task.cancel()
    if stage == "creation":
        await ticks()
        task.cancel()  # The creation handle still belongs to the cancelled owner.
        assert not task.done()
        release.set()
    if destroy_fails:
        with pytest.raises(BaseExceptionGroup):
            await task
    else:
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_capacity_wait_is_still_cancellable_before_creation(monkeypatch):
    from app.infrastructure.external.sandbox import runtime
    started = asyncio.Event()

    @asynccontextmanager
    async def admit(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        yield []

    monkeypatch.setattr(runtime, "_admit_execution_node_candidates", admit)
    manager = runtime.LocalDockerRuntime()
    create = AsyncMock()
    monkeypatch.setattr(manager, "_allocate_on_node", create)
    task = asyncio.create_task(manager.allocate_local_warm())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    create.assert_not_awaited()
