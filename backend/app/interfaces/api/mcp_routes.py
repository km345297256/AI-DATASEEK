from fastapi import APIRouter, Depends, HTTPException

from app.domain.models.audit import AuditRiskLevel
from app.domain.models.mcp_config import (
    MCPConfig,
    MCPRiskLevel,
    MCPScope,
    MCPServerConfig,
    MCPConfigurationConflict,
    can_access_mcp,
    can_manage_mcp,
    is_mcp_owned_by,
    mcp_catalog_source,
    mcp_owner_id,
)
from app.domain.models.user import User
from app.domain.services.approval_service import ApprovalService, get_approval_service
from app.domain.services.audit_service import AuditService, get_audit_service
from app.domain.services.tools.mcp import MCPToolkit
from app.infrastructure.repositories.mongo_mcp_repository import MongoMCPRepository
from app.interfaces.dependencies import get_current_user, get_user_repository
from app.domain.repositories.user_repository import UserRepository
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.mcp import (
    MCPServerListResponse,
    MCPServerRequest,
    MCPServerResponse,
    MCPServerUpsertResponse,
    MCPToolListResponse,
    MCPToolListRequest,
    MCPToolResponse,
)

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _repository() -> MongoMCPRepository:
    return MongoMCPRepository()


def _is_owned(config: MCPServerConfig, user_id: str) -> bool:
    return is_mcp_owned_by(config, user_id)


def _is_installed(name: str, config: MCPServerConfig, user: User) -> bool:
    return _is_owned(config, user.id) or name in user.installed_mcp_names


def _server_response(name: str, config: MCPServerConfig, user: User | None = None) -> MCPServerResponse:
    can_view_secrets = bool(user and _is_owned(config, user.id))
    return MCPServerResponse(
        name=name,
        transport=config.transport,
        enabled=config.enabled,
        description=config.description,
        # URLs (userinfo, query, path), arguments and commands can all contain
        # credentials. Project private connection fields as a unit; regex
        # redaction of a few known token names cannot make them public-safe.
        command=config.command if can_view_secrets else None,
        args=config.args if can_view_secrets else None,
        url=config.url if can_view_secrets else None,
        headers=config.headers if can_view_secrets else None,
        env=config.env if can_view_secrets else None,
        scope=getattr(config, "scope", MCPScope.GLOBAL),
        user_id=getattr(config, "user_id", None),
        owner_user_id=getattr(config, "owner_user_id", None),
        workspace_id=getattr(config, "workspace_id", None),
        risk_level=getattr(config, "risk_level", MCPRiskLevel.STANDARD),
        installed=_is_installed(name, config, user) if user else False,
        source=mcp_catalog_source(config, user.id) if user else "official",
    )


async def _stored_user(current_user: User, user_repository: UserRepository) -> User:
    user = await user_repository.get_user_by_id(current_user.id)
    if user:
        return user
    return await user_repository.create_user(current_user)


def _is_accessible(config: MCPServerConfig, user: User) -> bool:
    return can_access_mcp(config, user.id, is_admin=user.role == "admin")


def _can_manage(config: MCPServerConfig, user: User) -> bool:
    return can_manage_mcp(config, user.id, is_admin=user.role == "admin")


@router.get("/servers", response_model=APIResponse[MCPServerListResponse])
async def list_mcp_servers(
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[MCPServerListResponse]:
    config = await _repository().get_mcp_config()
    user = await _stored_user(current_user, user_repository)
    servers = [
        _server_response(name, server_config, user)
        for name, server_config in sorted(config.mcpServers.items())
        if _is_accessible(server_config, current_user) and _is_installed(name, server_config, user)
    ]
    return APIResponse.success(MCPServerListResponse(servers=servers))


@router.get("/servers/catalog", response_model=APIResponse[MCPServerListResponse])
async def list_mcp_catalog(
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[MCPServerListResponse]:
    config = await _repository().get_mcp_config()
    user = await _stored_user(current_user, user_repository)
    servers = [
        _server_response(name, server_config, user)
        for name, server_config in sorted(config.mcpServers.items())
        if _is_accessible(server_config, current_user)
    ]
    return APIResponse.success(MCPServerListResponse(servers=servers))


@router.post("/servers/{name}/install", response_model=APIResponse[MCPServerResponse])
async def install_mcp_server(
    name: str,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[MCPServerResponse]:
    config = (await _repository().get_mcp_config()).mcpServers.get(name)
    if not config or not _is_accessible(config, current_user):
        raise HTTPException(status_code=404, detail="MCP server not found")
    user = await _stored_user(current_user, user_repository)
    if name not in user.installed_mcp_names:
        user.installed_mcp_names.append(name)
        user = await user_repository.update_user(user)
    return APIResponse.success(_server_response(name, config, user))


@router.delete("/servers/{name}/install", response_model=APIResponse[MCPServerResponse])
async def uninstall_mcp_server(
    name: str,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[MCPServerResponse]:
    config = (await _repository().get_mcp_config()).mcpServers.get(name)
    if not config or not _is_accessible(config, current_user):
        raise HTTPException(status_code=404, detail="MCP server not found")
    if _is_owned(config, current_user.id):
        raise HTTPException(status_code=400, detail="Owned MCP servers are always in your library")
    user = await _stored_user(current_user, user_repository)
    user.installed_mcp_names = [item for item in user.installed_mcp_names if item != name]
    user = await user_repository.update_user(user)
    return APIResponse.success(_server_response(name, config, user))


@router.put("/servers/{name}", response_model=APIResponse[MCPServerUpsertResponse])
async def upsert_mcp_server(
    name: str,
    request: MCPServerRequest,
    current_user: User = Depends(get_current_user),
    audit_service: AuditService = Depends(get_audit_service),
    approval_service: ApprovalService = Depends(get_approval_service),
) -> APIResponse[MCPServerUpsertResponse]:
    if request.name != name:
        raise HTTPException(status_code=400, detail="Server name in path and body must match")
    if request.is_global and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create global MCP servers")

    repository = _repository()
    existing = (await repository.get_mcp_config()).mcpServers.get(name)
    if existing and not _can_manage(existing, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to modify this MCP server")

    # Editing the connection must not silently change its owner, scope or risk.
    scope = (MCPScope.GLOBAL if request.is_global else MCPScope.USER)
    if existing and "is_global" not in request.model_fields_set:
        scope = existing.scope
    risk_level = request.risk_level
    if existing and "risk_level" not in request.model_fields_set:
        risk_level = existing.risk_level
    requires_approval = (
        current_user.role != "admin"
        and risk_level in {MCPRiskLevel.SENSITIVE, MCPRiskLevel.RESTRICTED}
    )
    server_config = MCPServerConfig(
        transport=request.transport,
        enabled=False if requires_approval else request.enabled,
        description=request.description,
        command=request.command,
        args=request.args,
        url=request.url,
        headers=request.headers,
        env=request.env,
        scope=scope,
        owner_user_id=mcp_owner_id(existing) if existing else current_user.id,
        user_id=None if scope == MCPScope.GLOBAL else (existing.user_id if existing else current_user.id),
        workspace_id=existing.workspace_id if existing else None,
        risk_level=risk_level,
    )
    try:
        config = await repository.upsert_server(name, server_config, expected=existing)
    except MCPConfigurationConflict:
        raise HTTPException(status_code=409, detail="MCP configuration changed; reload before saving") from None
    if requires_approval:
        await approval_service.create_request(
            requester_user_id=current_user.id,
            resource_type="mcp_server",
            resource_id=name,
            requested_permissions=["mcp.use"],
            reason=f"Request access to {risk_level} MCP server {name}",
            metadata={
                "server_name": name,
                "risk_level": risk_level,
                "transport": request.transport,
                "auto_disabled_until_approved": True,
            },
        )
    await audit_service.record(
        actor_user_id=current_user.id,
        action="mcp.upsert",
        resource_type="mcp_server",
        resource_id=name,
        risk_level=AuditRiskLevel.HIGH if risk_level in {MCPRiskLevel.SENSITIVE, MCPRiskLevel.RESTRICTED} else AuditRiskLevel.MEDIUM,
        metadata={
            "transport": request.transport,
            "scope": server_config.scope,
            "risk_level": risk_level,
            "is_global": scope == MCPScope.GLOBAL,
            "requires_approval": requires_approval,
        },
    )
    return APIResponse.success(
        MCPServerUpsertResponse(server=_server_response(name, config.mcpServers[name], current_user))
    )


@router.delete("/servers/{name}", response_model=APIResponse[MCPServerListResponse])
async def delete_mcp_server(
    name: str,
    current_user: User = Depends(get_current_user),
    audit_service: AuditService = Depends(get_audit_service),
) -> APIResponse[MCPServerListResponse]:
    repository = _repository()
    existing_config = await repository.get_mcp_config()
    existing = existing_config.mcpServers.get(name)
    if existing and not _can_manage(existing, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to delete this MCP server")
    try:
        config = await repository.delete_server(name, expected=existing)
    except MCPConfigurationConflict:
        raise HTTPException(status_code=409, detail="MCP configuration changed; reload before deleting") from None
    await audit_service.record(
        actor_user_id=current_user.id,
        action="mcp.delete",
        resource_type="mcp_server",
        resource_id=name,
        risk_level=AuditRiskLevel.HIGH,
        metadata={"existed": existing is not None},
    )
    return APIResponse.success(
        MCPServerListResponse(
            servers=[
                _server_response(server_name, server_config, current_user)
                for server_name, server_config in sorted(config.mcpServers.items())
                if _is_accessible(server_config, current_user)
            ]
        )
    )


@router.post("/tools", response_model=APIResponse[MCPToolListResponse])
async def list_mcp_tools(
    request: MCPToolListRequest,
    current_user: User = Depends(get_current_user),
    user_repository: UserRepository = Depends(get_user_repository),
) -> APIResponse[MCPToolListResponse]:
    repository = _repository()
    config = await repository.get_mcp_config()
    user = await _stored_user(current_user, user_repository)
    config = MCPConfig(
        mcpServers={
            name: server_config
            for name, server_config in config.mcpServers.items()
            if _is_accessible(server_config, current_user) and _is_installed(name, server_config, user)
        }
    )
    if request.selected_servers:
        selected_servers = set(request.selected_servers)
        config = MCPConfig(
            mcpServers={
                name: server_config
                for name, server_config in config.mcpServers.items()
                if name in selected_servers and _is_accessible(server_config, current_user)
            }
        )

    toolkit = MCPToolkit()
    try:
        await toolkit.initialized(config)
        tools = []
        for tool in toolkit.get_tools():
            if not isinstance(tool, dict):
                continue
            function = tool.get("function", {})
            tool_name = function.get("name", "")
            route = toolkit.manager.get_tool_route(tool_name) if toolkit.manager else None
            server_name = route[0] if route else ""
            tools.append(
                MCPToolResponse(
                    name=tool_name,
                    server=server_name,
                    description=function.get("description", ""),
                    parameters=function.get("parameters", {}),
                )
            )
        return APIResponse.success(MCPToolListResponse(tools=tools))
    finally:
        await toolkit.cleanup()
