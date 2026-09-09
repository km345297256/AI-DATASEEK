"""Whole-turn history windows; event facts and the existing SSE stay unchanged."""
from dataclasses import dataclass

from app.domain.models.event import AgentEvent, MessageEvent


@dataclass(frozen=True)
class SessionHistoryPage:
    events: list[AgentEvent]
    has_more: bool
    next_before_seq: int | None


def page_legacy_history(events: list[AgentEvent], *, turns: int, before_seq: int | None) -> SessionHistoryPage:
    selected = [event for event in events if before_seq is None or (event.seq or 0) < before_seq]
    boundaries = [index for index, event in enumerate(selected)
                  if isinstance(event, MessageEvent) and event.role == "user"]
    start = boundaries[-turns] if len(boundaries) > turns else 0
    page = selected[start:]
    return SessionHistoryPage(page, start > 0, page[0].seq if start > 0 else None)
