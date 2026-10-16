"""Allocation handoffs without Docker, workers, databases or model requests."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.execution_node import ExecutionNodeType
from app.domain.models.session import Session
from app.infrastructure.external.sandbox import docker_sandbox as docker_module
from app.infrastructure.external.sandbox import runtime as runtime_module


@pytest.fixture
def allocation(monkeypatch):
    node = SimpleNamespace(
        node_id="local-default", type=ExecutionNodeType.LOCAL_DOCKER,
        base_url="http://offline.invalid", runtime_config={},
    )
    sandbox = SimpleNamespace(id="new-sandbox", ip="127.0.0.1", destroy=AsyncMock(return_value=True))
    monkeypatch.setattr(runtime_module, "_SANDBOX_ADMISSION_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime_module, "_list_execution_node_candidates", AsyncMock(return_value=[node]))
    monkeypatch.setattr(runtime_module, "mark_execution_node_unhealthy", AsyncMock())
    monkeypatch.setattr(runtime_module.DockerSandbox, "create", AsyncMock(return_value=sandbox))
    monkeypatch.setattr(runtime_module.DockerSandbox, "create_on_host", AsyncMock(return_value=sandbox))
    monkeypatch.setattr(runtime_module, "_create_worker_sandbox", AsyncMock(return_value=sandbox))
    monkeypatch.setattr(runtime_module.DockerSandbox, "create_record", AsyncMock())
    monkeypatch.setattr(runtime_module, "_upsert_allocation", AsyncMock())
    session = Session(id="offline-session", user_id="user", agent_id="agent")
    return runtime_module.LocalDockerRuntime(), node, sandbox, session


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,stage", [
    (ExecutionNodeType.LOCAL_DOCKER, "create"),
    (ExecutionNodeType.LOCAL_DOCKER, "record"),
    (ExecutionNodeType.LOCAL_DOCKER, "allocation"),
    (ExecutionNodeType.REMOTE_DOCKER, "create"),
    (ExecutionNodeType.WORKER_AGENT, "create"),
    (ExecutionNodeType.WORKER_AGENT, "allocation"),
])
async def test_cancel_waits_for_creation_and_late_writes_then_destroys_once(allocation, monkeypatch, kind, stage):
    runtime, node, sandbox, session = allocation
    node.type = kind
    entered, finish = asyncio.Event(), asyncio.Event()
    writes = []

    async def blocked(*args, **kwargs):
        assert runtime_module._SANDBOX_ADMISSION_LOCK.locked()
        entered.set()
        await finish.wait()
        writes.append(stage)
        return sandbox

    if stage == "record":
        monkeypatch.setattr(runtime_module.DockerSandbox, "create_record", blocked)
    elif stage == "allocation":
        monkeypatch.setattr(runtime_module, "_upsert_allocation", blocked)
    elif kind == ExecutionNodeType.WORKER_AGENT:
        monkeypatch.setattr(runtime_module, "_create_worker_sandbox", blocked)
    else:
        name = "create" if kind == ExecutionNodeType.LOCAL_DOCKER else "create_on_host"
        monkeypatch.setattr(runtime_module.DockerSandbox, name, blocked)

    async def destroy():
        assert writes == [stage]
        assert runtime_module._SANDBOX_ADMISSION_LOCK.locked()
        return True

    sandbox.destroy.side_effect = destroy
    task = asyncio.create_task(runtime.allocate(session))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    sandbox.destroy.assert_not_awaited()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    sandbox.destroy.assert_awaited_once()
    assert not runtime_module._SANDBOX_ADMISSION_LOCK.locked()
    runtime_module.mark_execution_node_unhealthy.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_child_not_yet_received_is_retired_on_cancel(allocation):
    runtime, _node, sandbox, session = allocation

    async def create(*args):
        asyncio.get_running_loop().call_soon(task.cancel)
        return sandbox

    runtime._allocate_on_node = create
    task = asyncio.create_task(runtime.allocate(session))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_during_destroy_keeps_admission_until_retirement(allocation):
    runtime, _node, sandbox, session = allocation
    created, finish_creation = asyncio.Event(), asyncio.Event()
    destroying, finish_destroy = asyncio.Event(), asyncio.Event()

    async def create(*args):
        created.set()
        await finish_creation.wait()
        return sandbox

    async def destroy():
        destroying.set()
        await finish_destroy.wait()
        return True

    runtime._allocate_on_node = create
    sandbox.destroy.side_effect = destroy
    task = asyncio.create_task(runtime.allocate(session))
    await asyncio.wait_for(created.wait(), 1)
    task.cancel()
    finish_creation.set()
    await asyncio.wait_for(destroying.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert runtime_module._SANDBOX_ADMISSION_LOCK.locked()
    finish_destroy.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    sandbox.destroy.assert_awaited_once()
    assert not runtime_module._SANDBOX_ADMISSION_LOCK.locked()


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, OSError("offline destroy error")])
async def test_registration_and_destroy_errors_are_retained_without_node_retry(allocation, monkeypatch, cleanup_failure):
    runtime, node, sandbox, session = allocation
    monkeypatch.setattr(runtime_module, "_list_execution_node_candidates", AsyncMock(return_value=[node, node]))
    monkeypatch.setattr(runtime_module.DockerSandbox, "create_record", AsyncMock(side_effect=ValueError("offline registration error")))
    if isinstance(cleanup_failure, BaseException):
        sandbox.destroy.side_effect = cleanup_failure
    else:
        sandbox.destroy.return_value = cleanup_failure
    with pytest.raises(ExceptionGroup) as caught:
        await runtime.allocate(session)
    errors = leaves(caught.value)
    assert any(isinstance(error, ValueError) for error in errors)
    assert any(isinstance(error, (RuntimeError, OSError)) for error in errors)
    runtime_module.DockerSandbox.create.assert_awaited_once()
    sandbox.destroy.assert_awaited_once()
    runtime_module.mark_execution_node_unhealthy.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_failure_is_retired_before_safe_node_retry(allocation, monkeypatch):
    runtime, node, first, session = allocation
    second = SimpleNamespace(id="second-sandbox", ip="127.0.0.1", destroy=AsyncMock(return_value=True))
    monkeypatch.setattr(runtime_module, "_list_execution_node_candidates", AsyncMock(return_value=[node, node]))

    async def create():
        if runtime_module.DockerSandbox.create.await_count == 1:
            return first
        first.destroy.assert_awaited_once()
        return second

    monkeypatch.setattr(runtime_module.DockerSandbox, "create", AsyncMock(side_effect=create))
    monkeypatch.setattr(runtime_module.DockerSandbox, "create_record", AsyncMock(side_effect=[ValueError("offline registration error"), None]))
    result = await runtime.allocate(session)
    assert result.id == second.id
    first.destroy.assert_awaited_once()
    second.destroy.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_and_destroy_failure_preserve_both_errors(allocation):
    runtime, _node, sandbox, session = allocation

    async def create(*args):
        asyncio.get_running_loop().call_soon(task.cancel)
        return sandbox

    runtime._allocate_on_node = create
    sandbox.destroy.return_value = False
    task = asyncio.create_task(runtime.allocate(session))
    with pytest.raises(BaseExceptionGroup) as caught:
        await asyncio.wait_for(task, 1)
    errors = leaves(caught.value)
    assert any(isinstance(error, asyncio.CancelledError) for error in errors)
    assert any(isinstance(error, RuntimeError) for error in errors)
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_class_runtime_owns_late_create_result_until_cancelled_caller_settles():
    sandbox = SimpleNamespace(id="class-sandbox", destroy=AsyncMock(return_value=True))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def create():
        entered.set()
        await finish.wait()
        return sandbox

    runtime = runtime_module.SandboxClassRuntime(SimpleNamespace(create=create))
    task = asyncio.create_task(runtime.allocate())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["record", "allocation"])
async def test_class_runtime_retires_created_docker_handle_on_registration_failure(allocation, monkeypatch, stage):
    _runtime, _node, _sandbox, session = allocation
    sandbox = object.__new__(runtime_module.DockerSandbox)
    sandbox._container_name = "class-docker-sandbox"
    sandbox.ip = "127.0.0.1"
    sandbox.destroy = AsyncMock(return_value=True)
    monkeypatch.setattr(runtime_module.DockerSandbox, "create", AsyncMock(return_value=sandbox))
    if stage == "record":
        monkeypatch.setattr(runtime_module.DockerSandbox, "create_record", AsyncMock(side_effect=ValueError("offline registration error")))
    else:
        monkeypatch.setattr(runtime_module, "_upsert_allocation", AsyncMock(side_effect=ValueError("offline registration error")))
    runtime = runtime_module.SandboxClassRuntime(runtime_module.DockerSandbox)
    with pytest.raises(ValueError, match="offline registration error"):
        await runtime.allocate(session)
    sandbox.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_docker_create_thread_boundary_is_not_abandoned(allocation, monkeypatch):
    runtime, _node, sandbox, session = allocation
    entered, finish = threading.Event(), threading.Event()

    def create(*args):
        entered.set()
        assert finish.wait(3)
        return sandbox

    monkeypatch.setattr(docker_module, "get_settings", lambda: SimpleNamespace(sandbox_address=None))
    monkeypatch.setattr(docker_module, "_DOCKER_CREATE_SEMAPHORE", asyncio.Semaphore(1))
    monkeypatch.setattr(docker_module.DockerSandbox, "_create_task", create)
    # The fixture replaces create; restore its real semaphore + to_thread path.
    monkeypatch.setattr(docker_module.DockerSandbox, "create", ORIGINAL_DOCKER_CREATE)
    task = asyncio.create_task(runtime.allocate(session))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        sandbox.destroy.assert_awaited_once()
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


ORIGINAL_DOCKER_CREATE = docker_module.DockerSandbox.create
