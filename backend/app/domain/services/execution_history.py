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
from app.domain.models.plan import ExecutionStatus, Step
from app.domain.utils.message_text import is_non_substantive_message_text
from app.domain.services.tools.spill_projection import spill_notice_from_result


def _is_reviewed_result(step: Step) -> bool:
    """Index only host-reviewed completed prose, never bare execution success.

    AnswerEvidence independently checks this marker again when admitting it.
    This index makes earlier results discoverable; it adds no new authority.
    """
    marker = step.outputs.get("answer_review")
    return (
        isinstance(marker, dict)
        and type(marker.get("version")) is int and marker["version"] == 1
        and marker.get("status") in {"verified", "corrected"}
        and step.outcome is not None and step.outcome.status == "succeeded"
        and step.success and step.status == ExecutionStatus.COMPLETED
        and isinstance(step.result, str) and bool(step.result.strip())
    )


def _remember_reviewed_plan(index: dict[str, list[Step]], event: PlanEvent) -> None:
    # A later snapshot of the SAME plan replaces its evidence, including a
    # revocation. A failed NEW plan must not erase another plan's valid result.
    # Plan ids matter: planners may reuse step ids such as "analysis".
    index.pop(event.plan.id, None)
    reviewed = [step.model_copy(deep=True) for step in event.plan.steps if _is_reviewed_result(step)]
    if reviewed:
        index[event.plan.id] = reviewed


class ExecutionHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    seq: int = Field(default=0, strict=True, ge=0, le=MAX_EVENT_SEQUENCE)
    event_count: int = Field(default=0, strict=True, ge=0)
    latest_plan: PlanEvent | None = None
    # Keep the latest valid steps per plan across failed follow-ups. Do not
    # truncate by global recency: unrelated scopes cannot evict the only valid
    # result for a selected dataset. Oversized projections bypass persistence.
    reviewed_steps_by_plan: dict[str, list[Step]] = Field(default_factory=dict)
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
            _remember_reviewed_plan(self.reviewed_steps_by_plan, event)
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


def reviewed_history_steps(
    history: ExecutionHistory | list[BaseEvent] | None, *,
    dataset_ids: set[str], input_file_ids: set[str],
) -> list[Step]:
    """Find up to three latest reviewed results in the exact active scope.

    The caller supplies a same-session snapshot cut before the current input;
    no database search, history mutation, new computation or file delivery is
    performed here. Empty selections are empty scopes, not wildcards.
    """
    if isinstance(history, ExecutionHistory):
        index = dict(history.reviewed_steps_by_plan)
        # Also supports an explicitly constructed snapshot and ensures the
        # current latest plan's revocations take precedence over its index.
        if history.latest_plan is not None:
            _remember_reviewed_plan(index, history.latest_plan)
    else:
        index = {}
        for event in history or []:
            if isinstance(event, PlanEvent):
                _remember_reviewed_plan(index, event)

    selected: list[Step] = []
    for steps in reversed(list(index.values())):
        for step in reversed(steps):
            marker = step.outputs.get("answer_review", {})
            scopes = (marker.get("dataset_ids"), marker.get("input_file_ids"))
            if not all(isinstance(value, list) and all(isinstance(item, str) and item for item in value)
                       for value in scopes):
                continue
            if dataset_ids == set(scopes[0]) and input_file_ids == set(scopes[1]):
                selected.append(step)
                if len(selected) == 3:
                    return selected
    return selected
