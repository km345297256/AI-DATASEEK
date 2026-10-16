"""Exercise actual application shutdown with synthetic dependency owners."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main


@pytest.fixture
def resources(monkeypatch):
    calls = []
    services = {}
    for name in ("pool", "node", "agent", "analysis", "approval", "plugin", "redis", "mongo"):
        async def shutdown(name=name):
            calls.append(name)
        services[name] = SimpleNamespace(shutdown=AsyncMock(side_effect=shutdown))
    services["node"].stop = services["node"].shutdown
    for field in ("jupyter_reaper_task", "spill_reaper_task", "analysis_job_monitor_task"):
        monkeypatch.setattr(main, field, None)
    monkeypatch.setattr(main, "execution_node_monitor", services["node"])
    monkeypatch.setattr(main, "get_sandbox_pool", lambda: services["pool"])
    monkeypatch.setattr(main, "set_sandbox_pool", lambda value: None)
    for name, getter in (("agent", "get_agent_service"), ("analysis", "get_analysis_job_service"),
                         ("approval", "get_tool_approval_service"), ("redis", "get_redis"), ("mongo", "get_mongodb")):
        monkeypatch.setattr(main, getter, lambda name=name: services[name])
    return SimpleNamespace(state=SimpleNamespace()), calls, services


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pool", "node", "agent", "analysis", "approval", "plugin", "redis", "mongo"])
async def test_one_failure_does_not_skip_remaining_cleanup(resources, failure):
    app, calls, services = resources

    async def fail():
        calls.append(failure)
        raise RuntimeError(failure)

    services[failure].shutdown.side_effect = fail
    for _ in range(2):
        with pytest.raises(RuntimeError, match=failure):
            await main._shutdown_application(app, services["plugin"])
    assert calls == ["node", "pool", "agent", "analysis", "approval", "plugin", "redis", "mongo"]
    for service in services.values():
        service.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_multiple_failures_remain_observable(resources):
    app, calls, services = resources
    services["analysis"].shutdown.side_effect = ValueError("analysis")
    services["redis"].shutdown.side_effect = RuntimeError("redis")
    with pytest.raises(ExceptionGroup) as caught:
        await main._shutdown_application(app, services["plugin"])
    assert [type(error) for error in caught.value.exceptions] == [ValueError, RuntimeError]
    assert services["mongo"].shutdown.await_count == 1
    assert services["approval"].shutdown.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_resource_does_not_skip_remaining_cleanup(resources):
    app, calls, services = resources
    services["analysis"].shutdown.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await main._shutdown_application(app, services["plugin"])
    services["approval"].shutdown.assert_awaited_once()
    services["mongo"].shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_caller_cancel_waits_for_final_write_before_store_close(resources):
    app, calls, services = resources
    entered, release = asyncio.Event(), asyncio.Event()

    async def final_write():
        calls.append("agent")
        entered.set()
        await release.wait()
        assert services["redis"].shutdown.await_count == 0
        assert services["mongo"].shutdown.await_count == 0
        calls.append("final-write")

    services["agent"].shutdown.side_effect = final_write
    first = asyncio.create_task(main._shutdown_application(app, services["plugin"]))
    second = asyncio.create_task(main._shutdown_application(app, services["plugin"]))
    await entered.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert calls.index("final-write") < calls.index("redis") < calls.index("mongo")
    for service in services.values():
        service.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_producer_settles_before_services_close(resources, monkeypatch):
    app, calls, services = resources
    running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def producer():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            assert not calls
            calls.append("producer-settled")

    producer_task = asyncio.create_task(producer())
    monkeypatch.setattr(main, "analysis_job_monitor_task", producer_task)
    await running.wait()
    closing = asyncio.create_task(main._shutdown_application(app, services["plugin"]))
    await cleaning.wait()
    assert not closing.done() and not calls
    release.set()
    await closing
    assert calls[0] == "producer-settled"
    assert main.analysis_job_monitor_task is None
