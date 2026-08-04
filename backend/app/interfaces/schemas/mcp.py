from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.domain.models.mcp_config import MCPRiskLevel, MCPScope, MCPTransport


class MCPServerResponse(BaseModel):
    name: str
    transport: MCPTransport
    enabled: bool
    description: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    env: Optional[Dict[str, str]] = None
    scope: MCPScope = MCPScope.GLOBAL
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    risk_level: MCPRiskLevel = MCPRiskLevel.STANDARD
    installed: bool = False
    source: str = "personal"


class MCPServerListResponse(BaseModel):
    servers: List[MCPServerResponse]


class MCPServerRequest(BaseModel):
    name: str
    transport: MCPTransport
    enabled: bool = True
    description: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    env: Optional[Dict[str, str]] = None
    is_global: bool = False
    risk_level: MCPRiskLevel = MCPRiskLevel.STANDARD


class MCPServerUpsertResponse(BaseModel):
    server: MCPServerResponse


class MCPToolResponse(BaseModel):
    name: str
    server: str
    description: str
    parameters: Dict[str, Any]


class MCPToolListResponse(BaseModel):
    tools: List[MCPToolResponse]


class MCPToolListRequest(BaseModel):
    selected_servers: List[str] = []
