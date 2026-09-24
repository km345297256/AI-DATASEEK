from pydantic import BaseModel, Field, PrivateAttr, RootModel
from typing import Dict, Any, Literal, Optional, Union, List, get_args
from datetime import datetime
import time
import uuid
from enum import Enum
from app.domain.models.plan import Plan, Step
from app.domain.models.file import FileInfo
import json
from app.domain.models.search import SearchResultItem
from app.domain.models.analysis_job import AnalysisJobView
from app.domain.models.tool_approval import ToolApprovalView
from app.domain.models.program_attempt import ProgramAttemptView

MAX_EVENT_SEQUENCE = 9_007_199_254_740_991


class PlanStatus(str, Enum):
    """Plan status enum"""
    CREATED = "created"
    UPDATED = "updated"
    COMPLETED = "completed"


class StepStatus(str, Enum):
    """Step status enum"""
    STARTED = "started"
    FAILED = "failed"
    COMPLETED = "completed"


class ToolStatus(str, Enum):
    """Tool status enum"""
    CALLING = "calling"
    CALLED = "called"


class BaseEvent(BaseModel):
    """Base class for agent events"""
    type: Literal[""] = ""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # ``seq`` is allocated atomically per session immediately before an event
    # is published.  It is intentionally optional so events persisted before
    # the versioned envelope was introduced remain readable.
    seq: Optional[int] = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_EVENT_SEQUENCE,
    )
    # Missing versions in legacy documents are interpreted as version 1.
    # Future incompatible envelopes must introduce a new explicit version.
    version: Literal[1] = 1
    timestamp: datetime = Field(default_factory=lambda: datetime.now())

    # This identity belongs to the producer operation, not to the transport.
    # It is deliberately a Pydantic private attribute: Redis replaces ``id``
    # with its stream cursor for the existing SSE contract, while this value
    # remains stable for sequence reservation and is never serialized into an
    # event, recording, API response, or JSON schema.
    _producer_event_id: Optional[str] = PrivateAttr(default=None)

    def bind_producer_event_id(self) -> str:
        """Seal and return the event identity used for durable idempotency.

        Binding is lazy because a few callers assign a deterministic client
        message ID immediately after constructing the model. Once sequence
        reservation starts, later Redis cursor assignment cannot change the
        producer identity.
        """
        if self._producer_event_id is None:
            if not isinstance(self.id, str) or not self.id:
                raise ValueError("Event producer identity must be a non-empty string")
            self._producer_event_id = self.id
        return self._producer_event_id

class ErrorEvent(BaseEvent):
    """Error event"""
    type: Literal["error"] = "error"
    error: str

class PlanEvent(BaseEvent):
    """Plan related events"""
    type: Literal["plan"] = "plan"
    plan: Plan
    status: PlanStatus
    step: Optional[Step] = None

class BrowserToolContent(BaseModel):
    """Browser tool content"""
    screenshot: str

class SearchToolContent(BaseModel):
    """Search tool content"""
    results: List[SearchResultItem]

class ShellToolContent(BaseModel):
    """Shell tool content"""
    console: Any

class FileToolContent(BaseModel):
    """File tool content"""
    content: str

class McpToolContent(BaseModel):
    """MCP tool content"""
    result: Any

class SkillToolContent(BaseModel):
    """Skill tool content"""
    result: Any

ToolContent = Union[
    BrowserToolContent,
    SearchToolContent,
    ShellToolContent,
    FileToolContent,
    McpToolContent,
    SkillToolContent,
]

class ToolEvent(BaseEvent):
    """Tool related events"""
    type: Literal["tool"] = "tool"
    tool_call_id: str
    tool_name: str
    tool_content: Optional[ToolContent] = None
    function_name: str
    function_args: Dict[str, Any]
    status: ToolStatus
    function_result: Optional[Any] = None
    # Optional and additive: old persisted events and SSE clients remain valid.
    presentation: Optional[Dict[str, Any]] = None
    analysis_job: Optional[AnalysisJobView] = None
    tool_approval: Optional[ToolApprovalView] = None
    # First-party execution ledger projection, not derived from tool text.
    program_attempt: Optional[ProgramAttemptView] = None

class TitleEvent(BaseEvent):
    """Title event"""
    type: Literal["title"] = "title"
    title: str

class StepEvent(BaseEvent):
    """Step related events"""
    type: Literal["step"] = "step"
    step: Step
    status: StepStatus

class MessageEvent(BaseEvent):
    """Message event"""
    type: Literal["message"] = "message"
    role: Literal["user", "assistant"] = "assistant"
    message: str
    attachments: Optional[List[FileInfo]] = None
    metadata: Optional[Dict[str, Any]] = None

class DoneEvent(BaseEvent):
    """Done event"""
    type: Literal["done"] = "done"
    advice: Optional[Dict[str, Any]] = None

class WaitEvent(BaseEvent):
    """Wait event"""
    type: Literal["wait"] = "wait"

AgentEvent = Union[
    ErrorEvent,
    PlanEvent, 
    ToolEvent,
    StepEvent,
    MessageEvent,
    DoneEvent,
    TitleEvent,
    WaitEvent,
]
