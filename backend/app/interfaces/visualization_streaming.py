"""Byte-preview ownership shared by public and provider-private HTTP routes."""

import asyncio
import logging

from starlette.responses import StreamingResponse

from app.application.services.unified_visualization import VisualizationBytes


logger = logging.getLogger(__name__)


def _close_visualization_bytes(result: VisualizationBytes) -> None:
    try:
        result.close()
    except Exception as error:
        logger.warning("Visualization result cleanup failed error_type=%s", type(error).__name__)


def _close_unclaimed_result(work: asyncio.Task) -> None:
    try:
        result = work.result()
    except (asyncio.CancelledError, Exception):
        # Consume worker failures without replacing the request's exception.
        return
    if isinstance(result, VisualizationBytes):
        _close_visualization_bytes(result)


async def reclaim_unclaimed_visualization(work: asyncio.Task) -> None:
    """Close a result not handed to a response, even after repeated cancellation."""
    if work.done():
        _close_unclaimed_result(work)
        return
    # Register before cancelling: a worker may return bytes during cleanup.
    work.add_done_callback(_close_unclaimed_result)
    work.cancel()
    try:
        # A second request cancellation must not interrupt worker cleanup.
        await asyncio.shield(work)
    except (asyncio.CancelledError, Exception):
        pass


class VisualizationStreamingResponse(StreamingResponse):
    def __init__(self, preview: VisualizationBytes, **kwargs):
        super().__init__(preview.chunks(), **kwargs)
        self._preview = preview

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Failed header/body sends can bypass the iterator/background task.
            _close_visualization_bytes(self._preview)
