from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional
from datetime import datetime

from app.domain.models.agent_profile import AgentPlannerConfig, AgentSubAgentConfig, default_subagents


class CreateAgentProfileRequest(BaseModel):
    name: str
    model_config_id: Optional[str] = None
    model_name: str = "gpt-4o"
    model_provider: str = "openai"
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 2000
    system_prompt: Optional[str] = None
    planner_config: AgentPlannerConfig = Field(default_factory=AgentPlannerConfig)
    subagents: List[AgentSubAgentConfig] = Field(default_factory=default_subagents)
    is_global: bool = False


class UpdateAgentProfileRequest(BaseModel):
    name: Optional[str] = None
    model_config_id: Optional[str] = None
    model_name: Optional[str] = None
    model_provider: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    planner_config: Optional[AgentPlannerConfig] = None
    subagents: Optional[List[AgentSubAgentConfig]] = None
    is_global: Optional[bool] = None


class AgentProfileResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str
    name: str
    user_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    scope: str = "user"
    model_config_id: Optional[str] = None
    model_name: str
    model_provider: str
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float
    max_tokens: int
    system_prompt: Optional[str] = None
    planner_config: AgentPlannerConfig = Field(default_factory=AgentPlannerConfig)
    subagents: List[AgentSubAgentConfig] = Field(default_factory=default_subagents)
    is_active: bool
    created_at: datetime
    updated_at: datetime
