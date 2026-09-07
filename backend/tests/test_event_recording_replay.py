import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.models.event import (
    MAX_EVENT_SEQUENCE,
    DoneEvent,
    MessageEvent,
    ToolEvent,
    ToolStatus,
)
from app.domain.models.execution_environment import (
    CordisCatalogIdentity,
    ExecutionEnvironmentSnapshot,
    ModelExecutionIdentity,
    SandboxExecutionIdentity,
    stable_sha256,
)
from app.domain.services.event_recording import (
    EventRecordingError,
    create_event_recording,
    load_event_recording_jsonl,
)
from app.domain.services import event_recording as recording_service
from app.interfaces.schemas.event import EventMapper


CAPTURED_AT = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)
REPLAY_FIXTURE = Path(__file__).parent / "fixtures" / "replays" / "versioned_session.jsonl"


def _snapshot(
    session_id: str = "session-recording",
    *,
    trigger_event_seq: int | None = None,
) -> ExecutionEnvironmentSnapshot:
    sandbox = SandboxExecutionIdentity(
        runtime="docker",
        image_reference="ai-dataseek-sandbox:latest",
        dataset_mount_count=1,
        dataset_mounts_digest=stable_sha256(["dataset-public-id"]),
        configuration_digest=stable_sha256({"network": "isolated", "read_only": True}),
    )
    model = ModelExecutionIdentity(
        role="executor",
        provider="deepseek",
        model="deepseek-chat",
        prompt_version="v1",
        prompt_digest=stable_sha256("stable-test-prompt"),
        configuration_digest=stable_sha256({"temperature": 0}),
    )
    catalog = CordisCatalogIdentity(
        status="ready",
        engine="cordis",
        version="1.2.3",
        revision="1" * 64,
        manifest_digest="2" * 64,
        execution_bundle_digest="3" * 64,
        plugin_count=1,
        tool_count=1,
    )
    return ExecutionEnvironmentSnapshot.create(
        task_id="task-recording",
        session_id=session_id,
        execution_mode="agent",
        catalog=catalog,
        sandbox=sandbox,
        models=(model,),
        trigger_event_seq=trigger_event_seq,
        captured_at=CAPTURED_AT,
    )


def _events():
    return [
        MessageEvent(
            id="event-user",
            role="user",
            message="请分析样例数据",
            timestamp=CAPTURED_AT,
        ),
        ToolEvent(
            id="event-tool",
            seq=4,
            tool_call_id="call-1",
            tool_name="dataset_summary",
            function_name="dataset_summary",
            function_args={"dataset_id": "dataset-public-id"},
            function_result={"rows": 12},
            status=ToolStatus.CALLED,
            presentation={"kind": "table", "title": "数据摘要"},
            timestamp=CAPTURED_AT,
        ),
        DoneEvent(id="event-done", seq=5, timestamp=CAPTURED_AT),
    ]


def test_recording_is_a_canonical_fixed_point_and_detaches_legacy_events():
    source_events = _events()
    recording = create_event_recording(
        session_id="session-recording",
        events=source_events,
        execution_snapshots=(_snapshot(trigger_event_seq=1),),
        captured_at=CAPTURED_AT,
    )

    assert source_events[0].seq is None
    assert [event.seq for event in recording.events] == [1, 4, 5]
    assert {event.version for event in recording.events} == {1}

    encoded = recording.to_jsonl()
    restored = load_event_recording_jsonl(encoded)

    assert restored.to_jsonl() == encoded
    assert restored.header.first_seq == 1
    assert restored.header.through_seq == 5
    assert restored.header.content_classification == "privileged-raw"
    assert restored.header.execution_snapshots[0].fingerprint == _snapshot().fingerprint
    assert restored.header.execution_snapshots[0].trigger_event_seq == 1


def test_committed_recording_is_a_normalization_fixed_point():
    encoded = REPLAY_FIXTURE.read_text(encoding="utf-8")

    restored = load_event_recording_jsonl(encoded)

    assert restored.to_jsonl() == encoded
    assert [event.id for event in restored.events] == [
        "event-user",
        "event-tool",
        "event-done",
    ]


@pytest.mark.asyncio
async def test_offline_replay_uses_the_shipping_sse_projection_without_a_model():
    encoded = create_event_recording(
        session_id="session-recording",
        events=_events(),
        execution_snapshots=(_snapshot(),),
        captured_at=CAPTURED_AT,
    ).to_jsonl()

    restored = load_event_recording_jsonl(encoded)
    projected = await EventMapper.events_to_sse_events(list(restored.events))

    assert [event.event for event in projected] == ["message", "tool", "done"]
    assert [event.data.seq for event in projected] == [1, 4, 5]
    assert {event.data.version for event in projected} == {1}
    assert projected[1].data.presentation.kind == "table"
    assert projected[1].data.presentation.title == "数据摘要"


def test_replay_rejects_event_tampering():
    encoded = create_event_recording(
        session_id="session-recording",
        events=_events(),
        execution_snapshots=(_snapshot(),),
        captured_at=CAPTURED_AT,
    ).to_jsonl()

    tampered = encoded.replace("请分析样例数据", "请删除样例数据")

    with pytest.raises(EventRecordingError, match="digest"):
        load_event_recording_jsonl(tampered)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rows: rows[2]["event"].update(seq=1),
            "event log",
        ),
        (
            lambda rows: rows[2]["event"].update(id="event-user"),
            "event log",
        ),
        (
            lambda rows: rows[1]["event"].update(version=2),
            "event log",
        ),
        (
            lambda rows: rows[0].update(format_version=2),
            "header",
        ),
        (
            lambda rows: rows[0].update(first_seq=1.0),
            "header",
        ),
    ],
)
def test_replay_fails_closed_for_invalid_sequence_identity_or_version(mutate, message):
    encoded = create_event_recording(
        session_id="session-recording",
        events=_events(),
        execution_snapshots=(_snapshot(),),
        captured_at=CAPTURED_AT,
    ).to_jsonl()
    rows = [json.loads(line) for line in encoded.splitlines()]
    mutate(rows)
    invalid = "\n".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        for row in rows
    )

    with pytest.raises(EventRecordingError, match=message):
        load_event_recording_jsonl(invalid)


def test_recording_rejects_snapshot_from_another_session():
    with pytest.raises(ValueError, match="another session"):
        create_event_recording(
            session_id="session-recording",
            events=_events(),
            execution_snapshots=(_snapshot("different-session"),),
            captured_at=CAPTURED_AT,
        )


def test_recording_rejects_path_shaped_session_identity():
    with pytest.raises(ValueError, match="safe identifier"):
        create_event_recording(
            session_id="/Users/alice/private-session",
            events=_events(),
            captured_at=CAPTURED_AT,
        )


def test_recording_never_synthesizes_beyond_the_safe_sequence_space():
    with pytest.raises(EventRecordingError, match="exhausted"):
        create_event_recording(
            session_id="session-recording",
            events=[
                DoneEvent(id="last-safe", seq=MAX_EVENT_SEQUENCE),
                DoneEvent(id="legacy-without-seq"),
            ],
            captured_at=CAPTURED_AT,
        )


def test_recording_creator_never_returns_output_its_loader_would_reject(monkeypatch):
    monkeypatch.setattr(recording_service, "MAX_RECORDING_BYTES", 128)

    with pytest.raises(EventRecordingError, match="size limit"):
        create_event_recording(
            session_id="session-recording",
            events=[MessageEvent(role="assistant", message="x" * 256)],
            captured_at=CAPTURED_AT,
        )


def test_replay_rejects_duplicate_json_keys():
    encoded = create_event_recording(
        session_id="session-recording",
        events=_events(),
        captured_at=CAPTURED_AT,
    ).to_jsonl()
    lines = encoded.splitlines()
    lines[0] = lines[0].replace(
        '"record_type":"header"',
        '"record_type":"header","record_type":"header"',
    )

    with pytest.raises(EventRecordingError, match="header"):
        load_event_recording_jsonl("\n".join(lines))


@pytest.mark.parametrize("classification", [None, "public"])
def test_replay_requires_the_privileged_raw_classification(classification):
    encoded = create_event_recording(
        session_id="session-recording",
        events=_events(),
        captured_at=CAPTURED_AT,
    ).to_jsonl()
    rows = [json.loads(line) for line in encoded.splitlines()]
    if classification is None:
        rows[0].pop("content_classification")
    else:
        rows[0]["content_classification"] = classification

    invalid = "\n".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        for row in rows
    )
    with pytest.raises(EventRecordingError, match="header"):
        load_event_recording_jsonl(invalid)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_unicode_line_separators_do_not_split_jsonl_records(separator):
    recording = create_event_recording(
        session_id="session-recording",
        events=[MessageEvent(role="assistant", message=f"before{separator}after")],
        captured_at=CAPTURED_AT,
    )

    encoded = recording.to_jsonl()
    restored = load_event_recording_jsonl(encoded)

    assert restored.to_jsonl() == encoded
    assert restored.events[0].message == f"before{separator}after"


def test_recording_stops_consuming_an_oversized_event_iterable(monkeypatch):
    consumed = []

    def events():
        for index in range(10):
            consumed.append(index)
            yield DoneEvent(id=f"event-{index}", seq=index + 1)

    monkeypatch.setattr(recording_service, "MAX_RECORDING_EVENTS", 2)

    with pytest.raises(EventRecordingError, match="too many events"):
        create_event_recording(
            session_id="session-recording",
            events=events(),
            captured_at=CAPTURED_AT,
        )
    assert consumed == [0, 1, 2]


def test_replay_rejects_excessive_line_count_before_splitting(monkeypatch):
    class SplitBomb(str):
        def split(self, *_args, **_kwargs):
            raise AssertionError("oversized input must be rejected before split")

    monkeypatch.setattr(recording_service, "MAX_RECORDING_EVENTS", 2)

    with pytest.raises(EventRecordingError, match="too many events"):
        load_event_recording_jsonl(SplitBomb("\n" * 4))


@pytest.mark.parametrize("trigger_event_seq", [2, 4])
def test_recording_snapshot_trigger_must_identify_a_recorded_user_message(
    trigger_event_seq,
):
    with pytest.raises(ValueError, match="recorded user message"):
        create_event_recording(
            session_id="session-recording",
            events=_events(),
            execution_snapshots=(
                _snapshot(trigger_event_seq=trigger_event_seq),
            ),
            captured_at=CAPTURED_AT,
        )
