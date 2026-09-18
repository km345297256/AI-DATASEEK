from typing import List, Any
from pydantic import BaseModel, PrivateAttr
from app.domain.models.file import FileInfo
from app.domain.models.dataset import MountedDataset
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.analysis_input import AnalysisInputContext

class Message(BaseModel):
    # Request-local history; never part of Redis, SSE, URLs, or persisted input.
    _session_events_snapshot: Any | None = PrivateAttr(default=None)
    _resume_checkpoint: dict | None = PrivateAttr(default=None)
    _accepted_event_seq: int | None = PrivateAttr(default=None)
    _budget_lineage_id: str | None = PrivateAttr(default=None)
    _budget_origin_seq: int | None = PrivateAttr(default=None)
    # Host-created validation feedback. Never accepted from the client or
    # serialized into plan events, URLs, queued input, or browser storage.
    _artifact_repair_context: dict | None = PrivateAttr(default=None)
    resume_from: str | None = None
    client_message_id: str | None = None
    deliverables: List[DeliverableRequirement] = []
    # The current admission's structured output contract. None is reserved for
    # internal callers without a front-controller decision, not prior turns.
    controller_requires_artifacts: bool | None = None
    message: str = ""
    attachments: List[str] = []
    attachment_file_ids: List[str] = []
    attachment_file_infos: List[FileInfo] = []
    skills: List[str] = []
    mcp_servers: List[str] = []
    datasets: List[MountedDataset] = []
    analysis_inputs: AnalysisInputContext | None = None
    controller_target_files: List[str] = []
    mcp_access_all: bool = False
