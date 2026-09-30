"""Checks run until completion or explicit cancellation, including disconnect."""
import asyncio
import threading

import pytest

from app.core.cancellable_checks import run_cancellable_check


class Request:
    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


@pytest.mark.asyncio
async def test_slow_check_can_span_many_observation_intervals_without_cancellation():
    started, release = threading.Event(), threading.Event()
    def check(*, cancelled):
        started.set()
        assert release.wait(5), "test must release its worker"
        assert not cancelled.is_set()
        return {"valid": True}
    task = asyncio.create_task(run_cancellable_check(Request(), check))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0.35)
        assert not task.done()
        release.set()
        assert await task == {"valid": True}
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["request_cancel", "disconnect"])
async def test_cancellation_is_propagated_and_worker_cleanup_finishes(trigger):
    started, cleaned = threading.Event(), threading.Event()
    request = Request()
    def check(*, cancelled):
        started.set()
        try:
            assert cancelled.wait(5), "caller must notify the worker"
            return {"valid": False, "reason": "validation_cancelled"}
        finally:
            cleaned.set()
    task = asyncio.create_task(run_cancellable_check(request, check))
    assert await asyncio.to_thread(started.wait, 5)
    if trigger == "request_cancel":
        task.cancel()
    else:
        request.disconnected = True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_real_check_error_is_not_suppressed_or_relabelled_timeout():
    def broken(*, cancelled):
        raise ValueError("invalid_test_content")
    with pytest.raises(ValueError, match="invalid_test_content"):
        await run_cancellable_check(Request(), broken)
