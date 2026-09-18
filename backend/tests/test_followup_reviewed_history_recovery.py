"""Historical explanation recovery never grants computation or delivery authority.

The production history projection, runner, flow and answer reviewer are exercised
with synthetic events/model responses only. No provider or user task is invoked.
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome, DeliverableRequirement
from app.domain.models.event import DoneEvent, PlanEvent, PlanStatus, ToolEvent
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.analysis_answer_review import AnswerEvidence, review_answer
from app.domain.services.execution_history import ExecutionHistory, reviewed_history_steps
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow
from test_analysis_answer_review_flow import (
    real_reviewer, terminal_payload, with_observed_results,
)
from test_analysis_repair_flow import collect, scenario, terminal_messages
from test_execution_history_repository import repository


FACT = "The historical sample has 12 observations and a median of 4.5 mm."
HISTORICAL_NOTE = "The earlier report describes only the inspected sample."
HISTORICAL_PATH = "/home/ubuntu/output/earlier-observations.png"


def reviewed_plan(*, plan_id="original-plan", dataset_ids=(), input_file_ids=(),
                  text=FACT, status="verified", success=True, seq=None):
    step = Step(id="dataset-fast-path", status=ExecutionStatus.COMPLETED if success else ExecutionStatus.FAILED,
                success=success, result=text, attachments=[HISTORICAL_PATH],
                outcome=AnalysisOutcome(status="succeeded" if success else "failed", reason_code="completed"),
                outputs={"answer_review": {"version": 1, "status": status,
                    "dataset_ids": list(dataset_ids), "input_file_ids": list(input_file_ids)}})
    return PlanEvent(seq=seq, status=PlanStatus.COMPLETED, plan=Plan(id=plan_id, steps=[step]))


def history_view(events, compact):
    if not compact:
        return events
    state = ExecutionHistory()
    for seq, event in enumerate(events, 1):
        state.fold(event.model_copy(update={"seq": seq}, deep=True))
    # Real private caches cross a JSON round trip between input turns.
    return ExecutionHistory.model_validate_json(state.model_dump_json())


def select(events, *, compact=True, datasets=(), files=()):
    return reviewed_history_steps(history_view(events, compact),
                                  dataset_ids=set(datasets), input_file_ids=set(files))


@pytest.mark.parametrize("compact", [False, True])
def test_failed_followup_with_reused_step_id_does_not_erase_earlier_review(compact):
    original = reviewed_plan(dataset_ids=["dataset-a"], input_file_ids=["upload-a"])
    failed = reviewed_plan(plan_id="failed-followup", dataset_ids=["dataset-a"],
                           input_file_ids=["upload-a"], status="unavailable", success=False,
                           text="The explanation could not be checked.")
    steps = select([original, failed], compact=compact, datasets=["dataset-a"], files=["upload-a"])
    assert [step.result for step in steps] == [FACT]
    evidence = AnswerEvidence()
    for step in steps:
        evidence.observe_reviewed_result(step)
    assert [source["kind"] for source in evidence.render_sources()] == ["prior_review"]
    assert evidence.render_sources()[0]["state"] == "historical"
    assert HISTORICAL_PATH in evidence.input_paths()


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("datasets,files", [([], []), (["dataset-a"], []), ([], ["upload-a"]),
    (["dataset-b"], ["upload-a"]), (["dataset-a"], ["upload-b"]),
    (["dataset-a", "dataset-b"], ["upload-a"])])
def test_scope_must_match_exactly_and_empty_selection_is_not_a_wildcard(compact, datasets, files):
    original = reviewed_plan(dataset_ids=["dataset-a"], input_file_ids=["upload-a"])
    assert select([original], compact=compact, datasets=datasets, files=files) == []


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("invalid", [None, "upload-a", [None], [""], {"upload-a": True}])
def test_malformed_scope_cannot_admit_history(compact, invalid):
    original = reviewed_plan()
    original.plan.steps[0].outputs["answer_review"]["input_file_ids"] = invalid
    assert select([original], compact=compact) == []


@pytest.mark.parametrize("compact", [False, True])
def test_latest_same_plan_snapshot_can_revoke_previous_review(compact):
    earlier = reviewed_plan(plan_id="earlier", text="An earlier valid result.")
    original = reviewed_plan()
    revoked = reviewed_plan(status="unavailable", success=False)
    assert [step.result for step in select([earlier, original, revoked], compact=compact)] == [earlier.plan.steps[0].result]


@pytest.mark.parametrize("compact", [False, True])
def test_unrelated_scopes_cannot_evict_the_only_reviewed_result(compact):
    events = [reviewed_plan(dataset_ids=["selected"])]
    for number in range(20):
        events.append(reviewed_plan(plan_id=f"unrelated-{number}", dataset_ids=[f"other-{number}"],
                                    text=f"Unrelated result {number}."))
    assert [step.result for step in select(events, compact=compact, datasets=["selected"])] == [FACT]


@pytest.mark.parametrize("compact", [False, True])
def test_latest_three_same_scope_results_keep_recency_and_do_not_duplicate_updated_plans(compact):
    events = [reviewed_plan(plan_id=f"plan-{number}", text=f"Result {number}.") for number in range(6)]
    events.extend([events[-1].model_copy(deep=True), reviewed_plan(plan_id="failed", success=False)])
    assert [step.result for step in select(events, compact=compact)] == ["Result 5.", "Result 4.", "Result 3."]


@pytest.mark.parametrize("compact", [False, True])
def test_executor_context_and_reviewer_use_same_scope_after_failure(compact):
    original = reviewed_plan(dataset_ids=["selected"])
    events = [original, reviewed_plan(plan_id="failed", dataset_ids=["selected"], success=False)]
    events += [reviewed_plan(plan_id=f"other-{number}", dataset_ids=["other"], text="Other dataset result.")
               for number in range(5)]
    history = history_view(events, compact)
    flow = PlanActFlow.__new__(PlanActFlow)
    payload = json.loads(flow._render_session_context(history, dataset_ids={"selected"}, input_file_ids=set()))
    assert payload["prior_analysis_results"] == [{"result": FACT, "attachments": [HISTORICAL_PATH]}]
    assert "Other dataset result" not in json.dumps(payload)
    assert [step.result for step in reviewed_history_steps(history, dataset_ids={"selected"}, input_file_ids=set())] == [FACT]


@pytest.mark.asyncio
async def test_prior_projection_version_is_rebuilt_privately_without_rewriting_events(monkeypatch):
    events = [reviewed_plan(seq=1), reviewed_plan(plan_id="failed", success=False, seq=2)]
    documents = [{"session_id": "s", "seq": event.seq, "event": event.model_dump(mode="json")} for event in events]
    repo, collection, sessions = repository(monkeypatch, documents)
    previous = history_view(events, True).model_dump(mode="json")
    previous.pop("reviewed_steps_by_plan")
    previous["version"] = 1
    sessions.document["execution_history_projection"] = previous
    result = await repo.get_execution_history("s", before_seq=3)
    assert collection.body_reads == 2
    assert result.version == 2
    assert [step.result for step in reviewed_history_steps(result, dataset_ids=set(), input_file_ids=set())] == [FACT]
    assert documents == collection.documents
    assert all(set(change["$set"]) == {"execution_history_projection"} for change in sessions.updates)


@pytest.mark.asyncio
async def test_future_or_other_session_result_is_never_loaded_by_reviewed_history_index(monkeypatch):
    entries = [("s", reviewed_plan(seq=1)), ("s", reviewed_plan(plan_id="future", seq=3, text="Future result.")),
               ("other", reviewed_plan(plan_id="other-session", seq=2, text="Other session private result."))]
    documents = [{"session_id": session_id, "seq": event.seq, "event": event.model_dump(mode="json")}
                 for session_id, event in entries]
    repo, _, _ = repository(monkeypatch, documents)
    result = await repo.get_execution_history("s", before_seq=3)
    assert [step.result for step in reviewed_history_steps(result, dataset_ids=set(), input_file_ids=set())] == [FACT]
    assert "Future result" not in result.model_dump_json()
    assert "Other session" not in result.model_dump_json()


def followup_scenario(history):
    runner, flow, step, message, state = scenario([[]], [])
    # Production's fast path reuses this id across independently identified
    # plans; history admission must not confuse it with the prior turn.
    step.id = "dataset-fast-path"
    step.inputs["artifact_policy"] = "optional"
    message.message = "Explain the preceding verified result in more detail."
    message._session_events_snapshot = history
    new_plan_event = flow._session_repository.get_events.return_value[0]

    async def create_followup_plan(_message):
        yield new_plan_event

    flow.status = AgentStatus.PLANNING
    flow.planner.create_plan = create_followup_plan
    flow._should_use_dataset_fast_path = lambda _message: False
    flow._normalize_plan_agents = lambda: None
    flow._ensure_vision_step_for_image_message = lambda _message: None
    flow._render_session_context = PlanActFlow._render_session_context.__get__(flow, PlanActFlow)
    agent = with_observed_results(runner, flow, [FACT + "\n\n" + HISTORICAL_NOTE], emit_tools=False)
    return runner, flow, step, message, state, agent


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("input_file_ids", [[], ["synthetic-upload-selection"]])
@pytest.mark.parametrize("correction_mode", ["reclassify", "withdraw", "no_coverage", "unavailable"])
async def test_followup_recovers_older_history_and_repairs_one_paragraph_without_reexecution(
        compact, input_file_ids, correction_mode):
    original = reviewed_plan(text=FACT + "\n\n" + HISTORICAL_NOTE, input_file_ids=input_file_ids)
    failed = reviewed_plan(plan_id="failed-followup", status="unavailable", success=False, input_file_ids=input_file_ids)
    runner, flow, step, message, state, agent = followup_scenario(history_view([original, failed], compact))
    message.attachment_file_ids = list(input_file_ids)
    payloads = []

    async def response(messages):
        payload = json.loads(messages[-1].content)
        payloads.append(payload)
        historical = next(source for source in payload["sources"] if source["kind"] == "prior_review")
        if len(payloads) == 1:
            assert [source["kind"] for source in payload["sources"]] == ["catalog", "prior_review", "delivery_inventory"]
            assert all(source["kind"] != "tool_result" for source in payload["sources"])
            inventory = next(source for source in payload["sources"] if source["kind"] == "delivery_inventory")
            assert json.loads(inventory["text"]) == []
            return json.dumps({"unsupported_claims": False, "paragraphs": [
                {"text": FACT, "kind": "analysis", "evidence": [{"source_id": historical["source_id"], "quote": FACT}]},
                {"text": HISTORICAL_NOTE, "kind": "limitation", "evidence": [{"source_id": historical["source_id"], "quote": HISTORICAL_NOTE}]},
            ], "requirement_checks": []})
        assert [item["index"] for item in payload["failed_paragraphs"]] == [1]
        assert payload["accepted_paragraphs"][0]["paragraph"]["text"] == FACT
        if correction_mode == "unavailable":
            raise RuntimeError("Synthetic correction transport failure")
        paragraph = ({"text": HISTORICAL_NOTE, "kind": "analysis",
                      "evidence": [historical["excerpts"][0]["evidence_id"]]}
                     if correction_mode in {"reclassify", "no_coverage"} else None)
        correction = {"paragraph_corrections": [{"index": 1, "paragraph": paragraph}],
                      "requirement_corrections": []}
        if correction_mode != "no_coverage":
            correction["answer_complete"] = True
        return json.dumps(correction)

    ask = AsyncMock(side_effect=response)
    real_reviewer(agent, ask)
    events = await collect(runner, message)

    assert ask.await_count == 2
    assert len(state["prompts"]) == state["drained"] == 1
    assert not any(isinstance(event, ToolEvent) for event in events)
    assert not runner._generated_files and step.attachments == []
    assert not flow._artifact_repair_requests
    assert not step.outcome.can_resume
    assert step.outputs["answer_review"]["input_file_ids"] == input_file_ids
    assert step.outputs["answer_review"]["source_count"] == 2  # Catalog + history, excludes the inventory.
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    assert FACT in step.result
    assert json.loads(flow.session_context)["prior_analysis_results"][0]["result"] == original.plan.steps[0].result
    public = terminal_payload(events)
    assert "目前没有可确认交付的文件" not in public
    if correction_mode in {"reclassify", "withdraw"}:
        assert step.success and step.outcome.status == "succeeded"
        assert step.outputs["answer_review"]["status"] == "corrected"
        assert "分析说明尚未完成证据核验" not in public
        assert any(FACT in event.message for event in terminal_messages(events))
    else:
        assert not step.success and step.outcome.reason_code == "answer_validation_unavailable"
    assert (HISTORICAL_NOTE in step.result) is (correction_mode in {"reclassify", "no_coverage"})
    if correction_mode in {"reclassify", "no_coverage"}:
        assert "此前已核验的结果说明：" + HISTORICAL_NOTE in step.result
    if correction_mode == "no_coverage":
        assert step.outputs["answer_review"]["answer_coverage_unverified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["new_calculation", "new_delivery"])
async def test_recovered_history_cannot_attest_new_calculation_or_current_file_delivery(claim):
    evidence = AnswerEvidence()
    for step in select([reviewed_plan(), reviewed_plan(plan_id="failed", success=False)]):
        evidence.observe_reviewed_result(step)
    drafts = []

    async def response(messages):
        payload = json.loads(messages[-1].content)
        if drafts:
            raise RuntimeError("No valid correction for fabricated new work")
        drafts.append(payload)
        citation = {"source_id": "prior_review_0001", "quote": FACT}
        return json.dumps({"unsupported_claims": False, "paragraphs": [{
            "text": "A newly computed result was delivered.",
            "kind": "analysis" if claim == "new_calculation" else "delivery", "evidence": [citation]}],
            "requirement_checks": [{"index": 0, "status": "met", "evidence": [citation]}]
                if claim == "new_calculation" else []})

    result = await review_answer(ask=AsyncMock(side_effect=response), question="Perform a new comparison.",
        draft="A newly computed result was delivered.", files=[], evidence=evidence,
        requirements=[DeliverableRequirement(kind="table", objective="Recompute comparison for this request")]
            if claim == "new_calculation" else [])
    assert result.status == "unavailable"
    assert not result.missing_requirement_indices  # Review failure does not authorize analytical replay.
    assert "A newly computed result was delivered." not in result.text
