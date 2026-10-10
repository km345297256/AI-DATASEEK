"""Join owned cleanup before releasing its dependencies; never retry work."""
import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any


def raise_cleanup_errors(errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Resource cleanup failed", errors)


async def cleanup_steps(*steps: Callable[[], Awaitable[Any]]) -> None:
    errors: list[BaseException] = []
    for step in steps:
        try:
            await step()
        except BaseException as error:
            errors.append(error)
    raise_cleanup_errors(errors)


async def join_cleanup(task: asyncio.Task):
    """A caller cancellation cannot abandon an already-owned cleanup task.

    Keep cancellation observable after cleanup, including repeated cancellation.
    There is no deadline and no rerun of a failed cleanup operation.
    """
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                break
            cancelled = error
        except BaseException:
            break
    if cancelled is not None:
        try:
            task.result()
        except BaseException as error:
            raise cancelled from error
        raise cancelled
    return task.result()


async def cancel_and_join(tasks: Iterable[asyncio.Task]) -> None:
    tasks = list(tasks)
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    raise_cleanup_errors([result for result in results
                          if isinstance(result, BaseException)
                          and not isinstance(result, asyncio.CancelledError)])
