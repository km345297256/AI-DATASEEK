from fastapi import APIRouter
from . import (
    analysis_job_routes,
    credential_routes,
    tool_approval_routes,
    model_trace_routes,
    admin_routes,
    agent_profile_routes,
    config_routes,
    dataset_routes,
    file_routes,
    mcp_routes,
    plugin_runtime_routes,
    domain_preset_routes,
    renderer_routes,
    session_routes,
    skill_routes,
)

def create_api_router() -> APIRouter:
    """Create and configure the main API router"""
    api_router = APIRouter()
    api_router.include_router(analysis_job_routes.router)
    api_router.include_router(credential_routes.router)
    api_router.include_router(tool_approval_routes.router)
    api_router.include_router(model_trace_routes.router)

    # Include all sub-routers
    api_router.include_router(session_routes.router)
    api_router.include_router(file_routes.router)
    api_router.include_router(config_routes.router)
    api_router.include_router(skill_routes.router)
    api_router.include_router(mcp_routes.router)
    api_router.include_router(plugin_runtime_routes.router)
    api_router.include_router(domain_preset_routes.router)
    api_router.include_router(agent_profile_routes.router)
    api_router.include_router(renderer_routes.router)
    api_router.include_router(admin_routes.router)
    api_router.include_router(dataset_routes.router)

    return api_router

# Create the main router instance
router = create_api_router()
