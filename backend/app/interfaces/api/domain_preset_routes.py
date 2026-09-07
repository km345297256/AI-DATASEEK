from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.domain.external.plugin_runtime import PluginRuntime
from app.domain.models.tool_runtime import ToolRuntimeConfig
from app.domain.services.domain_presets import describe_domain_presets
from app.domain.services.tool_runtime_config import default_tool_runtime
from app.interfaces.dependencies import get_current_user, get_plugin_runtime
from app.interfaces.schemas.base import APIResponse


router = APIRouter(prefix="/plugins/presets", tags=["plugins"])


class DomainPresetView(BaseModel):
    id: str
    name: str
    description: str
    plugin_ids: list[str]
    initial_tools: list[str]
    available_tool_count: int
    initial_tool_count: int


class DomainPresetList(BaseModel):
    presets: list[DomainPresetView]
    defaults: ToolRuntimeConfig
    catalog_revision: str | None


@router.get("", response_model=APIResponse[DomainPresetList])
async def get_domain_presets(
    user=Depends(get_current_user),
    runtime: PluginRuntime | None = Depends(get_plugin_runtime),
):
    snapshot = runtime.current_snapshot if runtime else None
    return APIResponse.success(DomainPresetList(
        presets=describe_domain_presets(snapshot), defaults=default_tool_runtime(),
        catalog_revision=(snapshot.revision or None) if snapshot else None,
    ))
