"""Private, rebuildable execution view of the immutable session event log.

This is not a replacement history or an authorization source. Keep the exact
deduplication history and archive manifests: cache-size limits must bypass the
cache, never silently remove evidence. Nothing here is serialized to the UI.
"""
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models.event import BaseEvent, MessageEvent, PlanEvent, ToolEvent, ToolStatus, MAX_EVENT_SEQUENCE
from app.domain.utils.message_text import is_non_substantive_message_text
from app.domain.services.tools.spill_projection import spill_notice_from_result


class ExecutionHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    seq: int = Field(default=0, strict=True, ge=0, le=MAX_EVENT_SEQUENCE)
    event_count: int = Field(default=0, strict=True, ge=0)
    latest_plan: PlanEvent | None = None
    latest_dataset_ids: list[str] = Field(default_factory=list)
    # Nine substantive messages retain eight after excluding the current input.
    conversation: list[MessageEvent] = Field(default_factory=list, max_length=9)
    recent_messages: list[MessageEvent] = Field(default_factory=list, max_length=6)
    vision_results: list[str] = Field(default_factory=list, max_length=3)
    analysis_results: list[tuple[str, tuple[str, ...]]] = Field(default_factory=list, max_length=3)
    seen_analysis: set[str] = Field(default_factory=set)
    spill_references: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    seen_spills: set[str] = Field(default_factory=set)
    archive_events: list[ToolEvent] = Field(default_factory=list)

    def fold(self, event: BaseEvent) -> None:
        """Apply one event exactly once in sequence order, without side effects."""
        if type(event.seq) is not int or event.seq <= self.seq:
            raise ValueError("Execution history requires increasing durable sequences")
        self.seq = event.seq
        self.event_count += 1
        if isinstance(event, MessageEvent):
            if (event.metadata or {}).get("dataset_ids"):
                self.latest_dataset_ids = list(dict.fromkeys(event.metadata["dataset_ids"]))
            self.recent_messages = [*self.recent_messages, event][-6:]
            if event.message.strip() and not (
                event.role == "assistant" and is_non_substantive_message_text(event.message)
            ):
                self.conversation = [*self.conversation, event][-9:]
        if isinstance(event, ToolEvent):
            if event.function_name == "dataset_unpack":
                self.archive_events.append(event)
            if event.status == ToolStatus.CALLED:
                notice = spill_notice_from_result(event.function_result)
                reference = notice.reference if notice is not None else None
                if reference is not None and reference.locator not in self.seen_spills:
                    self.seen_spills.add(reference.locator)
                    self.spill_references = [*self.spill_references, {
                        "locator": reference.locator, "byte_count": reference.byte_count,
                        "sha256": reference.sha256, "media_type": reference.media_type,
                    }][-8:]
        if isinstance(event, PlanEvent):
            self.latest_plan = event
            for step in event.plan.steps:
                if step.agent == "vision" and step.result:
                    self.vision_results = [*self.vision_results, step.result][-3:]
                    continue
                if not step.success or (not step.result and not step.attachments):
                    continue
                result = step.result or "Prior analysis completed."
                attachments = tuple(path for path in step.attachments[:8] if isinstance(path, str) and path)
                key = hashlib.sha256(json.dumps([result, attachments], ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()
                if key not in self.seen_analysis:
                    self.seen_analysis.add(key)
                    self.analysis_results = [*self.analysis_results, (result, attachments)][-3:]

    def with_event(self, event: BaseEvent) -> "ExecutionHistory":
        snapshot = self.model_copy(deep=True)
        snapshot.fold(event.model_copy(deep=True))
        return snapshot

    def resolver_events(self) -> list[BaseEvent]:
        """Preserve the resolver's six messages and complete unpack evidence."""
        return sorted([*self.recent_messages, *self.archive_events], key=lambda event: event.seq or 0)

    def context_conversation(self, current_user_message: str | None) -> list[tuple[str, str]]:
        excluded = None
        if current_user_message:
            excluded = next((i for i in range(len(self.conversation) - 1, -1, -1)
                             if self.conversation[i].role == "user"
                             and self.conversation[i].message == current_user_message), None)
        return [(event.role, event.message.strip()) for i, event in enumerate(self.conversation) if i != excluded]
