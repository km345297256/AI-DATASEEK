"""Default delivery preserves execution results without automatic scientific review.

Independent review helpers remain separately callable and tested. These tests
use synthetic model, sandbox and storage I/O only.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import httpx
from openai import APIStatusError

from app.domain.models.dataset import MountedDataset
from app.domain.models.event import (
    DoneEvent, MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, ToolEvent, ToolStatus,
)
from app.domain.services.analysis_answer_review import AnswerEvidence, AnswerReviewResult, review_answer
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.execution_history import ExecutionHistory
from app.domain.services.flows.plan_act import AgentStatus
from test_analysis_repair_flow import collect, output, scenario, terminal_messages
from test_answer_scientific_scope_routing import with_answer_scope_checks


FALSE_DRAFT = (
    "UNSUPPORTED_DRAFT: Three charts were generated: `overview.png`, "
    "`correlation.png`, and `projection.png`. PCA proves three separated groups."
)
CHECKED_TEXT = "已生成 `overview.png` 和 `quicklook_correlation_32ad.png`，可在附件中查看。"
EXECUTED_FACT = "A distribution chart and Pearson correlation heatmap were generated."
PRIVATE_SENTINEL = "PRIVATE_REVIEW_EVIDENCE_482ce9"


def terminal_payload(events):
    """Tool traces may contain source data; conclusions/plans must not leak it."""
    return json.dumps([event.model_dump(mode="json") for event in events
                       if isinstance(event, (MessageEvent, PlanEvent, StepEvent))], ensure_ascii=False)


def with_observed_results(runner, flow, drafts, *, facts=None, agent_key="execution", emit_tools=True):
    agent = flow._domain_agents.get(agent_key, flow.executor)
    original = agent._execute_with_tool_scope
    position = 0
    runner._handle_tool_event = AsyncMock()
    runner._remember_private_tool_output = Mock()

    async def execute(prompt, **kwargs):
        nonlocal position
        index = position
        position += 1
        async for event in original(prompt, **kwargs):
            if isinstance(event, MessageEvent):
                if emit_tools:
                    yield ToolEvent(
                        tool_call_id=f"observed-{index}", tool_name="shell", function_name="shell_run",
                        function_args={"command": "python3 analysis.py"}, status=ToolStatus.CALLED,
                        function_result={"success": True, "data": {"exit_code": 0,
                            "output": (facts or [EXECUTED_FACT] * len(drafts))[index],
                            "private_diagnostic": PRIVATE_SENTINEL}},
                    )
                payload = json.loads(event.message)
                payload["result"] = drafts[index]
                event = event.model_copy(update={"message": json.dumps(payload)})
            yield event

    agent._execute_with_tool_scope = execute
    return agent


def real_reviewer(agent, ask):
    async def review(**arguments):
        return await review_answer(ask=ask, **arguments)
    agent.review_delivery_answer = AsyncMock(side_effect=review)


def inventory_review_response(messages):
    payload = json.loads(messages[-1].content)
    inventory = next(item for item in payload["sources"] if item["source_id"] == "verified_files")
    return json.dumps({
        "unsupported_claims": True,
        "paragraphs": [{"text": CHECKED_TEXT, "kind": "delivery", "evidence": [
            {"source_id": "verified_files", "quote": inventory["text"]}]}],
        "requirement_checks": [],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("detached_event", [False, True])
async def test_valid_files_publish_model_prose_without_review_or_semantic_replay(monkeypatch, detached_event):
    valid = [output("overview.png", "image"), output("quicklook_correlation_32ad.png", "image")]
    missing = [output(name, "image", valid=False, reason="missing_artifact", uploaded=False)
               for name in ("correlation.png", "projection.png")]
    runner, flow, step, message, state = scenario([valid + missing], [{"kind": "image"}])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT])
    if detached_event:
        original_flow = flow.run
        async def detached_flow(current_message):
            async for event in original_flow(current_message):
                yield event.model_copy(deep=True)
        flow.run = detached_flow
    agent.review_delivery_answer = AsyncMock(side_effect=AssertionError("No scientific review gate"))
    runner._review_scope_completion = AsyncMock(side_effect=AssertionError("No semantic replay"))
    checkpoint = AsyncMock()
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", checkpoint)
    events = await collect(runner, message)
    assert len(state["prompts"]) == state["drained"] == 1
    agent.review_delivery_answer.assert_not_awaited()
    runner._review_scope_completion.assert_not_awaited()
    checkpoint.assert_not_awaited()
    assert step.success and step.outcome.status == "succeeded"
    assert step.result == FALSE_DRAFT  # Interpretation is the user's decision.
    assert step.attachments == [item.file_path for item in runner._generated_files]
    assert {item.file_path for item in runner._generated_files} == {pair[0]["path"] for pair in valid}
    assert not flow._artifact_repair_requests and not runner._analysis_delivery_trackers
    assert not step.outcome.can_resume
    assert any(event.message == FALSE_DRAFT for event in terminal_messages(events))
    assert PRIVATE_SENTINEL not in terminal_payload(events)
    terminals = [event for event in events if isinstance(event, StepEvent)
                 and event.status in {StepStatus.COMPLETED, StepStatus.FAILED}]
    assert len(terminals) == 1 and set(terminals[0].step.attachments) == {pair[0]["path"] for pair in valid}
    assert "answer_review" not in step.outputs
    assert step.outputs["result_assessment"] == {
        "version": 1, "mode": "user_evaluated", "scientific_review": "not_performed",
        "dataset_ids": [], "input_file_ids": [],
    }
    assert all(not issue.blocking for issue in step.outcome.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("review_status", ["verified", "corrected", "unavailable"])
async def test_review_provider_outcome_cannot_change_delivery_or_offer_replay(review_status, monkeypatch):
    chart = output("measured.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT])
    agent.review_delivery_answer = AsyncMock(return_value=AnswerReviewResult(
        "A hypothetical reviewer changed the answer", review_status, {"reason": "review_unavailable"}))
    checkpoint = AsyncMock()
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", checkpoint)
    events = await collect(runner, message)
    agent.review_delivery_answer.assert_not_awaited()
    checkpoint.assert_not_awaited()
    assert len(state["prompts"]) == state["drained"] == 1
    assert step.success and step.outcome.reason_code == "completed"
    assert step.result == FALSE_DRAFT and not step.outcome.can_resume
    assert step.attachments == [chart[0]["path"]]
    assert chart[1].file_id in {item.file_id for event in terminal_messages(events) for item in event.attachments or []}


@pytest.mark.asyncio
@pytest.mark.parametrize("named_output", [False, True])
async def test_scientific_objective_does_not_trigger_automatic_semantic_reexecution(named_output):
    chart = output("comparison.png", "image")
    requirement = {"kind": "image", "objective": "Compare class distributions"}
    if named_output:
        requirement["output_paths"] = [chart[0]["path"]]
    runner, flow, step, message, state = scenario([[chart]], [requirement])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT])
    agent.review_delivery_answer = AsyncMock(return_value=AnswerReviewResult("Method may be inadequate", "corrected", {}, (0,)))
    runner._review_scope_completion = AsyncMock(side_effect=AssertionError("No semantic obligation"))
    await collect(runner, message)
    assert len(state["prompts"]) == 1 and step.success
    assert not flow._artifact_repair_requests and not runner._analysis_delivery_trackers
    agent.review_delivery_answer.assert_not_awaited()
    runner._review_scope_completion.assert_not_awaited()
    assert step.deliverables[0].objective == requirement["objective"]


@pytest.mark.asyncio
async def test_optional_invalid_file_does_not_suppress_analysis_or_weaken_file_validation():
    chart = output("observations.png", "image")
    helper = output("helper.csv", "table", valid=False, reason="inconsistent_table_width")
    runner, flow, step, message, state = scenario([[chart, helper]], [{"kind": "image"}])
    fact = "Observed 12 measurements; median = 4.5 mm."
    agent = with_observed_results(runner, flow, [fact], facts=[fact])
    agent.review_delivery_answer = AsyncMock(side_effect=AssertionError("No model gate"))
    events = await collect(runner, message)
    assert step.success and step.result == fact and len(state["prompts"]) == 1
    assert all(not issue.blocking for issue in step.outcome.issues)
    assert {item.filename for item in runner._generated_files} == {"observations.png"}
    assert any(event.message == fact for event in terminal_messages(events))


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Explain the previous measurements.", "总结此数据集的字段。", "请复核上述结果并说明局限。"])
async def test_text_only_and_requested_review_answers_do_not_need_another_review(question):
    runner, flow, step, message, state = scenario([[]], [])
    step.inputs["artifact_policy"] = "optional"
    message.message = question
    draft = "Here is the requested explanation, including methods and limitations."
    agent = with_observed_results(runner, flow, [draft], emit_tools=False)
    agent.review_delivery_answer = AsyncMock(side_effect=AssertionError("Analysis is already the task"))
    runner._review_scope_completion = AsyncMock(side_effect=AssertionError("No hidden semantic replay"))
    events = await collect(runner, message)
    assert step.success and step.result == draft
    assert not any(isinstance(event, ToolEvent) for event in events)
    assert len(state["prompts"]) == 1
    assert step.outputs["result_assessment"]["scientific_review"] == "not_performed"
    agent.review_delivery_answer.assert_not_awaited()
    runner._review_scope_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_request_resets_old_live_evidence_without_gating_prose():
    old_evidence = AnswerEvidence()
    old_evidence.begin_step("old-task")
    old_evidence.observe(ToolEvent(tool_call_id="stale-tool", tool_name="shell", function_name="shell_run",
        function_args={"command": "old.py"}, status=ToolStatus.CALLED,
        function_result={"success": True, "data": {"exit_code": 0, "output": "STALE_TASK_ONLY"}}))
    runner, flow, step, message, _ = scenario([[]], [])
    step.inputs["artifact_policy"] = "optional"
    runner._analysis_answer_evidence = old_evidence
    agent = with_observed_results(runner, flow, ["A new explanation."], emit_tools=False)
    await collect(runner, message)
    assert runner._analysis_answer_evidence is not old_evidence
    assert "STALE_TASK_ONLY" not in json.dumps(runner._analysis_answer_evidence.render_sources())
    assert step.success and step.result == "A new explanation."
    agent.review_delivery_answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_summary_cannot_introduce_missing_attachments_or_erase_delivered_answer():
    chart = output("overview.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}])
    draft = "The analysis is available for user assessment."
    with_observed_results(runner, flow, [draft])
    ordinary_flow = flow.run
    async def late_summary(current_message):
        async for event in ordinary_flow(current_message):
            if isinstance(event, DoneEvent):
                flow.status = AgentStatus.SUMMARIZING
                yield MessageEvent(message="Invented replacement summary", attachments=[output("projection.png", "image")[1]])
                flow.status = AgentStatus.COMPLETED
            yield event
    flow.run = late_summary
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1 and step.success
    assert all("projection.png" not in item.filename for event in terminal_messages(events) for item in event.attachments or [])
    assert all(event.message == draft for event in terminal_messages(events))


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_key", ["domain_tabular", "domain_geoscience", "configured_execution_alias"])
async def test_domain_execution_does_not_invoke_any_automatic_reviewer(agent_key):
    chart = output("domain-output.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}], agent_key=agent_key)
    agent = with_observed_results(runner, flow, [FALSE_DRAFT], agent_key=agent_key)
    agent.review_delivery_answer = AsyncMock(side_effect=AssertionError("No domain review"))
    flow.executor.review_delivery_answer = AsyncMock(side_effect=AssertionError("Do not cross domain scope"))
    await collect(runner, message)
    assert len(state["prompts"]) == 1 and step.success
    agent.review_delivery_answer.assert_not_awaited()
    flow.executor.review_delivery_answer.assert_not_awaited()

@pytest.mark.asyncio
async def test_execution_summary_reuses_reviewed_step_without_model_or_tool_call():
    chart = output("valid.png", "image")
    runner, flow, step, message, _ = scenario([[chart]], [{"kind": "image"}])
    await collect(runner, message)
    agent = flow.executor
    agent.execute = AsyncMock(side_effect=AssertionError("A model-only summary cannot create new evidence"))
    agent._parse_json = AsyncMock(side_effect=AssertionError("No summary JSON repair"))
    events = [event async for event in ExecutionAgent.summarize(agent)]
    assert len(events) == 1 and events[0].message == step.result
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_agent_review_uses_tool_free_independent_transport(monkeypatch):
    monkeypatch.setattr("app.domain.services.agents.execution.get_settings", lambda: SimpleNamespace(
        llm_retry_attempts=1, llm_retry_base_seconds=1, llm_retry_max_seconds=8))
    agent = object.__new__(ExecutionAgent)
    chart = output("overview.png", "image")[1]
    correlation = output("quicklook_correlation_32ad.png", "image")[1]
    transport = SimpleNamespace(ainvoke=AsyncMock(side_effect=inventory_review_response))
    agent._model = SimpleNamespace(bind=Mock(return_value=transport))
    agent.execute = AsyncMock(side_effect=AssertionError("No execution chain for answer review"))
    agent._parse_json = AsyncMock(side_effect=AssertionError("No hidden model JSON repair"))
    reviewed = await agent.review_delivery_answer(question="Show the data", draft=FALSE_DRAFT,
        files=[chart, correlation], evidence=AnswerEvidence(), requirements=[], language="zh")
    assert reviewed.status == "corrected" and reviewed.text == CHECKED_TEXT
    agent._model.bind.assert_called_once_with(response_format={"type": "json_object"})
    messages = transport.ainvoke.await_args.args[0]
    assert len(messages) == 2
    assert all(message.type != "tool" for message in messages)
    assert all(not getattr(message, "tool_calls", []) for message in messages)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind,expected_calls,expected_delay", [
    ("transient", 2, 2.5), ("provider_wait_too_long", 1, None),
    ("billing", 1, None), ("invalid_json", 2, 1.0),
])
async def test_review_transport_retry_is_model_only_honors_provider_wait_and_never_repairs_json(
        failure_kind, expected_calls, expected_delay, monkeypatch):
    from app.domain.services.agents import execution as execution_module
    from app.domain.services import model_runtime

    if failure_kind == "invalid_json":
        # Syntax errors retain the read-only scope plus fixed feedback; make its existing
        # bounded jitter deterministic without changing provider Retry-After.
        monkeypatch.setattr("app.domain.services.model_retry.random.random", lambda: 0.5)
    request = httpx.Request("POST", "https://provider.invalid/independent-review")
    response = httpx.Response(429, request=request, headers={
        "Retry-After": "999" if failure_kind == "provider_wait_too_long" else "2.5"})
    error = APIStatusError("private provider diagnostic", response=response, body={"error": {
        "code": "insufficient_quota" if failure_kind == "billing" else "rate_limit"}})
    calls = []

    async def invoke(messages):
        calls.append(messages)
        if len(calls) == 1:
            if failure_kind == "invalid_json":
                return "Not a review JSON response."
            raise error
        return inventory_review_response(messages[:2] if failure_kind == "invalid_json" else messages)

    agent = object.__new__(ExecutionAgent)
    transport = SimpleNamespace(ainvoke=AsyncMock(side_effect=invoke))
    agent._model = SimpleNamespace(bind=Mock(return_value=transport))
    agent.execute = AsyncMock(side_effect=AssertionError("Transport retry must never execute tools"))
    agent._parse_json = AsyncMock(side_effect=AssertionError("No model JSON repair"))
    sleep, recorded = AsyncMock(), AsyncMock()
    monkeypatch.setattr(execution_module, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(execution_module, "get_settings", lambda: SimpleNamespace(
        llm_retry_attempts=3, llm_retry_base_seconds=1, llm_retry_max_seconds=8))
    monkeypatch.setattr(model_runtime, "record_model_retry", recorded)
    reviewed = await agent.review_delivery_answer(question="Visualize data", draft=FALSE_DRAFT,
        files=[output("overview.png", "image")[1], output("quicklook_correlation_32ad.png", "image")[1]],
        evidence=AnswerEvidence(), requirements=[], language="zh")
    assert len(calls) == expected_calls
    assert reviewed.status == ("corrected" if failure_kind in {"transient", "invalid_json"} else "unavailable")
    if expected_delay is not None:
        sleep.assert_awaited_once_with(expected_delay)
        recorded.assert_awaited_once()
        assert recorded.await_args.args[1].reason == (
            "exponential_jitter" if failure_kind == "invalid_json" else "retry_after_seconds")
        if failure_kind == "invalid_json":
            assert isinstance(recorded.await_args.args[0], json.JSONDecodeError)
            assert len(calls[1]) == 3 and calls[1][:2] == calls[0]
            assert all(calls[1][index] is calls[0][index] for index in range(2))
            assert "complete, compact JSON object" in calls[1][-1].content
            assert "Do not truncate" in calls[1][-1].content and "Do not use tools" in calls[1][-1].content
        else:
            assert calls[1] is calls[0], "Transport retry must not introduce an executor repair prompt"
    else:
        sleep.assert_not_awaited()
        recorded.assert_not_awaited()
    assert [len(messages) for messages in calls] == ([2, 3] if failure_kind == "invalid_json" else [2] * expected_calls)
    assert all(message.type != "tool" for messages in calls for message in messages)
    assert all(call.kwargs == {"response_format": {"type": "json_object"}}
               for call in agent._model.bind.call_args_list)
    agent.execute.assert_not_called()
    agent._parse_json.assert_not_awaited()
