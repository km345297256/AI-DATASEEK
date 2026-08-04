from typing import Optional, Dict, List, Any
from pydantic import BaseModel, Field, field_validator
from enum import Enum


class MCPTransport(str, Enum):
    """MCP transport types"""
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"


class MCPScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    WORKSPACE = "workspace"
    PRIVATE = "private"
    SHARED = "shared"


class MCPRiskLevel(str, Enum):
    STANDARD = "standard"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class MCPServerConfig(BaseModel):
    """
    MCP server configuration model
    """
    # For stdio transport
    command: Optional[str] = None
    args: Optional[List[str]] = None
    
    # For HTTP-based transports
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    
    # Common fields
    transport: MCPTransport
    enabled: bool = Field(default=True)
    description: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    scope: MCPScope = MCPScope.GLOBAL
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    risk_level: MCPRiskLevel = MCPRiskLevel.STANDARD
    
    @field_validator("url")
    def validate_url_for_http_transport(cls, v: Optional[str], values) -> Optional[str]:
        """Validate URL is required for HTTP-based transports"""
        if hasattr(values, 'data'):
            transport = values.data.get('transport')
            if transport in [MCPTransport.SSE, MCPTransport.STREAMABLE_HTTP] and not v:
                raise ValueError("URL is required for HTTP-based transports")
        return v
    
    @field_validator("command")
    def validate_command_for_stdio(cls, v: Optional[str], values) -> Optional[str]:
        """Validate command is required for stdio transport"""
        if hasattr(values, 'data'):
            transport = values.data.get('transport')
            if transport == MCPTransport.STDIO and not v:
                raise ValueError("Command is required for stdio transport")
        return v
    
    class Config:
        extra = "allow"


class MCPConfig(BaseModel):
    """
    MCP configuration model containing all server configurations
    """
    mcpServers: Dict[str, MCPServerConfig] = Field(default_factory=dict)
    
    class Config:
        arbitrary_types_allowed = True
        extra = "allow"


def mcp_owner_id(config: MCPServerConfig) -> Optional[str]:
    return config.owner_user_id or config.user_id


def is_mcp_owned_by(config: MCPServerConfig, user_id: str) -> bool:
    return mcp_owner_id(config) == user_id


def can_access_mcp(config: MCPServerConfig, user_id: str, *, is_admin: bool = False) -> bool:
    if is_admin:
        return True
    if config.scope == MCPScope.GLOBAL:
        return True
    return is_mcp_owned_by(config, user_id)


def mcp_catalog_source(config: MCPServerConfig, user_id: str) -> str:
    if is_mcp_owned_by(config, user_id):
        return "personal"
    if mcp_owner_id(config):
        return "community"
    return "official"
