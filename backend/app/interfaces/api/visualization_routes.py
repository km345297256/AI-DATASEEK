from fastapi import APIRouter, Depends, HTTPException, Query

from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationNotFoundError
from app.domain.external.plugin_runtime import PluginRuntimeError
from app.domain.models.user import User
from app.domain.models.visualization import VisualizationCatalog, VisualizationPluginState, VisualizationStateRequest
from app.interfaces.dependencies import get_current_user, get_visualization_catalog
from app.interfaces.schemas.base import APIResponse

router = APIRouter(prefix="/visualizations", tags=["visualizations"])


@router.get("", response_model=APIResponse[VisualizationCatalog])
async def list_visualizations(
    contract_version: int = Query(default=1, ge=1, le=2),
    user: User = Depends(get_current_user),
    catalog: VisualizationCatalogService = Depends(get_visualization_catalog),
) -> APIResponse[VisualizationCatalog]:
    try:
        result = await catalog.list_for_user(user.id)
        return APIResponse.success(result.model_copy(update={
            "plugins": [plugin for plugin in result.plugins if plugin.contract_version <= contract_version],
        }))
    except PluginRuntimeError:
        raise HTTPException(status_code=503, detail="Cordis visualization runtime is unavailable") from None


@router.patch("/{plugin_id}/state", response_model=APIResponse[VisualizationPluginState])
async def set_visualization_state(
    plugin_id: str,
    request: VisualizationStateRequest,
    user: User = Depends(get_current_user),
    catalog: VisualizationCatalogService = Depends(get_visualization_catalog),
) -> APIResponse[VisualizationPluginState]:
    try:
        return APIResponse.success(await catalog.set_state(user.id, plugin_id, request.enabled))
    except VisualizationNotFoundError:
        raise HTTPException(status_code=404, detail="Visualization plugin not found") from None
    except PluginRuntimeError:
        raise HTTPException(status_code=503, detail="Cordis visualization runtime is unavailable") from None
