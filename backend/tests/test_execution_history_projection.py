"""Differential tests against the existing complete-history renderers."""

import json

import pytest

from app.application.services.dataset_request_resolver import DatasetRequestResolver
from app.domain.models.event import (
    DoneEvent, MessageEvent, PlanEvent, PlanStatus, ToolEvent, ToolStatus,
)
from app.domain.models.plan import Plan, Step
from app.domain.models.spill import SpillArtifactNotice, SpillArtifactRef
from app.domain.services.execution_history import ExecutionHistory
from app.domain.services.flows.plan_act import PlanActFlow


def message(text, *, role="user", dataset_ids=None):
    return MessageEvent(
        role=role, message=text,
        metadata={"dataset_ids": dataset_ids} if dataset_ids is not None else None,
    )


def plan(*steps):
    return PlanEvent(status=PlanStatus.UPDATED, plan=Plan(steps=list(steps)))


def spill(number, *, status=ToolStatus.CALLED):
    notice = SpillArtifactNotice(
        status="stored", preview="preview-must-not-enter-followup",
        original_bytes=100, retained_bytes=20, omitted_bytes=80,
        reference=SpillArtifactRef(
            locator=f"spill://artifact/{number:032x}", byte_count=100,
            sha256=f"{number:064x}", media_type="application/json",
            retrieval_hint="Read using the session-scoped spill tool.",
        ),
    )
    return ToolEvent(
        tool_call_id=f"call-{number}", tool_name="plugin",
        function_name="large_result", function_args={}, status=status,
        function_result={"success": True, "data": {"spill": notice.model_dump()}},
    )


def archive(number, *, success=True):
    return ToolEvent(
        tool_call_id=f"unpack-{number}", tool_name="plugin",
        function_name="dataset_unpack", function_args={}, status=ToolStatus.CALLED,
        function_result={"success": success, "data": {
            "status": "completed", "returncode": 0,
            "output": json.dumps({
                "success": True, "source_archive": f"archive-{number % 7}.zip",
                "files": [
                    {"path": f"nested/table-{number % 11}.csv", "size": number},
                    {"path": "../outside.csv", "size": 1},
                ],
            }),
        }},
    )


def numbered(events):
    return [event.model_copy(update={"seq": index}, deep=True)
            for index, event in enumerate(events, start=1)]


def fold(events):
    projection = ExecutionHistory()
    for event in events:
        projection.fold(event)
    return projection


def render(events, current=None):
    flow = PlanActFlow.__new__(PlanActFlow)
    return flow._render_session_context(events, current_user_message=current)


@pytest.fixture(scope="module", params=[1_000, 10_000, 50_000])
def long_history(request):
    # Unique analysis/spill values appear early, then reappear well after
    # eviction from the display tail. Their first-occurrence ordering matters.
    prefix = [message("早期问题", dataset_ids=["early", "early"])]
    for number in range(24):
        prefix.extend([
            plan(Step(success=True, result=f"analysis-{number}")),
            spill(number),
        ])
    suffix = [
        archive(1001), archive(1001, success=False),
        message("  最新有效数据选择  ", dataset_ids=["latest", "latest", "second"]),
        message("没有覆盖数据选择", dataset_ids=[]),
        message("重复的问题"), message("前一次回答", role="assistant"),
        message(" ", role="assistant"), message("placeholder", role="assistant"),
        message("待补充", role="assistant"), message("TODO - do not send", role="assistant"),
        message("中间问题"), message("回答中的 placeholder 是有效内容", role="assistant"),
        message("重复的问题"), message("另一次回答", role="assistant"),
        message("气候数据🙂" * 1000), message("统计值🙂" * 1000, role="assistant"),
        plan(
            Step(agent="vision", result="same-vision", success=False),
            Step(success=True, result="analysis-0"),
            Step(success=True, attachments=[f"artifact-{index}" for index in range(10)]),
        ),
        plan(
            Step(agent="vision", result="same-vision", success=False),
            Step(success=True, attachments=[f"artifact-{index}" for index in range(8)] + ["different-ninth"]),
            Step(success=False, result="failed analysis excluded"),
        ),
        spill(0), spill(999, status=ToolStatus.CALLING),
        message("重复的问题"),
        message("暂无结果", role="assistant"), DoneEvent(),
    ]
    middle = []
    for index in range(request.param - len(prefix) - len(suffix)):
        slot = index % 13
        if slot == 0:
            event = message(f"问题-{index}")
        elif slot == 1:
            event = message(f"回答-{index}", role="assistant")
        elif slot == 2:
            event = plan(Step(success=True, result=f"analysis-{index % 24}"))
        elif slot == 3:
            event = plan(Step(agent="vision", result=f"vision-{index % 5}"))
        elif slot == 4:
            event = spill(index % 24)
        elif slot == 5:
            event = archive(index)
        else:
            event = DoneEvent()
        middle.append(event)
    events = numbered(prefix + middle + suffix)
    return events, fold(events)


@pytest.mark.parametrize("current", [None, "重复的问题", "早期问题", "不存在的问题"])
def test_1k_10k_50k_render_matches_full_history_byte_for_byte(long_history, current):
    events, projection = long_history
    expected = render(events, current)
    assert render(projection, current).encode("utf-8") == expected.encode("utf-8")
    assert "preview-must-not-enter-followup" not in expected
    assert "failed analysis excluded" not in expected
    assert projection.event_count == len(events)
    assert projection.seq == events[-1].seq
    assert projection.latest_plan == next(event for event in reversed(events) if isinstance(event, PlanEvent))
    assert projection.latest_dataset_ids == ["latest", "second"]


def test_1k_10k_50k_resolver_messages_and_archive_inventory_are_unchanged(long_history):
    events, projection = long_history
    kwargs = dict(
        question="读取之前解包文件", datasets=[], selected_skills=[],
        selected_mcp_servers=[], attachment_names=[],
    )
    expected = DatasetRequestResolver._context_payload(events=events, **kwargs)
    actual = DatasetRequestResolver._context_payload(events=projection.resolver_events(), **kwargs)
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert DatasetRequestResolver._archive_inventory_records(projection.resolver_events()) == (
        DatasetRequestResolver._archive_inventory_records(events)
    )
    assert len(projection.archive_events) == sum(
        isinstance(event, ToolEvent) and event.function_name == "dataset_unpack" for event in events
    )


def test_1k_10k_50k_incremental_fold_survives_serialization_restore(long_history):
    events, expected = long_history
    restored = ExecutionHistory()
    # Emulate cold restoration and subsequent delta folds at uneven boundaries.
    boundaries = sorted({1, 9, 17, 127, 513, len(events) // 2, len(events) - 1, len(events)})
    previous = 0
    for boundary in boundaries:
        for event in events[previous:boundary]:
            restored.fold(event)
        restored = ExecutionHistory.model_validate_json(restored.model_dump_json())
        assert render(restored, "重复的问题") == render(events[:boundary], "重复的问题")
        previous = boundary
    assert restored.model_dump(mode="python") == expected.model_dump(mode="python")


@pytest.mark.parametrize("count", [0, 1, 8, 9, 10, 100])
def test_last_nine_retains_exact_last_eight_after_excluding_current(count):
    events = numbered([message(f"question-{index}") for index in range(count)])
    projection = fold(events)
    assert len(projection.conversation) == min(count, 9)
    for current in (None, "question-0", f"question-{max(0, count - 1)}", "not present"):
        assert render(projection, current) == render(events, current)


def test_exclude_only_latest_identical_user_question_not_older_or_assistant():
    events = numbered([
        message("repeat"), message("repeat", role="assistant"),
        message("repeat"), message("newer answer", role="assistant"),
    ])
    expected = render(events, "repeat")
    assert render(fold(events), "repeat") == expected
    assert json.loads(expected)["messages"] == [
        {"role": "user", "content": "repeat"},
        {"role": "assistant", "content": "repeat"},
        {"role": "assistant", "content": "newer answer"},
    ]


def test_duplicate_analysis_and_spills_keep_first_occurrence_but_vision_keeps_latest():
    events = []
    for index in range(12):
        events.extend([
            plan(Step(success=True, result=f"result-{index}")),
            plan(Step(agent="vision", result=f"vision-{index}")), spill(index),
        ])
    events.extend([plan(Step(success=True, result="result-0")), spill(0)])
    events.extend([plan(Step(agent="vision", result="repeated")) for _ in range(3)])
    events = numbered(events)
    projection = fold(events)
    payload = json.loads(render(projection))
    assert render(projection) == render(events)
    assert [item["result"] for item in payload["prior_analysis_results"]] == ["result-9", "result-10", "result-11"]
    assert payload["prior_vision_results"] == ["repeated"] * 3
    assert [item["locator"] for item in payload["spill_artifacts"]] == [
        f"spill://artifact/{index:032x}" for index in range(4, 12)
    ]


def test_with_event_snapshot_does_not_mutate_prior_or_input_event():
    events = numbered([
        message("prior", dataset_ids=["prior-dataset"]),
        plan(Step(success=True, result="prior result")), archive(1), spill(1),
    ])
    prior = fold(events)
    before = prior.model_dump_json()
    current = message("current", dataset_ids=["new-dataset"])
    current.seq = prior.seq + 1
    snapshot = prior.with_event(current)
    assert prior.model_dump_json() == before
    assert snapshot.event_count == prior.event_count + 1
    assert snapshot.latest_dataset_ids == ["new-dataset"]
    snapshot.conversation[-1].message = "mutated snapshot input"
    snapshot.latest_plan.plan.steps[0].result = "mutated snapshot plan"
    snapshot.archive_events[0].function_result["data"]["output"] = "mutated manifest"
    snapshot.spill_references[0]["locator"] = "mutated reference"
    snapshot.seen_analysis.clear()
    assert current.message == "current"
    assert current.metadata == {"dataset_ids": ["new-dataset"]}
    assert prior.model_dump_json() == before


@pytest.mark.parametrize("sequence", [None, 1, 2])
def test_fold_rejects_missing_duplicate_and_out_of_order_sequences_without_mutation(sequence):
    prior = fold(numbered([message("one"), message("two")]))
    before = prior.model_dump_json()
    with pytest.raises(ValueError, match="increasing durable sequences"):
        prior.fold(message("invalid").model_copy(update={"seq": sequence}))
    assert prior.model_dump_json() == before
