"""Privileged raw recordings for deterministic developer replay tests.

This module deliberately preserves complete AgentEvent payloads so a replay is
lossless. It is not a public export or sanitization boundary and is not wired to
an HTTP route. A caller that persists a recording must protect it like the
underlying session history and review committed fixture diffs for private data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Iterable, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.domain.models.event import (
    AgentEvent,
    BaseEvent,
    MAX_EVENT_SEQUENCE,
    MessageEvent,
)
from app.domain.models.execution_environment import (
    ExecutionEnvironmentSnapshot,
    safe_public_identifier,
)


EVENT_RECORDING_FORMAT = "ai-dataseek-session-events"
EVENT_RECORDING_FORMAT_VERSION = 1
EVENT_RECORDING_CONTENT_CLASSIFICATION = "privileged-raw"
MAX_RECORDING_BYTES = 64 * 1024 * 1024
MAX_RECORDING_EVENTS = 100_000
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_EVENT_ADAPTER = TypeAdapter(AgentEvent)


class EventRecordingError(ValueError):
    """Raised when a recording cannot be trusted for deterministic replay."""


class EventRecordingHeader(BaseModel):
    """Versioned first line of an offline session-event recording."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["header"] = "header"
    format: Literal["ai-dataseek-session-events"] = EVENT_RECORDING_FORMAT
    format_version: Literal[1] = EVENT_RECORDING_FORMAT_VERSION
    content_classification: Literal["privileged-raw"]
    session_id: str = Field(min_length=1, max_length=128)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_count: int = Field(strict=True, ge=0, le=MAX_RECORDING_EVENTS)
    first_seq: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_EVENT_SEQUENCE,
    )
    through_seq: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_EVENT_SEQUENCE,
    )
    execution_snapshots: tuple[ExecutionEnvironmentSnapshot, ...] = ()
    recording_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if safe_public_identifier(value) != value:
            raise ValueError("recording session ID is not a safe identifier")
        return value

    @model_validator(mode="after")
    def validate_boundaries(self) -> "EventRecordingHeader":
        if self.event_count == 0:
            if self.first_seq is not None or self.through_seq is not None:
                raise ValueError("empty recording cannot declare sequence boundaries")
        elif self.first_seq is None or self.through_seq is None:
            raise ValueError("non-empty recording requires sequence boundaries")
        elif self.first_seq > self.through_seq:
            raise ValueError("recording sequence boundaries are reversed")

        task_ids: set[str] = set()
        for snapshot in self.execution_snapshots:
            if snapshot.session_id != self.session_id:
                raise ValueError("execution snapshot belongs to another session")
            if snapshot.task_id in task_ids:
                raise ValueError("recording contains duplicate execution snapshot task IDs")
            task_ids.add(snapshot.task_id)
        return self


class EventRecordingLine(BaseModel):
    """One complete, unprojected AgentEvent in a recording."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["event"] = "event"
    event: dict[str, Any]


class EventRecording(BaseModel):
    """Validated recording ready for deterministic offline replay."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    header: EventRecordingHeader
    events: tuple[AgentEvent, ...] = ()

    @model_validator(mode="after")
    def validate_event_log(self) -> "EventRecording":
        _validate_event_log(self.events)
        if len(self.events) != self.header.event_count:
            raise ValueError("recording event_count does not match its event log")
        first_seq = self.events[0].seq if self.events else None
        through_seq = self.events[-1].seq if self.events else None
        if first_seq != self.header.first_seq or through_seq != self.header.through_seq:
            raise ValueError("recording sequence boundaries do not match its event log")
        events_by_seq = {
            event.seq: event
            for event in self.events
            if event.seq is not None
        }
        for snapshot in self.header.execution_snapshots:
            trigger_seq = snapshot.trigger_event_seq
            if trigger_seq is None:
                continue
            trigger = events_by_seq.get(trigger_seq)
            if not isinstance(trigger, MessageEvent) or trigger.role != "user":
                raise ValueError(
                    "execution snapshot trigger does not identify a recorded user message"
                )
        return self

    def to_jsonl(self) -> str:
        """Serialize to the canonical, digest-protected JSONL representation."""
        return dump_event_recording(self)


def create_event_recording(
    *,
    session_id: str,
    events: Iterable[AgentEvent | Mapping[str, Any]],
    execution_snapshots: Iterable[ExecutionEnvironmentSnapshot] = (),
    captured_at: datetime | None = None,
) -> EventRecording:
    """Snapshot an ordered event iterable without retaining caller-owned objects.

    Legacy events without ``seq`` are assigned the next available sequence in
    their supplied order. The normalized sequence is written into the recording,
    so every recording produced here is a fixed point when loaded and dumped.
    """
    detached_events = _detach_events(events, synthesize_missing_seq=True)
    snapshots = tuple(
        ExecutionEnvironmentSnapshot.model_validate(
            snapshot.model_dump(mode="json")
        )
        for snapshot in execution_snapshots
    )
    header_values: dict[str, Any] = {
        "session_id": session_id,
        "captured_at": captured_at or datetime.now(UTC),
        "event_count": len(detached_events),
        "first_seq": detached_events[0].seq if detached_events else None,
        "through_seq": detached_events[-1].seq if detached_events else None,
        "execution_snapshots": snapshots,
    }
    digest = _recording_digest(header_values, detached_events)
    header = EventRecordingHeader(
        **header_values,
        content_classification=EVENT_RECORDING_CONTENT_CLASSIFICATION,
        recording_sha256=digest,
    )
    recording = EventRecording(header=header, events=detached_events)
    # A producer must never return an object that its own loader rejects.
    # Validate the encoded byte ceiling now, not only when a caller later dumps.
    dump_event_recording(recording)
    return recording


def dump_event_recording(recording: EventRecording) -> str:
    """Return canonical JSONL, refusing a mutated or forged in-memory record."""
    validated = EventRecording.model_validate(recording)
    expected_digest = _recording_digest(
        validated.header.model_dump(
            mode="python",
            exclude={
                "record_type",
                "format",
                "format_version",
                "content_classification",
                "recording_sha256",
            },
        ),
        validated.events,
    )
    if expected_digest != validated.header.recording_sha256:
        raise EventRecordingError("recording digest does not match its contents")

    lines = [_canonical_json(validated.header.model_dump(mode="json"))]
    lines.extend(
        _canonical_json(EventRecordingLine(event=_event_payload(event)).model_dump(mode="json"))
        for event in validated.events
    )
    encoded = "\n".join(lines) + "\n"
    if len(encoded.encode("utf-8")) > MAX_RECORDING_BYTES:
        raise EventRecordingError("recording exceeds the size limit")
    return encoded


def load_event_recording_jsonl(source: str | bytes) -> EventRecording:
    """Validate and detach a recording without invoking a model or any tools."""
    if isinstance(source, bytes):
        if len(source) > MAX_RECORDING_BYTES:
            raise EventRecordingError("recording exceeds the size limit")
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EventRecordingError("recording is not valid UTF-8") from error
    else:
        text = source
        if len(text.encode("utf-8")) > MAX_RECORDING_BYTES:
            raise EventRecordingError("recording exceeds the size limit")

    # JSON permits U+2028/U+2029 inside strings. ``str.splitlines()`` treats
    # those characters as record boundaries and would make a recording emitted
    # by this module impossible to load. JSONL records are delimited only by the
    # ASCII LF byte; accept one canonical trailing LF and reject blank records.
    # Bound the number of allocations before splitting. A byte-bounded hostile
    # input made almost entirely of LF characters could otherwise allocate
    # millions of tiny Python strings before the event-count check below.
    if text.count("\n") > MAX_RECORDING_EVENTS + 1:
        raise EventRecordingError("recording contains too many events")
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    if not raw_lines:
        raise EventRecordingError("recording is empty")
    if any(not line.strip() for line in raw_lines):
        raise EventRecordingError("recording contains a blank JSONL record")
    if len(raw_lines) - 1 > MAX_RECORDING_EVENTS:
        raise EventRecordingError("recording contains too many events")

    try:
        raw_header = _strict_json_loads(raw_lines[0])
        header = EventRecordingHeader.model_validate(raw_header)
    except (json.JSONDecodeError, ValidationError, ValueError) as error:
        raise EventRecordingError("recording header is invalid") from error

    raw_events: list[dict[str, Any]] = []
    for index, line in enumerate(raw_lines[1:], start=1):
        try:
            row = EventRecordingLine.model_validate(_strict_json_loads(line))
        except (json.JSONDecodeError, ValidationError, ValueError) as error:
            raise EventRecordingError(f"recording event line {index} is invalid") from error
        raw_events.append(row.event)

    try:
        events = _detach_events(raw_events, synthesize_missing_seq=False)
        recording = EventRecording(header=header, events=events)
    except (ValidationError, ValueError) as error:
        raise EventRecordingError("recording event log is invalid") from error

    expected_digest = _recording_digest(
        header.model_dump(
            mode="python",
            exclude={
                "record_type",
                "format",
                "format_version",
                "content_classification",
                "recording_sha256",
            },
        ),
        events,
    )
    if expected_digest != header.recording_sha256:
        raise EventRecordingError("recording digest does not match its contents")
    return recording


def _detach_events(
    events: Iterable[AgentEvent | Mapping[str, Any]],
    *,
    synthesize_missing_seq: bool,
) -> tuple[AgentEvent, ...]:
    detached: list[AgentEvent] = []
    previous_seq = 0
    for index, source in enumerate(events):
        if index >= MAX_RECORDING_EVENTS:
            raise EventRecordingError("recording contains too many events")
        payload = (
            source.model_dump(mode="json")
            if isinstance(source, BaseEvent)
            else dict(source)
        )
        try:
            event = _EVENT_ADAPTER.validate_python(payload)
        except ValidationError as error:
            raise EventRecordingError(f"event at index {index} is invalid") from error
        if event.seq is None:
            if not synthesize_missing_seq:
                raise EventRecordingError(
                    f"event at index {index} has no persisted sequence"
                )
            if previous_seq >= MAX_EVENT_SEQUENCE:
                raise EventRecordingError("recording sequence space is exhausted")
            event.seq = previous_seq + 1
        if event.seq <= previous_seq:
            raise EventRecordingError(
                f"event at index {index} is not in increasing sequence order"
            )
        previous_seq = event.seq
        detached.append(event)
    _validate_event_log(detached)
    return tuple(detached)


def _validate_event_log(events: Iterable[AgentEvent]) -> None:
    previous_seq = 0
    event_ids: set[str] = set()
    count = 0
    for index, event in enumerate(events):
        count += 1
        if count > MAX_RECORDING_EVENTS:
            raise ValueError("recording contains too many events")
        if event.seq is None:
            raise ValueError(f"event at index {index} has no persisted sequence")
        if (
            isinstance(event.seq, bool)
            or event.seq <= previous_seq
            or event.seq > MAX_EVENT_SEQUENCE
        ):
            raise ValueError("recording sequences must be strictly increasing")
        if event.id in event_ids:
            raise ValueError("recording contains duplicate event IDs")
        previous_seq = event.seq
        event_ids.add(event.id)


def _recording_digest(
    header_values: Mapping[str, Any],
    events: Iterable[AgentEvent],
) -> str:
    payload = {
        "format": EVENT_RECORDING_FORMAT,
        "format_version": EVENT_RECORDING_FORMAT_VERSION,
        "header": _json_compatible(dict(header_values)),
        "events": [_event_payload(event) for event in events],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _event_payload(event: AgentEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json")
    # Additive display metadata must not change canonical older recordings or
    # their digest by introducing absent null fields during deserialization.
    if payload.get("analysis_job") is None:
        payload.pop("analysis_job", None)
    if payload.get("tool_approval") is None:
        payload.pop("tool_approval", None)
    if payload.get("program_attempt") is None:
        payload.pop("program_attempt", None)
    return payload


def _json_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        # Use one path for nested timestamps. A model dumped directly in JSON
        # mode may spell UTC as ``Z`` while the same model nested in a Python
        # dump contains a datetime (``+00:00``), which would break fixed-point
        # digest verification after a load.
        return _json_compatible(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _json_compatible(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise EventRecordingError("recording contains a non-JSON value") from error


def _strict_json_loads(line: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(
        line,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_constant,
    )
