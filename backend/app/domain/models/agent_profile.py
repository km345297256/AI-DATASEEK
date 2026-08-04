from typing import Any, Dict, List, Optional
from datetime import datetime, UTC
from pydantic import BaseModel, ConfigDict, Field, field_validator
import secrets

class AgentPlannerConfig(BaseModel):
    system_prompt: Optional[str] = None
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class AgentSubAgentConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    key: str
    name: str
    handler_type: str
    enabled: bool = True
    planner_capability: str = ""
    use_when: str = ""
    avoid_when: str = ""
    input_contract: str = ""
    output_contract: str = ""
    system_prompt: Optional[str] = None
    model_config_id: Optional[str] = None
    model_settings: Dict[str, Any] = Field(default_factory=dict, alias="model_config")
    tool_permissions: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("subagent key is required")
        if not normalized.replace("_", "").replace("-", "").isalnum():
            raise ValueError("subagent key may only contain letters, numbers, hyphen, and underscore")
        return normalized

    @field_validator("handler_type")
    @classmethod
    def validate_handler_type(cls, value: str) -> str:
        normalized = (value or "").strip()
        if normalized not in {"execution", "vision"}:
            raise ValueError("handler_type must be execution or vision")
        return normalized


def default_subagents() -> List[AgentSubAgentConfig]:
    return [
        AgentSubAgentConfig(
            key="execution",
            name="Execution Agent",
            handler_type="execution",
            planner_capability="Run shell commands, read/write files, call enabled tools, write code, analyze data, and produce task artifacts.",
            use_when="Use for coding, data processing, file operations, shell execution, web/file/search/tool operations, and non-visual analysis.",
            avoid_when="Avoid for direct pixel-level image understanding when a vision agent is available.",
            input_contract="User request, sandbox file paths, uploaded attachment paths, previous step outputs.",
            output_contract="Text result, generated file paths, structured analysis, or step completion summary.",
        ),
        AgentSubAgentConfig(
            key="vision",
            name="Vision Agent",
            handler_type="vision",
            planner_capability="Analyze image attachments, screenshots, charts, diagrams, remote-sensing imagery, and OCR-visible text.",
            use_when="Use when the task requires understanding pixels, visual layout, image content, screenshots, charts, maps, or OCR.",
            avoid_when="Avoid for pure shell/code/data-processing work that does not need direct image understanding.",
            input_contract="Image attachments or image file paths plus the user's question.",
            output_contract="Concrete visual observations, OCR text, chart/image interpretation, and answer relevant to the task.",
        ),
    ]


class AgentProfile(BaseModel):
    id: str
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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @staticmethod
    def generate_id() -> str:
        return secrets.token_urlsafe(16)

    @field_validator("subagents")
    @classmethod
    def validate_subagents(cls, value: List[AgentSubAgentConfig]) -> List[AgentSubAgentConfig]:
        keys = [subagent.key for subagent in value]
        if len(keys) != len(set(keys)):
            raise ValueError("subagent keys must be unique")
        return value
