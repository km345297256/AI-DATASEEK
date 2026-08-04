from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import List, Dict, Any, Optional
from enum import Enum
import uuid

class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    success: bool = False
    result: Optional[str] = None
    attachments: List[str] = Field(default_factory=list)


def normalize_execution_status(value):
    if isinstance(value, ExecutionStatus) or value is None:
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        aliases = {
            "success": ExecutionStatus.COMPLETED,
            "succeeded": ExecutionStatus.COMPLETED,
            "done": ExecutionStatus.COMPLETED,
            "complete": ExecutionStatus.COMPLETED,
            "finished": ExecutionStatus.COMPLETED,
            "error": ExecutionStatus.FAILED,
            "failure": ExecutionStatus.FAILED,
            "fail": ExecutionStatus.FAILED,
            "waiting": ExecutionStatus.PENDING,
            "wait": ExecutionStatus.PENDING,
        }
        return aliases.get(normalized, normalized)
    return value

class Step(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    agent: str = "execution"
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    status: ExecutionStatus = ExecutionStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    success: bool = False
    attachments: List[str] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        return normalize_execution_status(value)

    def is_done(self) -> bool:
        return self.status == ExecutionStatus.COMPLETED or self.status == ExecutionStatus.FAILED

class Plan(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    goal: str = ""
    language: Optional[str] = "en"
    steps: List[Step] = Field(default_factory=list)
    message: Optional[str] = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        return normalize_execution_status(value)

    def is_done(self) -> bool:
        return self.status == ExecutionStatus.COMPLETED or self.status == ExecutionStatus.FAILED
    
    def get_next_step(self) -> Optional[Step]:
        for step in self.steps:
            if not step.is_done():
                return step
        return None
    
    def dump_json(self) -> str:
        return self.model_dump_json(include={"goal", "language", "steps"})
