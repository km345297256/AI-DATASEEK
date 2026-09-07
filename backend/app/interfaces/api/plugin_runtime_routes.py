from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.config import Settings, get_settings
from app.domain.external.plugin_runtime import (
    PluginCatalogSnapshot,
    PluginRuntime,
    PluginRuntimeError,
)
from app.interfaces.dependencies import get_plugin_runtime
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.plugin_runtime import (
    PluginRuntimeSnapshotResponse,
    PluginRuntimeStatus,
)


router = APIRouter(prefix="/plugins/runtime", tags=["plugins"])
_RELOAD_ACTION = "plugin-runtime-reload"
_RELOAD_ACTION_HEADER = "X-AI-DataSeek-Action"
_RELOAD_RESPONSE_LOCK = asyncio.Lock()


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return (
        scheme,
        parsed.hostname.lower(),
        port if port is not None else (443 if scheme == "https" else 80),
    )


def _request_host(value: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlsplit(f"//{value.strip()}")
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return None
    return parsed.hostname.lower(), port


def require_same_origin_reload(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject cross-site and form-compatible reload requests."""

    if request.headers.get(_RELOAD_ACTION_HEADER) != _RELOAD_ACTION:
        raise HTTPException(status_code=403, detail="Plugin runtime reload request rejected")

    trusted = _origin(settings.server_host or "")
    if trusted is None:
        raise HTTPException(
            status_code=503,
            detail="Plugin runtime reload origin is not configured",
        )

    host = _request_host(request.headers.get("host", ""))
    if (
        host is None
        or host[0] != trusted[1]
        or (host[1] is not None and host[1] != trusted[2])
    ):
        raise HTTPException(status_code=403, detail="Plugin runtime reload request rejected")

    origin_header = request.headers.get("origin")
    referer_header = request.headers.get("referer")
    if not origin_header and not referer_header:
        raise HTTPException(status_code=403, detail="Plugin runtime reload request rejected")
    if origin_header and _origin(origin_header) != trusted:
        raise HTTPException(status_code=403, detail="Plugin runtime reload request rejected")
    if referer_header and _origin(referer_header) != trusted:
        raise HTTPException(status_code=403, detail="Plugin runtime reload request rejected")


def _response(
    snapshot: PluginCatalogSnapshot,
    runtime: PluginRuntime | None,
    *,
    status: PluginRuntimeStatus | None = None,
    last_error: object | None = None,
) -> PluginRuntimeSnapshotResponse:
    healthy = bool(runtime and runtime.healthy)
    return PluginRuntimeSnapshotResponse.from_snapshot(
        snapshot,
        healthy=healthy,
        status=status,
        last_error=last_error,
    )


@router.get("", response_model=APIResponse[PluginRuntimeSnapshotResponse])
async def get_runtime_snapshot(
    runtime: PluginRuntime | None = Depends(get_plugin_runtime),
) -> APIResponse[PluginRuntimeSnapshotResponse]:
    if runtime is None:
        return APIResponse.success(
            _response(PluginCatalogSnapshot.unavailable(), None, status="unavailable")
        )

    try:
        snapshot = await runtime.snapshot()
    except PluginRuntimeError as error:
        # Snapshot generations are immutable. If the host is temporarily
        # unavailable, expose the last complete generation rather than a
        # partially loaded catalog.
        return APIResponse.success(
            _response(
                runtime.current_snapshot,
                runtime,
                status="error",
                last_error=runtime.last_error or error,
            )
        )

    last_error = runtime.last_error
    return APIResponse.success(
        _response(
            snapshot,
            runtime,
            status="error" if last_error else None,
            last_error=last_error,
        )
    )


@router.post(
    "/reload",
    response_model=APIResponse[PluginRuntimeSnapshotResponse],
    dependencies=[Depends(require_same_origin_reload)],
)
async def reload_runtime_snapshot(
    runtime: PluginRuntime | None = Depends(get_plugin_runtime),
) -> APIResponse[PluginRuntimeSnapshotResponse]:
    if runtime is None:
        raise HTTPException(status_code=503, detail="Cordis plugin runtime is unavailable")

    # Keep snapshot, status and retained error from one reload generation in
    # the same response. NodePluginRuntime already serializes the state swap;
    # this outer lock also prevents a second HTTP reload from changing
    # ``last_error`` between the swap and response construction.
    async with _RELOAD_RESPONSE_LOCK:
        previous_snapshot = runtime.current_snapshot
        try:
            # The runtime validates a complete candidate generation before this
            # await returns, then swaps the active snapshot atomically.
            snapshot = await runtime.reload()
        except PluginRuntimeError as error:
            return APIResponse.success(
                _response(
                    previous_snapshot,
                    runtime,
                    status="error",
                    last_error=runtime.last_error or error,
                )
            )

        last_error = runtime.last_error
        return APIResponse.success(
            _response(
                snapshot,
                runtime,
                status="error" if last_error else None,
                last_error=last_error,
            )
        )
