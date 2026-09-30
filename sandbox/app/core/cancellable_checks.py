"""Cooperative cancellation for read-only, unbounded-duration worker checks."""
from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

from starlette.requests import Request


async def run_cancellable_check(request: Request, function, *args, **kwargs):
    """Stop a worker on disconnect/cancellation, never merely on elapsed time.

    Cancelling an asyncio.to_thread await does not stop its thread. Keep the
    worker task shielded, signal the shared event, and await its cleanup so a
    native validator cannot outlive the cancelled check unnoticed.
    """
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(function, *args, cancelled=cancelled, **kwargs))

    async def observe_disconnect():
        while not await request.is_disconnected():
            await asyncio.sleep(0.1)  # Observation cadence, not a task deadline.
        cancelled.set()

    disconnect = asyncio.create_task(observe_disconnect())
    try:
        done, _ = await asyncio.wait((worker, disconnect), return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            return worker.result()
        # A disconnected caller no longer owns a validation in the background.
        disconnect.result()
        raise asyncio.CancelledError()
    finally:
        cancelled.set()
        disconnect.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect
        # Shielding ensures request cancellation cannot drop native cleanup.
        # The worker checks this event between bounded reads/parser operations.
        with suppress(Exception):
            await asyncio.shield(worker)
