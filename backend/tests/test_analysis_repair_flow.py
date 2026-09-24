"""Real runner → PlanActFlow → ExecutionAgent repair boundaries, fake I/O only."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services.dataset_request_resolver import (
    ExecutionDecision, FrontControllerResolution, RequestDecision,
)
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.safety import SafetyReview
from app.domain.models.session import SessionStatus
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow


def proof(*, unknown=False, code="completed"):
    return {"code": "tool_execution_unknown" if unknown else code,
            "has_unconfirmed_tool_execution": unknown,
            "side_effect_state": "unknown" if unknown else "confirmed_terminal",
            "execution_evidence": {"execution_confirmed": not unknown, "pending_execution": unknown,
                                   "unresolved_call_count": int(unknown), "replay_safe": False,
                                   "tracked_operation_count": 1}}


def output(name, kind, *, valid=True, reason="invalid_content", digest="a", uploaded=True):
    path = "/home/ubuntu/output/private-repair-dir/" + name
    digest = digest if len(digest) == 64 else digest * 64
    record = {"path": path, "kind": kind, "expected_kind": kind, "valid": valid,
              "reason": "validated" if valid else reason, "sha256": digest, "size": 37,
              "diagnostics": {} if valid else {"row_number": 2, "expected_columns": 2, "actual_columns": 9}}
    info = FileInfo(file_id=(name + "-" + digest) if uploaded else None,
                    filename=name, file_path=path, size=37,
                    metadata={"artifact_sha256": digest})
    return record, info


def scenario(rounds, requirements, *, unknown=False, model_error=False, cancel_after_drain=False,
             agent_key="execution"):
    """Leave orchestration/result parsing real; replace only model/storage APIs."""
    state = {"current": [], "prompts": [], "messages": [], "drained": 0}
    step = Step(id="analysis", agent=agent_key, description="Inspect real measurements and deliver requested outputs",
                inputs={"dataset_intent": "analysis", "artifact_policy": "required"},
                deliverables=[DeliverableRequirement.model_validate(item) for item in requirements])
    plan = Plan(goal="Explain measurements and produce the requested verified outputs", language="zh", steps=[step])
    message = Message(message=plan.goal, client_message_id="fixture-client",
                      deliverables=step.deliverables)
    repo = SimpleNamespace(
        find_by_id=AsyncMock(return_value=SimpleNamespace(status=SessionStatus.PENDING, files=[])),
        get_events=AsyncMock(return_value=[PlanEvent(status=PlanStatus.CREATED, plan=plan)]),
        update_status=AsyncMock(),
    )
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()
    agent.compact_memory = AsyncMock()
    agent._parse_json = AsyncMock(side_effect=json.loads)
    # This harness tests execution/delivery repair, not answer entailment. Its
    # synthetic measured-result draft is explicitly authenticated here; answer
    # grounding regressions override this boundary with the real review service.
    async def verified_fixture_answer(**arguments):
        from app.domain.services.analysis_answer_review import AnswerReviewResult
        return AnswerReviewResult(arguments["draft"], "verified", {"source_count": 1})

    agent.review_delivery_answer = AsyncMock(side_effect=verified_fixture_answer)

    async def execute(prompt, **_kwargs):
        index = len(state["prompts"])
        assert index < len(rounds), "Repair loop executed without new authorized evidence"
        state["prompts"].append(prompt)
        state["messages"].append(agent._current_message.model_copy(deep=True))
        state["current"] = rounds[index]
        agent.last_execution_outcome = proof(unknown=unknown)
        try:
            if model_error and index == 0:
                agent.last_execution_outcome = proof(code="tool_execution_failed")
                yield ErrorEvent(error="Confirmed execution failed before its normal final response")
                return
            yield MessageEvent(message=json.dumps({
                "success": True,
                "result": f"Measured findings from execution {index + 1}; units and limitations are retained.",
                "attachments": [record["path"] for record, _ in state["current"]],
            }))
        finally:
            state["drained"] += 1

    agent._execute_with_tool_scope = execute
    flow = object.__new__(PlanActFlow)
    flow._agent_id, flow._session_id = "fixture-agent", "fixture-session"
    flow._session_repository = repo
    flow.status = AgentStatus.EXECUTING
    async def forbidden_primary_executor(*_args, **_kwargs):
        raise AssertionError("A domain repair must not fall back to the broader primary executor")
        yield  # pragma: no cover - retain an async-generator boundary for the negative fixture.

    flow.executor = (agent if agent_key == "execution" else
                     SimpleNamespace(execute_step=forbidden_primary_executor))
    flow.planner = SimpleNamespace(update_plan=AsyncMock(side_effect=AssertionError("Original step must not be replanned")))
    flow.enabled_subagents = {agent_key: SimpleNamespace(handler_type="execution")}
    flow._domain_agents = {} if agent_key == "execution" else {agent_key: agent}
    flow._dataset_fast_path_active = False
    flow.prepare_execution_environment = lambda _message: None
    flow._activate_skills = lambda _skills: []
    flow._render_session_context = lambda *_args, **_kwargs: ""

    async def validate(items):
        records = {record["path"]: record for record, _ in state["current"]}
        return SimpleNamespace(success=True, data={"version": 1, "files": [deepcopy(records[item["path"]]) for item in items]})

    async def sync_step(event):
        if event.status not in {StepStatus.COMPLETED, StepStatus.FAILED}:
            return []
        return [info.model_copy(deep=True) for record, info in state["current"]
                if info.file_id and record["path"] in event.step.attachments]

    async def require_live(*_args):
        if cancel_after_drain and state["drained"]:
            raise asyncio.CancelledError()

    runner = object.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = "fixture-agent", "fixture-session", "fixture-owner"
    runner._flow, runner._session_repository = flow, repo
    runner._sandbox = SimpleNamespace(id="fixture-sandbox", validate_artifacts=AsyncMock(side_effect=validate))
    runner._artifact_baseline_paths = set()
    runner._pending_artifact_paths = set()
    runner._generated_files = []
    runner._front_controller_resolution = FrontControllerResolution(
        decision=RequestDecision(safety=SafetyReview(decision="allow", risk_level="low"),
                                 execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")),
        answer="", controller_metadata={})
    runner._record_safety_audit = AsyncMock()
    runner._initialize_mcp_tool = AsyncMock()
    runner._open_analysis_runtime = AsyncMock()
    runner._list_sandbox_artifacts = AsyncMock(side_effect=lambda: [record["path"] for record, _ in state["current"]])
    runner._sync_step_attachments_to_storage = AsyncMock(side_effect=sync_step)
    runner._sync_discovered_artifacts_to_storage = AsyncMock(side_effect=lambda **_kwargs: [
        info.model_copy(deep=True) for _, info in state["current"] if info.file_id])
    runner._sync_message_attachments_to_storage = AsyncMock()
    runner._sync_file_to_storage = AsyncMock(return_value=None)
    runner._input_delivery = SimpleNamespace(_require_live=AsyncMock(side_effect=require_live),
                                            mark_analysis_started=AsyncMock(side_effect=require_live))
    runner._accepted_input_key = "fixture-input"
    return runner, flow, step, message, state


async def collect(runner, message):
    # Events normally serialize when yielded; snapshot before mutable Step reuse.
    events = []
    async with asyncio.timeout(5):
        async for event in runner._run_flow(message):
            events.append(event.model_copy(deep=True))
    return events


def terminal_messages(events):
    return [event for event in events if isinstance(event, MessageEvent)
            and not (event.metadata or {}).get("analysis_progress")]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kind,reason", [
    ("values.csv", "table", "inconsistent_table_width"),
    ("chart.png", "image", "invalid_content"),
    ("findings.json", "report", "invalid_json_syntax"),
    ("reproduce.py", "code", "invalid_code_syntax"),
])
async def test_required_artifact_repairs_through_real_flow_without_repeating_original_request(name, kind, reason):
    preserved = output("preserved.png", "image")
    bad = output(name, kind, valid=False, reason=reason)
    fixed = output(name, kind, digest="b")
    runner, flow, step, message, state = scenario([[bad, preserved], [fixed, preserved]],
                                                [{"kind": kind, "min_count": 2 if kind == "image" else 1}])
    events = await collect(runner, message)

    assert len(state["prompts"]) == 2
    assert state["drained"] == 2  # The first generator fully closed before local repair.
    assert state["messages"][0]._artifact_repair_context is None
    context = state["messages"][1]._artifact_repair_context
    assert context["original_goal"] == message.message
    assert context["failed_files"][0]["reason"] == reason
    assert context["constraints"]["replay_original_step"] is False
    assert preserved[0]["path"] in {item["path"] for item in context["protected_files"]}
    assert "host_artifact_validation_feedback" in state["prompts"][1]
    assert message._artifact_repair_context is None
    assert step.success is True and step.outcome.status == "succeeded"
    assert flow.plan.status == ExecutionStatus.COMPLETED
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    terminal_steps = [event for event in events if isinstance(event, StepEvent)
                      and event.status in {StepStatus.COMPLETED, StepStatus.FAILED}]
    assert len(terminal_steps) == 1 and terminal_steps[0].status == StepStatus.COMPLETED
    assert any("Measured findings" in event.message for event in terminal_messages(events))
    assert not any("execution 1" in event.message for event in terminal_messages(events))
    public = json.dumps([event.model_dump(mode="json") for event in events], ensure_ascii=False)
    for private_key in ("failed_files", "protected_files", "previous_analysis", "_artifact_repair_context",
                        "actual_columns", "host_artifact_validation_feedback"):
        assert private_key not in public
    assert "_artifact_repair_context" not in state["messages"][1].model_dump_json()


@pytest.mark.asyncio
async def test_optional_invalid_helper_does_not_block_required_chart_or_erase_explanation():
    chart = output("figure.png", "image")
    helper = output("helper.csv", "table", valid=False, reason="inconsistent_table_width")
    runner, _, step, message, state = scenario([[chart, helper]], [{"kind": "image"}])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1 and step.success
    assert state["messages"][0]._artifact_repair_context is None
    assert step.outcome.issues and all(not issue.blocking for issue in step.outcome.issues)
    final_files = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert chart[1].file_id in {info.file_id for info in final_files}
    assert any("Measured findings" in event.message for event in terminal_messages(events))


@pytest.mark.asyncio
async def test_optional_unsupported_format_does_not_prevent_required_file_local_repair():
    bad = output("table.csv", "table", valid=False, reason="inconsistent_table_width")
    fixed = output("table.csv", "table", digest="b")
    optional = output("optional.pdf", "report", valid=False, reason="unsupported_format")
    runner, _, step, message, state = scenario([[bad, optional], [fixed, optional]], [{"kind": "table"}])
    await collect(runner, message)
    assert len(state["prompts"]) == 2 and step.success
    assert {item["path"] for item in state["messages"][1]._artifact_repair_context["failed_files"]} == {bad[0]["path"]}


@pytest.mark.asyncio
async def test_intermediate_execution_error_does_not_escape_as_terminal_before_authorized_repair():
    bad = output("table.csv", "table", valid=False)
    fixed = output("table.csv", "table", digest="b")
    runner, _, step, message, state = scenario([[bad], [fixed]], [{"kind": "table"}], model_error=True)
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2 and step.success
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_unchanged_required_failure_after_one_local_repair_stops_without_success_claim():
    bad, good = output("values.csv", "table", valid=False), output("plot.png", "image")
    runner, flow, step, message, state = scenario([[bad, good], [bad, good]], [{"kind": "table"}])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2 and state["drained"] == 2
    assert step.success is False and step.outcome.status == "partial"
    assert flow.plan.status == ExecutionStatus.FAILED
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    assert len([event for event in events if isinstance(event, StepEvent)
                and event.status in {StepStatus.COMPLETED, StepStatus.FAILED}]) == 1
    assert all("Measured findings" not in event.message for event in terminal_messages(events))
    assert any("分析说明尚未通过完整核验" in event.message for event in terminal_messages(events))


@pytest.mark.asyncio
async def test_unknown_execution_never_starts_local_repair_despite_repairable_file():
    bad = output("data.csv", "table", valid=False)
    runner, _, step, message, state = scenario([[bad]], [{"kind": "table"}], unknown=True)
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1
    assert not step.success and step.outcome.reason_code == "tool_execution_unknown"
    assert all(item._artifact_repair_context is None for item in state["messages"])
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["unavailable_or_unsafe_path", "validator_unavailable", "unsupported_format"])
async def test_unsafe_or_unavailable_validation_does_not_reexecute_model(reason):
    bad = output("table.csv", "table", valid=False, reason=reason)
    runner, _, step, message, state = scenario([[bad]], [{"kind": "table"}])
    await collect(runner, message)
    assert len(state["prompts"]) == 1 and not step.success


@pytest.mark.asyncio
async def test_cancellation_after_first_generator_drains_prevents_any_repair_model_call():
    bad = output("data.csv", "table", valid=False)
    fixed = output("data.csv", "table", digest="b")
    runner, _, _, message, state = scenario([[bad], [fixed]], [{"kind": "table"}], cancel_after_drain=True)
    with pytest.raises(asyncio.CancelledError):
        await collect(runner, message)
    assert len(state["prompts"]) == 1 and state["drained"] == 1


@pytest.mark.asyncio
async def test_actual_required_deliverable_progress_is_not_stopped_at_sixteen_repairs():
    outputs = [output(f"table-{index}.csv", "table", digest=f"{index:064x}") for index in range(16)]
    outputs += [output(f"chart-{index}.png", "image", digest=f"{index + 16:064x}") for index in range(4)]
    rounds = [outputs[:count] for count in range(21)]
    runner, _, step, message, state = scenario(rounds, [{"kind": "table", "min_count": 16},
                                                       {"kind": "image", "min_count": 4}])
    await collect(runner, message)
    assert len(state["prompts"]) == 21 and step.success


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", [False, True])
async def test_invalid_json_byte_or_name_changes_stop_after_one_unproductive_repair(rename):
    original = output("invalid.json", "report", valid=False, reason="invalid_json_syntax")
    changed = output("renamed.json" if rename else "invalid.json", "report", valid=False,
                     reason="invalid_json_syntax", digest="b")
    changed[0]["size"] += 3
    changed[1].size += 3
    changed[0]["diagnostics"] = {"line_number": 2, "column_number": 3}
    runner, _, step, message, state = scenario([[original], [changed]], [{"kind": "report"}])
    await collect(runner, message)
    assert len(state["prompts"]) == 2 and not step.success


@pytest.mark.asyncio
async def test_smaller_trusted_structural_error_allows_followup_repair():
    bad = output("required.csv", "table", valid=False, reason="inconsistent_table_width")
    improving = output("required.csv", "table", valid=False, reason="inconsistent_table_width", digest="b")
    improving[0]["diagnostics"]["actual_columns"] = 4
    fixed = output("required.csv", "table", digest="c")
    runner, _, step, message, state = scenario([[bad], [improving], [fixed]], [{"kind": "table"}])
    await collect(runner, message)
    assert len(state["prompts"]) == 3 and step.success


@pytest.mark.asyncio
async def test_missing_output_receives_creation_feedback_not_original_operation_replay():
    created = output("required.csv", "table")
    runner, _, step, message, state = scenario([[], [created]], [{"kind": "table"}])
    await collect(runner, message)
    assert len(state["prompts"]) == 2 and step.success
    feedback = state["messages"][1]._artifact_repair_context
    assert feedback["failed_files"] == []
    assert feedback["missing"] == [{"kind": "table", "min_count": 1, "formats": []}]
    assert feedback["constraints"]["replay_original_step"] is False


@pytest.mark.asyncio
async def test_changed_protected_working_copy_stops_even_after_missing_output_is_fixed():
    bad = output("required.csv", "table", valid=False)
    fixed = output("required.csv", "table", digest="b")
    preserved = output("preserved.png", "image")
    overwritten = output("preserved.png", "image", digest="c")
    runner, _, step, message, state = scenario([[bad, preserved], [fixed, overwritten]], [{"kind": "table"}])
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2
    assert not step.success and step.outcome.status != "succeeded"
    # Working-copy mutation never destroys the original immutable upload.
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert preserved[1].file_id in {info.file_id for info in delivered + runner._generated_files}
    assert not any("本次分析已完成" in event.message for event in terminal_messages(events))


@pytest.mark.asyncio
async def test_repair_cannot_relax_an_explicit_required_format_to_claim_completion():
    bad = output("required.csv", "table", valid=False, reason="inconsistent_table_width")
    substitute = output("different.tsv", "table", digest="b")
    runner, _, step, message, state = scenario([[bad], [bad, substitute]],
                                               [{"kind": "table", "formats": ["csv"]}])
    await collect(runner, message)
    assert len(state["prompts"]) == 2 and not step.success
    assert step.outcome.missing[0].formats == ["csv"]
    assert state["messages"][1]._artifact_repair_context["requirements"] == [
        {"kind": "table", "min_count": 1, "formats": ["csv"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_key", ["domain_tabular", "domain_geoscience", "configured_execution_alias"])
async def test_resolved_execution_handler_repairs_without_changing_domain_tool_scope(agent_key):
    bad = output("values.csv", "table", valid=False, reason="inconsistent_table_width")
    fixed = output("values.csv", "table", digest="b")
    runner, flow, step, message, state = scenario([[bad], [fixed]], [{"kind": "table"}], agent_key=agent_key)
    executor = flow._domain_agents[agent_key]
    await collect(runner, message)
    assert len(state["prompts"]) == 2 and step.success
    assert step.agent == agent_key and flow._domain_agents[agent_key] is executor
    assert state["messages"][1]._artifact_repair_context["constraints"]["replay_original_step"] is False


@pytest.mark.asyncio
async def test_valid_bytes_with_failed_upload_do_not_start_content_regeneration():
    valid_not_uploaded = output("complete.csv", "table", uploaded=False)
    runner, _, step, message, state = scenario([[valid_not_uploaded]], [{"kind": "table"}])
    await collect(runner, message)
    assert len(state["prompts"]) == 1 and not step.success
    assert step.outcome.reason_code == "delivery_failed"
    assert state["messages"][0]._artifact_repair_context is None
    runner._sync_file_to_storage.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_content_failure_and_unavailable_storage_never_regenerates_valid_file():
    bad = output("required.csv", "table", valid=False, reason="inconsistent_table_width")
    valid_not_uploaded = output("complete.png", "image", uploaded=False)
    runner, _, step, message, state = scenario([[bad, valid_not_uploaded]],
                                               [{"kind": "table"}, {"kind": "image"}])
    await collect(runner, message)
    assert len(state["prompts"]) == 1 and not step.success
    assert state["messages"][0]._artifact_repair_context is None
    runner._sync_file_to_storage.assert_awaited_once()
    assert runner._sync_file_to_storage.await_args.args[0] == valid_not_uploaded[0]["path"]


@pytest.mark.asyncio
async def test_verified_output_upload_retry_is_host_only_not_another_model_execution():
    valid_not_uploaded = output("complete.csv", "table", uploaded=False)
    runner, _, step, message, state = scenario([[valid_not_uploaded]], [{"kind": "table"}])
    uploaded = valid_not_uploaded[1].model_copy(update={"file_id": "host-upload-retry"})
    runner._sync_file_to_storage.return_value = uploaded
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1 and step.success
    runner._sync_file_to_storage.assert_awaited_once()
    assert uploaded.file_id in {info.file_id for event in terminal_messages(events) for info in event.attachments or []}
