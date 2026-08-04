from fastapi import APIRouter
from . import (
    admin_routes,
    admin_dataset_routes,
    agent_profile_routes,
    config_routes,
    dataset_routes,
    file_routes,
    mcp_routes,
    renderer_routes,
    session_routes,
    skill_routes,
)

def create_api_router() -> APIRouter:
    """Create and configure the main API router"""
    api_router = APIRouter()

    # Include all sub-routers
    api_router.include_router(session_routes.router)
    api_router.include_router(file_routes.router)
    api_router.include_router(config_routes.router)
    api_router.include_router(skill_routes.router)
    api_router.include_router(mcp_routes.router)
    api_router.include_router(agent_profile_routes.router)
    api_router.include_router(renderer_routes.router)
    api_router.include_router(admin_routes.router)
    api_router.include_router(admin_dataset_routes.router)
    api_router.include_router(dataset_routes.router)

    return api_router

# Create the main router instance
router = create_api_router()
