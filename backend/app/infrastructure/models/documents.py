from typing import Dict, Optional, List, Type, TypeVar, Generic, get_args, Self
from datetime import date, datetime, timezone, UTC
import uuid
from beanie import Document
from pydantic import BaseModel, Field
from app.domain.models.agent import Agent
from app.domain.models.memory import Memory
from app.domain.models.event import AgentEvent
from app.domain.models.session import Session, SessionStatus
from app.domain.models.file import FileInfo
from app.domain.models.user import User, UserRole, RegistrationStatus
from app.domain.models.api_key import APIKey
from app.domain.models.agent_profile import AgentPlannerConfig, AgentProfile, AgentSubAgentConfig, default_subagents
from app.domain.models.model_configuration import ModelConfiguration, ModelType
from app.domain.models.sandbox_record import SandboxRecord
from app.domain.models.skill import Skill, SkillScope
from app.domain.models.renderer import Renderer, RendererKind, RendererScope
from app.domain.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from app.domain.models.audit import AuditLog, AuditRiskLevel, AuditStatus
from app.domain.models.safety import SafetyRule, SafetyRuleSeedState
from app.domain.models.approval import ApprovalRequest, ApprovalStatus
from app.domain.models.usage import TokenUsageRecord
from app.domain.models.execution_node import (
    ExecutionNode,
    ExecutionNodeAuthType,
    ExecutionNodeCapacity,
    ExecutionNodeHealth,
    ExecutionNodeStatus,
    ExecutionNodeType,
    SandboxAllocation,
    SandboxAllocationStatus,
)
from app.domain.models.dataset import DataCenterDataset, DatasetFile, DatasetLocation
from pymongo import IndexModel, ASCENDING, DESCENDING
from typing import Any

T = TypeVar('T', bound=BaseModel)

class BaseDocument(Document, Generic[T]):
    def __init_subclass__(cls, id_field="id", domain_model_class: Type[T] = None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._ID_FIELD = id_field
        cls._DOMAIN_MODEL_CLASS = domain_model_class
    
    def update_from_domain(self, domain_obj: T) -> None:
        """Update the document from domain model"""
        data = domain_obj.model_dump(exclude={'id', 'created_at'})
        data[self._ID_FIELD] = domain_obj.id
        if hasattr(self, 'updated_at'):
            data['updated_at'] = datetime.now(UTC)
        
        for field, value in data.items():
            setattr(self, field, value)
    
    def to_domain(self) -> T:
        """Convert MongoDB document to domain model"""
        # Convert to dict and map agent_id to id field
        data = self.model_dump(exclude={'id'})
        data['id'] = data.pop(self._ID_FIELD)
        return self._DOMAIN_MODEL_CLASS.model_validate(data)
    
    @classmethod
    def from_domain(cls, domain_obj: T) -> Self:
        """Create a new MongoDB agent from domain"""
        # Convert to dict and map id to agent_id field
        data = domain_obj.model_dump()
        data[cls._ID_FIELD] = data.pop('id')
        return cls.model_validate(data)

class UserDocument(BaseDocument[User], id_field="user_id", domain_model_class=User):
    """MongoDB document for User"""
    user_id: str
    fullname: str
    email: str  # Now required field for login
    password_hash: Optional[str] = None
    role: UserRole = UserRole.USER
    is_active: bool = True
    registration_status: RegistrationStatus = RegistrationStatus.APPROVED
    registration_reviewed_by: Optional[str] = None
    registration_reviewed_at: Optional[datetime] = None
    registration_review_note: Optional[str] = None
    token_balance: Optional[int] = 0
    token_daily_refill_override: Optional[int] = None
    token_last_refill_date: Optional[date] = None
    auto_enabled_skills: List[str] = Field(default_factory=list)
    installed_skill_ids: List[str] = Field(default_factory=list)
    installed_mcp_names: List[str] = Field(default_factory=list)
    installed_renderer_ids: List[str] = Field(default_factory=list)
    created_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)
    last_login_at: Optional[datetime] = None

    class Settings:
        name = "users"
        indexes = [
            "user_id",
            "fullname",  # Keep fullname index but not unique
            IndexModel([("email", ASCENDING)], unique=True),  # Email as unique index
        ]


class RoleTokenQuotaDocument(Document):
    role: UserRole
    initial_tokens: Optional[int] = 0
    daily_refill_tokens: Optional[int] = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "role_token_quotas"
        indexes = [
            IndexModel([("role", ASCENDING)], unique=True),
        ]


class WorkspaceDocument(BaseDocument[Workspace], id_field="workspace_id", domain_model_class=Workspace):
    workspace_id: str
    name: str
    owner_user_id: str
    is_personal: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "workspaces"
        indexes = [
            "workspace_id",
            IndexModel([("owner_user_id", ASCENDING)]),
        ]


class WorkspaceMemberDocument(BaseDocument[WorkspaceMember], id_field="member_id", domain_model_class=WorkspaceMember):
    member_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    workspace_id: str
    user_id: str
    role: WorkspaceRole = WorkspaceRole.OWNER
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "workspace_members"
        indexes = [
            "member_id",
            IndexModel([("workspace_id", ASCENDING), ("user_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING)]),
        ]


class AuditLogDocument(BaseDocument[AuditLog], id_field="audit_id", domain_model_class=AuditLog):
    audit_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    actor_user_id: str
    workspace_id: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    status: AuditStatus = AuditStatus.SUCCESS
    risk_level: AuditRiskLevel = AuditRiskLevel.LOW
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "audit_logs"
        indexes = [
            "audit_id",
            IndexModel([("actor_user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("workspace_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("resource_type", ASCENDING), ("resource_id", ASCENDING)]),
            IndexModel([("action", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("risk_level", ASCENDING), ("created_at", DESCENDING)]),
        ]


class SafetyRuleDocument(BaseDocument[SafetyRule], id_field="rule_id", domain_model_class=SafetyRule):
    rule_id: str = Field(default_factory=lambda: f"safety_rule_{uuid.uuid4().hex[:16]}")
    name: str
    name_key: str = ""
    description: str = ""
    category: str
    risk_level: str = "high"
    match_type: str = "keyword"
    patterns: List[str] = Field(default_factory=list)
    enabled: bool = True
    reason: str = ""
    suggestion: str = ""
    priority: int = 100
    built_in: bool = False
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "safety_rules"
        indexes = [
            "rule_id",
            IndexModel([("name_key", ASCENDING)], unique=True),
            IndexModel([("enabled", ASCENDING), ("priority", ASCENDING)]),
            IndexModel([("category", ASCENDING), ("updated_at", DESCENDING)]),
        ]

    def to_domain(self) -> SafetyRule:
        return SafetyRule(
            id=self.rule_id,
            name=self.name,
            description=self.description,
            category=self.category,
            risk_level=self.risk_level,
            match_type=self.match_type,
            patterns=self.patterns,
            enabled=self.enabled,
            reason=self.reason,
            suggestion=self.suggestion,
            priority=self.priority,
            built_in=self.built_in,
            created_by=self.created_by,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class SafetyRuleSeedStateDocument(Document):
    state_id: str = "safety_rule_seed_state"
    version: int = 1
    initialized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "safety_rule_seed_state"


class ApprovalRequestDocument(BaseDocument[ApprovalRequest], id_field="approval_id", domain_model_class=ApprovalRequest):
    approval_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    requester_user_id: str
    workspace_id: Optional[str] = None
    resource_type: str
    resource_id: Optional[str] = None
    requested_permissions: List[str] = Field(default_factory=list)
    reason: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer_user_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    decision_note: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "approval_requests"
        indexes = [
            "approval_id",
            IndexModel([("requester_user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("workspace_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("status", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("resource_type", ASCENDING), ("resource_id", ASCENDING)]),
        ]


class TokenUsageDocument(BaseDocument[TokenUsageRecord], id_field="usage_id", domain_model_class=TokenUsageRecord):
    usage_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "token_usage"
        indexes = [
            "usage_id",
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("workspace_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("session_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("model_name", ASCENDING), ("created_at", DESCENDING)]),
        ]


class StoredFileDocument(Document):
    file_id: str
    provider: str
    bucket: Optional[str] = None
    object_key: Optional[str] = None
    filename: str
    content_type: Optional[str] = None
    size: int = 0
    user_id: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    upload_date: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "stored_files"
        indexes = [
            IndexModel([("file_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("provider", ASCENDING)]),
            IndexModel([("bucket", ASCENDING), ("object_key", ASCENDING)]),
        ]


class FileUploadSessionDocument(Document):
    upload_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    file_id: str
    provider: str = "minio"
    bucket: str
    object_key: str
    minio_upload_id: str
    filename: str
    content_type: Optional[str] = None
    size: int = 0
    user_id: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    part_size: int
    status: str = "initiated"
    parts: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "file_upload_sessions"
        indexes = [
            IndexModel([("upload_id", ASCENDING)], unique=True),
            IndexModel([("file_id", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("status", ASCENDING), ("expires_at", ASCENDING)]),
        ]


class ExecutionNodeDocument(BaseDocument[ExecutionNode], id_field="node_id", domain_model_class=ExecutionNode):
    node_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    description: str = ""
    type: ExecutionNodeType = ExecutionNodeType.LOCAL_DOCKER
    status: ExecutionNodeStatus = ExecutionNodeStatus.UNKNOWN
    enabled: bool = True
    base_url: Optional[str] = None
    auth_type: ExecutionNodeAuthType = ExecutionNodeAuthType.NONE
    credential_ref: Optional[str] = None
    runtime_config: Dict[str, Any] = Field(default_factory=dict)
    capacity: ExecutionNodeCapacity = Field(default_factory=ExecutionNodeCapacity)
    labels: Dict[str, str] = Field(default_factory=dict)
    taints: Dict[str, str] = Field(default_factory=dict)
    health: ExecutionNodeHealth = Field(default_factory=ExecutionNodeHealth)
    last_heartbeat_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "execution_nodes"
        indexes = [
            "node_id",
            IndexModel([("name", ASCENDING)], unique=True),
            IndexModel([("enabled", ASCENDING), ("status", ASCENDING)]),
            IndexModel([("type", ASCENDING)]),
            IndexModel([("updated_at", DESCENDING)]),
        ]


class DataCenterDatasetDocument(Document):
    dataset_id: str = Field(default_factory=lambda: f"ds_{uuid.uuid4().hex[:16]}")
    external_id: str = ""
    data_center_id: str
    data_center_name: str
    name: str
    name_key: str
    description: str = ""
    temporal_coverage: str = ""
    spatial_coverage: str = ""
    data_type: str = ""
    tags: List[str] = Field(default_factory=list)
    preview_url: str = ""
    files: List[DatasetFile] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    locations: List[DatasetLocation] = Field(default_factory=list)
    enabled: bool = True
    is_submission: bool = False
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_domain(self) -> DataCenterDataset:
        return DataCenterDataset.model_validate(self.model_dump(exclude={"id"}))

    class Settings:
        name = "data_center_datasets"
        indexes = [
            IndexModel([("dataset_id", ASCENDING)], unique=True),
            IndexModel([("name_key", ASCENDING)], unique=True),
            IndexModel([("enabled", ASCENDING), ("updated_at", DESCENDING)]),
            IndexModel([("created_by", ASCENDING), ("is_submission", ASCENDING), ("updated_at", DESCENDING)]),
            IndexModel([("locations.node_id", ASCENDING)]),
        ]


class TemporaryDatasetDocument(Document):
    """Owner-scoped setup submission kept outside the public catalog."""

    dataset_id: str
    owner_id: str
    dataset: DataCenterDataset
    # Optional only for a rolling upgrade from documents created before the
    # bounded-slot indexes existed. New submissions always populate both.
    owner_slot: Optional[int] = None
    global_slot: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime

    def to_domain(self) -> DataCenterDataset:
        return self.dataset.model_copy(deep=True)

    class Settings:
        name = "temporary_data_center_datasets"
        indexes = [
            IndexModel([("dataset_id", ASCENDING)], unique=True),
            IndexModel(
                [("owner_id", ASCENDING), ("owner_slot", ASCENDING)],
                unique=True,
                name="temporary_dataset_owner_slot_unique",
                partialFilterExpression={"owner_slot": {"$type": "int"}},
            ),
            IndexModel(
                [("global_slot", ASCENDING)],
                unique=True,
                name="temporary_dataset_global_slot_unique",
                partialFilterExpression={"global_slot": {"$type": "int"}},
            ),
            IndexModel([("owner_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="temporary_dataset_expiration_ttl",
            ),
        ]


class NodeCredentialDocument(Document):
    credential_ref: str
    secret_value: str
    purpose: str = "worker_agent_bearer"
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "node_credentials"
        indexes = [
            IndexModel([("credential_ref", ASCENDING)], unique=True),
            IndexModel([("purpose", ASCENDING)]),
        ]


class SandboxAllocationDocument(BaseDocument[SandboxAllocation], id_field="allocation_id", domain_model_class=SandboxAllocation):
    allocation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    node_id: str
    sandbox_id: str
    status: SandboxAllocationStatus = SandboxAllocationStatus.ALLOCATED
    api_url: Optional[str] = None
    vnc_url: Optional[str] = None
    cdp_url: Optional[str] = None
    resource_limits: Dict[str, Any] = Field(default_factory=dict)
    last_heartbeat_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "sandbox_allocations"
        indexes = [
            "allocation_id",
            IndexModel([("session_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("task_id", ASCENDING)]),
            IndexModel([("sandbox_id", ASCENDING), ("status", ASCENDING)]),
            IndexModel([("node_id", ASCENDING), ("status", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("workspace_id", ASCENDING), ("created_at", DESCENDING)]),
        ]


class AgentDocument(BaseDocument[Agent], id_field="agent_id", domain_model_class=Agent):
    """MongoDB document for Agent"""
    agent_id: str
    model_name: str
    temperature: float
    max_tokens: int
    memories: Dict[str, Memory] = {}
    created_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)

    class Settings:
        name = "agents"
        indexes = [
            "agent_id",
        ]


class SessionDocument(BaseDocument[Session], id_field="session_id", domain_model_class=Session):
    """MongoDB model for Session"""
    session_id: str
    user_id: str  # User ID that owns this session
    sandbox_id: Optional[str] = None
    agent_id: str
    task_id: Optional[str] = None
    llm_overrides: Optional[Dict] = None
    dataset_ids: List[str] = Field(default_factory=list)
    sandbox_dataset_ids: List[str] = Field(default_factory=list)
    title: Optional[str] = None
    title_manually_set: bool = False
    unread_message_count: int = 0
    latest_message: Optional[str] = None
    latest_message_at: Optional[datetime] = None
    created_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)
    status: SessionStatus
    files: List[FileInfo] = []
    is_shared: Optional[bool] = False
    collaborator_user_ids: List[str] = Field(default_factory=list)
    client_message_ids: List[str] = Field(default_factory=list)
    class Settings:
        name = "sessions"
        indexes = [
            "session_id",
            "user_id",
            IndexModel([("collaborator_user_ids", ASCENDING)]),
            IndexModel(
                [("user_id", ASCENDING), ("latest_message_at", DESCENDING)],
                name="user_id_latest_message_at",
            ),
        ]


class SessionEventDocument(Document):
    """MongoDB document for a single session event (replaces embedded events array)"""
    session_id: str
    event_key: Optional[str] = None
    event: Dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "session_events"
        indexes = [
            IndexModel([("session_id", ASCENDING), ("created_at", ASCENDING)]),
            IndexModel([("event_key", ASCENDING)], unique=True, sparse=True),
            IndexModel(
                [("event.metadata.dataset_ids", ASCENDING), ("event.role", ASCENDING), ("session_id", ASCENDING)],
                name="dataset_chat_history_lookup",
            ),
        ]


class MCPConfigDocument(Document):
    """MongoDB document storing the global MCP server configuration"""
    config_id: str = "global"
    servers: Dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "mcp_config"
        indexes = [
            IndexModel([("config_id", ASCENDING)], unique=True),
        ]


class APIKeyDocument(BaseDocument[APIKey], id_field="key_id", domain_model_class=APIKey):
    key_id: str
    user_id: str
    name: str
    key_prefix: str
    key_hash: str
    scopes: List[str]
    status: str
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "api_keys"
        indexes = [
            "key_id",
            IndexModel([("key_hash", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING)]),
        ]


class AgentProfileDocument(BaseDocument[AgentProfile], id_field="profile_id", domain_model_class=AgentProfile):
    profile_id: str
    name: str
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    scope: str = "user"
    model_config_id: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model_provider: str = "openai"
    model_name: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 2000
    system_prompt: Optional[str] = None
    planner_config: AgentPlannerConfig = Field(default_factory=AgentPlannerConfig)
    subagents: List[AgentSubAgentConfig] = Field(default_factory=default_subagents)
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "agent_profiles"
        indexes = [
            "profile_id",
            IndexModel([("user_id", ASCENDING)]),
            IndexModel([("workspace_id", ASCENDING)]),
            IndexModel([("scope", ASCENDING), ("name", ASCENDING)]),
        ]



class ModelConfigurationDocument(BaseDocument[ModelConfiguration], id_field="model_config_id", domain_model_class=ModelConfiguration):
    model_config_id: str
    name: str
    description: str = ""
    model_provider: str
    model_name: str
    model_type: ModelType = ModelType.CHAT
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 2000
    extra_config: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "model_configurations"
        indexes = [
            "model_config_id",
            IndexModel([("name", ASCENDING)], unique=True),
            IndexModel([("enabled", ASCENDING), ("updated_at", DESCENDING)]),
        ]


class SandboxRecordDocument(BaseDocument[SandboxRecord], id_field="container_name", domain_model_class=SandboxRecord):
    container_name: str
    container_ip: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    status: str = "warm"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    assigned_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    destroyed_at: Optional[datetime] = None

    class Settings:
        name = "sandbox_records"
        indexes = [
            "container_name",
            IndexModel([("session_id", ASCENDING)]),
            IndexModel([("task_id", ASCENDING)]),
            IndexModel([("status", ASCENDING)]),
        ]


class SkillDocument(BaseDocument[Skill], id_field="skill_id", domain_model_class=Skill):
    skill_id: str
    name: str
    description: str = ""
    triggers: List[str] = Field(default_factory=list)
    priority: int = 0
    max_context_chars: int = 6000
    content: str
    path: str
    scripts: List[str] = Field(default_factory=list)
    references: List[str] = Field(default_factory=list)
    templates: List[str] = Field(default_factory=list)
    scope: SkillScope = SkillScope.GLOBAL
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    created_from_session_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "skills"
        indexes = [
            "skill_id",
            IndexModel([("scope", ASCENDING), ("name", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("name", ASCENDING)]),
            IndexModel([("workspace_id", ASCENDING), ("name", ASCENDING)]),
            IndexModel([("path", ASCENDING)], unique=True),
        ]


class RendererDocument(BaseDocument[Renderer], id_field="renderer_id", domain_model_class=Renderer):
    renderer_id: str
    name: str
    description: str = ""
    kind: RendererKind = RendererKind.API
    extensions: List[str] = Field(default_factory=list)
    scope: RendererScope = RendererScope.USER
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    enabled: bool = True
    api_url: Optional[str] = None
    entry: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "renderers"
        indexes = [
            "renderer_id",
            IndexModel([("scope", ASCENDING), ("name", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("name", ASCENDING)]),
            IndexModel([("workspace_id", ASCENDING), ("name", ASCENDING)]),
        ]
