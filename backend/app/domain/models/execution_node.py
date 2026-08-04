from datetime import datetime, UTC
from enum import Enum
from typing import Any, Dict, Optional
import uuid

from pydantic import BaseModel, Field


class ExecutionNodeType(str, Enum):
    LOCAL_DOCKER = "local_docker"
    WORKER_AGENT = "worker_agent"
    REMOTE_DOCKER = "remote_docker"
    KUBERNETES = "kubernetes"
    FIXED_SANDBOX = "fixed_sandbox"


class ExecutionNodeStatus(str, Enum):
    UNKNOWN = "unknown"
    CHECKING = "checking"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"
    DRAINING = "draining"
    DELETED = "deleted"


class ExecutionNodeAuthType(str, Enum):
    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    MTLS = "mtls"
    KUBECONFIG = "kubeconfig"
    DOCKER_TLS = "docker_tls"


class ExecutionNodeCapacity(BaseModel):
    max_sandboxes: int = 1
    cpu_cores: Optional[float] = None
    memory_bytes: Optional[int] = None
    disk_bytes: Optional[int] = None
    gpu_count: int = 0


class ExecutionNodeHealth(BaseModel):
    running_sandboxes: int = 0
    warm_sandboxes: int = 0
    assigned_sandboxes: int = 0
    paused_sandboxes: int = 0
    destroyed_sandboxes: int = 0
    cpu_percent: Optional[float] = None
    memory_used_bytes: Optional[int] = None
    disk_used_bytes: Optional[int] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class ExecutionNode(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
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


class SandboxAllocationStatus(str, Enum):
    PENDING = "pending"
    ALLOCATED = "allocated"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    UNHEALTHY = "unhealthy"
    RELEASING = "releasing"
    RELEASED = "released"
    FAILED = "failed"


class SandboxAllocation(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
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
