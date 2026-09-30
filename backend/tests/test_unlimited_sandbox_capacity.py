"""Capacity is a cancellable queue, not an analysis execution deadline."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import Settings
from app.domain.models.session import Session, SessionStatus
from app.domain.services.agent_domain_service import AgentDomainService
from app.infrastructure.external.sandbox import runtime as runtime_module
from app.infrastructure.external.sandbox.sandbox_pool import SandboxPool


def test_stale_capacity_deadline_setting_cannot_reactivate_queue_expiry(monkeypatch):
    monkeypatch.setenv("SANDBOX_CAPACITY_WAIT_SECONDS", "0")
    settings = Settings(_env_file=None)
    assert "sandbox_capacity_wait_seconds" not in Settings.model_fields
    assert "sandbox_capacity_wait_seconds" not in settings.model_dump()


@pytest.mark.asyncio
async def test_full_node_wait_does_not_block_other_eligible_nodes(monkeypatch):
    entered = asyncio.Event()
    node = SimpleNamespace(node_id="remote")
    monkeypatch.setattr(runtime_module, "_SANDBOX_ADMISSION_LOCK", asyncio.Lock())

    async def candidates(required_node_ids=None):
        assert runtime_module._SANDBOX_ADMISSION_LOCK.locked()
        if required_node_ids == {"full-local"}:
            entered.set()
            raise runtime_module.SandboxCapacityError("full")
        return [node]

    monkeypatch.setattr(runtime_module, "_list_execution_node_candidates", candidates)
    sandbox = SimpleNamespace(resume=AsyncMock(return_value=True))
    waiting = asyncio.create_task(runtime_module.NodeBoundSandbox(
        sandbox, SimpleNamespace(node_id="full-local"),
    ).resume())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        runtime = runtime_module.LocalDockerRuntime()
        runtime._allocate_on_node = AsyncMock(return_value="allocated-on-remote")
        assert await asyncio.wait_for(runtime.allocate(), timeout=1) == "allocated-on-remote"
        assert not waiting.done()
        sandbox.resume.assert_not_awaited()
    finally:
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert not runtime_module._SANDBOX_ADMISSION_LOCK.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_user_stop_cancels_capacity_wait_before_analysis_task_creation(monkeypatch, resume):
    entered = asyncio.Event()
    monkeypatch.setattr(runtime_module, "_SANDBOX_ADMISSION_LOCK", asyncio.Lock())

    async def full_nodes(*_args):
        entered.set()
        raise runtime_module.SandboxCapacityError("full")

    monkeypatch.setattr(runtime_module, "_list_execution_node_candidates", full_nodes)
    monkeypatch.setattr("app.domain.services.agent_domain_service.get_sandbox_pool", lambda: None)
    runtime = runtime_module.LocalDockerRuntime()
    runtime._allocate_on_node = AsyncMock(side_effect=AssertionError("cancelled work must not allocate"))
    paused = SimpleNamespace(id="paused", is_paused=AsyncMock(return_value=True),
                             resume=AsyncMock(return_value=True), destroy=AsyncMock())
    runtime.restore = AsyncMock(return_value=runtime_module.NodeBoundSandbox(
        paused, SimpleNamespace(node_id="local-default"),
    ))
    session = Session(id="queued-session", user_id="user", agent_id="agent",
                      sandbox_id="paused" if resume else None)
    repository = SimpleNamespace(find_by_id=AsyncMock(return_value=session), update_status=AsyncMock())
    service = AgentDomainService(
        agent_repository=object(), session_repository=repository, sandbox_cls=object(),
        task_cls=object(), file_storage=object(), mcp_repository=object(), sandbox_runtime=runtime,
    )
    service._ensure_plugin_runtime_ready = AsyncMock()
    service._get_task = AsyncMock(return_value=None)
    service._input_delivery = SimpleNamespace(cancel_session=AsyncMock())
    bootstrap = service._track_chat_bootstrap(asyncio.create_task(service._create_task(session)), session.id)
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await service.stop_session(session.id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(bootstrap, timeout=1)
    finally:
        if not bootstrap.done():
            bootstrap.cancel()
            await asyncio.gather(bootstrap, return_exceptions=True)
    runtime._allocate_on_node.assert_not_awaited()
    paused.resume.assert_not_awaited()
    paused.destroy.assert_not_awaited()
    repository.update_status.assert_awaited_once_with(session.id, SessionStatus.COMPLETED)
    service._input_delivery.cancel_session.assert_awaited_once_with(session.id)
    assert not runtime_module._SANDBOX_ADMISSION_LOCK.locked()
    assert bootstrap not in service._chat_bootstrap_sessions


@pytest.mark.asyncio
async def test_background_warm_pool_retries_without_blocking_resize_when_full(monkeypatch):
    pool = SandboxPool(pool_size=1)
    pool._replenish_retry_seconds = 0
    pool.schedule_replenish = Mock()
    create = AsyncMock(side_effect=runtime_module.SandboxCapacityError("full"))
    monkeypatch.setattr(pool, "_create_and_warm", create)
    await pool._replenish()
    create.assert_awaited_once_with(wait_for_capacity=False)
    pool.schedule_replenish.assert_called_once()
    await asyncio.wait_for(pool.resize(0), timeout=1)
    assert not pool.enabled
