from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.config import ClientConfigResponse

router = APIRouter(prefix="/config", tags=["config"])

# Independent from build/image versions. Retain older protocols while their
# browser API contracts remain supported; ordinary releases need no bump.
SUPPORTED_CLIENT_PROTOCOLS = (1,)


@router.get("/client-contract")
async def get_client_contract() -> JSONResponse:
    """Public compatibility metadata, with no configuration or host identity."""
    return JSONResponse(
        {"schema_version": 1, "supported_client_protocols": list(SUPPORTED_CLIENT_PROTOCOLS)},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/frontend", response_model=APIResponse[ClientConfigResponse])
async def get_frontend_config() -> APIResponse[ClientConfigResponse]:
    """Get frontend runtime config."""
    settings = get_settings()

    return APIResponse.success(
        ClientConfigResponse(
            auth_provider=settings.auth_provider,
            default_agent_name="AI-DataSeek 数据分析代理",
            default_model_provider=settings.model_provider,
            default_model_name=settings.model_name,
        )
    )
