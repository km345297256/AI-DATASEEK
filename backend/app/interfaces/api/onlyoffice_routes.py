"""Provider-private capabilities, not another plugin protocol or file API."""
import asyncio
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.background import BackgroundTask

from app.application.services import onlyoffice_viewer as service
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.visualization_catalog import VisualizationDisabledError, VisualizationNotFoundError
from app.interfaces.dependencies import get_current_user, get_file_service, get_visualization_catalog
from app.interfaces.schemas.base import APIResponse
from app.interfaces.visualization_streaming import VisualizationStreamingResponse, reclaim_unclaimed_visualization

router = APIRouter(prefix="/office-viewer", tags=["office-viewer"])


async def _safe(operation):
    try:
        return await operation
    except (FileNotFoundError, PermissionError, VisualizationDisabledError, VisualizationNotFoundError):
        raise HTTPException(404, "Office resource unavailable") from None
    except PreviewVersionChanged:
        raise HTTPException(409, "Office document changed") from None
    except ScientificPreviewRejected:
        raise HTTPException(422, "Office resource exceeds its preview capability") from None
    except Exception:
        raise HTTPException(503, "Office viewer unavailable") from None


@router.get("/private/frame/{token}", response_class=HTMLResponse, include_in_schema=False)
async def office_frame(token: str, files=Depends(get_file_service), catalog=Depends(get_visualization_catalog),
                       gateway: str | None = Header(default=None, alias="X-Office-Gateway")):
    async def operation():
        service.require_gateway(gateway)
        lease, _, info = await service.authorize_lease(token, files, catalog)
        return HTMLResponse(service.frame_html(service.view_configuration(token, lease, info), lease.expires_at, token),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
    return await _safe(operation())


@router.get("/private/status/{token}", include_in_schema=False)
async def office_status(token: str, files=Depends(get_file_service), catalog=Depends(get_visualization_catalog),
                        gateway: str | None = Header(default=None, alias="X-Office-Gateway")):
    async def operation():
        service.require_gateway(gateway)
        await service.authorize_lease(token, files, catalog)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return await _safe(operation())


@router.api_route("/private/files/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def office_source(token: str, request: Request, files=Depends(get_file_service), catalog=Depends(get_visualization_catalog),
                        gateway: str | None = Header(default=None, alias="X-Office-Gateway")):
    async def operation():
        from app.application.services.unified_visualization import _bytes
        service.require_gateway(gateway)
        lease, plugin, info = await service.authorize_lease(token, files, catalog)
        headers = {"Cache-Control": "no-store", "Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff",
            "Content-Length": str(info.size)}
        if request.method == "HEAD":
            return Response(headers=headers, media_type="application/octet-stream")
        await service.get_office_lease_store().claim_source(token)
        work = asyncio.create_task(_bytes(files, catalog, lease.file_id, lease.user_id, plugin, lease.revision, info, lease.version))
        streaming = False
        try:
            while not work.done():
                await asyncio.wait({work}, timeout=.25)
                if not work.done() and await request.is_disconnected():
                    raise asyncio.CancelledError()
            result = await work
            await service.authorize_lease(token, files, catalog)
            response = VisualizationStreamingResponse(result, media_type="application/octet-stream", headers=headers,
                background=BackgroundTask(result.aclose))
            streaming = True
            return response
        finally:
            if not streaming:
                await reclaim_unclaimed_visualization(work)
    return await _safe(operation())


@router.post("/leases/{token}/revoke", response_model=APIResponse[None])
async def revoke_lease(token: str, user=Depends(get_current_user),
                       action: str | None = Header(default=None, alias="X-Visualization-Action")):
    if action != "revoke":
        raise HTTPException(400, "Explicit lease revoke required")
    async def operation():
        store = service.get_office_lease_store()
        lease = await store.get(token)
        if lease.user_id != user.id:
            raise FileNotFoundError()
        # Revocation stays possible after plugin stop or source removal.
        await store.delete(token)
        return APIResponse.success()
    return await _safe(operation())
