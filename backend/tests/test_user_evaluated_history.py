"""Completed answers remain multi-turn context, never scientific certification."""
import copy
import json

import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import MessageEvent, PlanEvent, PlanStatus
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.analysis_answer_review import AnswerEvidence
from app.domain.services.execution_history import (
    ExecutionHistory, analysis_history_steps, reviewed_history_steps,
)
from app.domain.services.flows.plan_act import PlanActFlow
from test_execution_history_repository import repository


def completed(plan_id="first", *, text="The sample mean is 4.5 mm.",
              datasets=("dataset-a",), files=("upload-a",), success=True):
    return PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(id=plan_id, steps=[Step(
        id="analysis", success=success,
        status=ExecutionStatus.COMPLETED if success else ExecutionStatus.FAILED,
        result=text, attachments=["/home/ubuntu/output/summary.csv"],
        outcome=AnalysisOutcome(status="succeeded" if success else "failed", reason_code="completed"),
        outputs={"result_assessment": {"version": 1, "mode": "user_evaluated",
            "scientific_review": "not_performed", "dataset_ids": list(datasets),
            "input_file_ids": list(files)}})]))


def view(events, compact):
    if not compact:
        return events
    history = ExecutionHistory()
    for seq, event in enumerate(events, 1):
        history.fold(event.model_copy(update={"seq": seq}, deep=True))
    return ExecutionHistory.model_validate_json(history.model_dump_json())


def select(events, compact, datasets=("dataset-a",), files=("upload-a",)):
    return analysis_history_steps(view(events, compact), dataset_ids=set(datasets), input_file_ids=set(files))


@pytest.mark.parametrize("compact", [False, True])
def test_three_turns_retain_result_and_explanation_without_review(compact):
    events = [
        MessageEvent(role="user", message="Summarize the sample."), completed(),
        MessageEvent(role="assistant", message="The sample mean is 4.5 mm."),
        MessageEvent(role="user", message="Explain that mean."),
        completed("followup", text="The preceding mean is the sum divided by the sample count."),
        MessageEvent(role="assistant", message="The preceding mean is the sum divided by the sample count."),
        MessageEvent(role="user", message="How should I interpret that?"),
    ]
    before = [event.model_dump_json() for event in events]
    history = view(events, compact)
    flow = PlanActFlow.__new__(PlanActFlow)
    rendered = json.loads(flow._render_session_context(
        history, current_user_message="How should I interpret that?",
        dataset_ids={"dataset-a"}, input_file_ids={"upload-a"}))
    assert [item["result"] for item in rendered["prior_analysis_results"]] == [
        events[1].plan.steps[0].result, events[4].plan.steps[0].result]
    assert all(item["assessment"] == {"mode": "user_evaluated", "scientific_review": "not_performed"}
               for item in rendered["prior_analysis_results"])
    assert rendered["messages"][-1]["content"].startswith("The preceding mean")
    assert reviewed_history_steps(history, dataset_ids={"dataset-a"}, input_file_ids={"upload-a"}) == []
    evidence = AnswerEvidence()
    for step in select(events, compact):
        evidence.observe_reviewed_result(step)
    assert evidence.render_sources() == []
    assert [event.model_dump_json() for event in events] == before


@pytest.mark.parametrize("compact", [False, True])
def test_failed_followup_and_many_other_scopes_do_not_erase_delivered_context(compact):
    events = [completed(), completed("failed", success=False, text="Execution failed.")]
    events += [completed(str(index), datasets=("other-dataset",), text=f"Other {index}") for index in range(10)]
    assert [step.result for step in select(events, compact)] == [events[0].plan.steps[0].result]


@pytest.mark.parametrize("compact", [False, True])
def test_same_plan_failure_revokes_context_without_erasing_another_plan(compact):
    events = [completed("earlier", text="Earlier result."), completed(), completed(success=False)]
    assert [step.result for step in select(events, compact)] == ["Earlier result."]


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("datasets,files", [((), ()), (("dataset-a",), ()), ((), ("upload-a",)),
    (("dataset-b",), ("upload-a",)), (("dataset-a",), ("upload-b",)),
    (("dataset-a", "dataset-b"), ("upload-a",))])
def test_context_uses_exact_authorized_input_scope(compact, datasets, files):
    assert select([completed()], compact, datasets, files) == []


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("change", ["no_marker", "bad_scope", "claimed_review", "running", "unknown_execution"])
def test_unconfirmed_or_malformed_result_is_not_promoted_to_completed_context(compact, change):
    event = completed()
    step = event.plan.steps[0]
    if change == "no_marker":
        step.outputs.clear()
    elif change == "bad_scope":
        step.outputs["result_assessment"]["dataset_ids"] = [None]
    elif change == "claimed_review":
        step.outputs["result_assessment"]["scientific_review"] = "verified"
    elif change == "running":
        step.status = ExecutionStatus.RUNNING
    else:
        step.outputs["execution_outcome"] = {"has_unconfirmed_tool_execution": True}
    assert select([event], compact) == []


@pytest.mark.parametrize("compact", [False, True])
def test_latest_three_results_keep_chronological_context_across_new_turns(compact):
    events = [completed(str(index), text=f"Answer {index}") for index in range(5)]
    assert [step.result for step in select(events, compact)] == ["Answer 4", "Answer 3", "Answer 2"]


@pytest.mark.asyncio
async def test_v2_projection_rebuilds_private_context_without_rewriting_old_events(monkeypatch):
    event = completed().model_copy(update={"seq": 1}, deep=True)
    stored = {"session_id": "s", "seq": 1, "event": event.model_dump(mode="json")}
    repo, events, sessions = repository(monkeypatch, [stored])
    old_cache = view([event], True).model_dump(mode="json")
    old_cache.pop("contextual_steps_by_plan")
    old_cache["version"] = 2
    sessions.document["execution_history_projection"] = old_cache
    original = copy.deepcopy(events.documents)
    history = await repo.get_execution_history("s", before_seq=2)
    assert history.version == 3
    assert events.body_reads == 1
    assert list(history.contextual_steps_by_plan) == ["first"]
    assert sessions.document["execution_history_projection"]["version"] == 3
    assert events.documents == original
    assert all(set(change["$set"]) == {"execution_history_projection"} for change in sessions.updates)
