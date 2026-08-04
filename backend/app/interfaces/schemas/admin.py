"""Schemas for the deliberately small AI-DataSeek administration surface."""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.domain.models.mcp_config import MCPRiskLevel, MCPScope, MCPTransport
from app.domain.models.execution_node import (
    ExecutionNodeAuthType,
    ExecutionNodeCapacity,
    ExecutionNodeHealth,
    ExecutionNodeStatus,
    ExecutionNodeType,
)
from app.domain.models.session import SessionStatus
from app.domain.models.skill import SkillScope
from app.domain.models.user import RegistrationStatus, UserRole


class AdminUserResponse(BaseModel):
    id: str
    fullname: str
    email: str
    role: UserRole
    is_active: bool
    registration_status: RegistrationStatus = RegistrationStatus.APPROVED
    registration_reviewed_by: Optional[str] = None
    registration_reviewed_at: Optional[datetime] = None
    registration_review_note: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None
    workspace_count: int = 0
    member_count: int = 0
    token_balance: Optional[int] = 0
    token_daily_refill: Optional[int] = 0
    token_daily_refill_override: Optional[int] = None
    token_last_refill_date: Optional[date] = None


class AdminUserListResponse(BaseModel):
    users: List[AdminUserResponse] = Field(default_factory=list)
    total: int = 0


class AdminUserUpdateRequest(BaseModel):
    role: Optional[UserRole] = None
    token_balance: Optional[int] = Field(default=None, ge=0)
    token_daily_refill_override: Optional[int] = Field(default=None, ge=0)


class AdminRegistrationDecisionRequest(BaseModel):
    status: RegistrationStatus
    decision_note: Optional[str] = None


class RoleTokenQuotaResponse(BaseModel):
    role: UserRole
    initial_tokens: Optional[int] = 0
    daily_refill_tokens: Optional[int] = 0
    created_at: datetime
    updated_at: datetime


class RoleTokenQuotaListResponse(BaseModel):
    quotas: List[RoleTokenQuotaResponse] = Field(default_factory=list)


class RoleTokenQuotaUpdateRequest(BaseModel):
    initial_tokens: Optional[int] = Field(..., ge=0)
    daily_refill_tokens: Optional[int] = Field(..., ge=0)


class AdminTaskResponse(BaseModel):
    session_id: str
    user_id: str
    user_fullname: Optional[str] = None
    agent_id: str
    task_id: Optional[str] = None
    sandbox_id: Optional[str] = None
    title: Optional[str] = None
    latest_message: Optional[str] = None
    latest_message_at: Optional[datetime] = None
    status: SessionStatus
    unread_message_count: int = 0
    is_shared: bool = False
    created_at: datetime
    updated_at: datetime


class AdminTaskListResponse(BaseModel):
    tasks: List[AdminTaskResponse] = Field(default_factory=list)
    total: int = 0


class AdminSkillResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    triggers: List[str] = Field(default_factory=list)
    scope: SkillScope
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    owner_fullname: Optional[str] = None
    workspace_id: Optional[str] = None
    path: str
    created_from_session_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AdminSkillListResponse(BaseModel):
    skills: List[AdminSkillResponse] = Field(default_factory=list)
    total: int = 0


class AdminSkillUpdateRequest(BaseModel):
    scope: Optional[SkillScope] = None
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None


class AdminMCPServerResponse(BaseModel):
    name: str
    transport: MCPTransport
    enabled: bool
    description: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    scope: MCPScope = MCPScope.GLOBAL
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    owner_fullname: Optional[str] = None
    workspace_id: Optional[str] = None
    risk_level: MCPRiskLevel = MCPRiskLevel.STANDARD


class AdminMCPServerListResponse(BaseModel):
    servers: List[AdminMCPServerResponse] = Field(default_factory=list)
    total: int = 0


class AdminMCPServerUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    scope: Optional[MCPScope] = None
    risk_level: Optional[MCPRiskLevel] = None
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None


class TokenUsageByModelResponse(BaseModel):
    model_name: str
    record_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class TokenUsageDimensionResponse(BaseModel):
    key: str
    label: Optional[str] = None
    record_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class TokenUsageOverviewResponse(BaseModel):
    record_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    by_model: List[TokenUsageByModelResponse] = Field(default_factory=list)


class AuthUsageOverviewResponse(BaseModel):
    users_total: int = 0
    users_active: int = 0
    api_keys_total: int = 0
    api_keys_active: int = 0
    sessions_total: int = 0


class ServerUsageOverviewResponse(BaseModel):
    cpu: Dict[str, Any] = Field(default_factory=dict)
    memory: Dict[str, Any] = Field(default_factory=dict)
    disk: Dict[str, Any] = Field(default_factory=dict)
    docker: Dict[str, Any] = Field(default_factory=dict)
    file_storage: Dict[str, Any] = Field(default_factory=dict)


class ResourceUsageOverviewResponse(BaseModel):
    token_usage: TokenUsageOverviewResponse
    token_usage_by_user: List[TokenUsageDimensionResponse] = Field(default_factory=list)
    token_usage_by_workspace: List[TokenUsageDimensionResponse] = Field(default_factory=list)
    auth_usage: AuthUsageOverviewResponse
    server_usage: ServerUsageOverviewResponse
    sandbox_usage: List[Dict[str, Any]] = Field(default_factory=list)
    execution_nodes_usage: List[Dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime


class ExecutionNodeResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    type: ExecutionNodeType
    status: ExecutionNodeStatus
    enabled: bool
    base_url: Optional[str] = None
    auth_type: ExecutionNodeAuthType
    credential_ref: Optional[str] = None
    runtime_config: Dict[str, Any] = Field(default_factory=dict)
    capacity: ExecutionNodeCapacity
    labels: Dict[str, str] = Field(default_factory=dict)
    taints: Dict[str, str] = Field(default_factory=dict)
    health: ExecutionNodeHealth
    last_heartbeat_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ExecutionNodeListResponse(BaseModel):
    nodes: List[ExecutionNodeResponse] = Field(default_factory=list)
    total: int = 0
