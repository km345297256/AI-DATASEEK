"""Grounding gates must reconcile successful delivery and public analysis text.

Real runner/flow/execution parsing and the independent answer review service;
only model responses, sandbox execution and storage are synthetic. No datasets,
scripts, provider calls or old tasks are executed by these regressions.
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
async def test_minimum_fulfilled_phantom_claims_are_corrected_without_additional_execution(monkeypatch, detached_event):
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
    ask = AsyncMock(side_effect=inventory_review_response)
    real_reviewer(agent, ask)
    checkpoint = AsyncMock(side_effect=AssertionError("A corrected answer must not checkpoint/replay work"))
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", checkpoint)

    events = await collect(runner, message)

    assert len(state["prompts"]) == state["drained"] == 1
    ask.assert_awaited_once()
    assert step.success and step.outcome.status == "succeeded"
    assert step.result == CHECKED_TEXT
    assert step.attachments == [item.file_path for item in runner._generated_files]
    assert {item.file_path for item in runner._generated_files} == {pair[0]["path"] for pair in valid}
    assert not runner._flow._artifact_repair_requests
    assert not runner._analysis_delivery_trackers
    assert all(item._artifact_repair_context is None for item in state["messages"])
    assert not step.outcome.can_resume
    checkpoint.assert_not_awaited()
    assert any(event.message == CHECKED_TEXT for event in terminal_messages(events))
    public = terminal_payload(events)
    for rejected in (FALSE_DRAFT, "PCA proves", "UNSUPPORTED_DRAFT", PRIVATE_SENTINEL):
        assert rejected not in public
    terminal_steps = [event for event in events if isinstance(event, StepEvent)
                      and event.status in {StepStatus.COMPLETED, StepStatus.FAILED}]
    assert len(terminal_steps) == 1
    # A detached/redacted StepEvent must also lose the rejected attachment list.
    assert set(terminal_steps[0].step.attachments) == {pair[0]["path"] for pair in valid}
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    review_meta = step.outputs["answer_review"]
    assert review_meta["status"] == "corrected"
    assert review_meta["discarded_attachment_claim_count"] == 2
    assert step.outcome.issues == []  # Rejected pure assertions are not real auxiliary failures.
    assert not ({"sources", "draft", "question", "paragraphs", "evidence"} & review_meta.keys())
    assert PRIVATE_SENTINEL not in json.dumps(review_meta)


@pytest.mark.asyncio
async def test_review_unavailability_does_not_offer_replay_even_when_other_checkpoint_conditions_pass(monkeypatch):
    chart = output("measured.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}])
    await collect(runner, message)
    # Make every ordinary execution continuation precondition hold; the answer
    # review failure itself must prevent checkpoint/replay authorization.
    message.datasets = [MountedDataset(data_center_id="fixture", data_center_name="Synthetic",
        name="Synthetic", sandbox_path="/home/ubuntu/datasets/synthetic")]
    step.success, step.result = True, FALSE_DRAFT
    step.outputs["execution_outcome"]["execution_evidence"]["replay_safe"] = True
    flow.executor.review_delivery_answer = AsyncMock(return_value=AnswerReviewResult(
        "已保留文件，分析说明尚未完成证据核验。", "unavailable", {"reason": "review_unavailable"}))
    runner._delivery_audit_store = SimpleNamespace(record=AsyncMock())
    saved = AsyncMock(return_value="a" * 32)
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", saved)
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                        message, [chart[1]], source_seq=32)
    assert step.outcome.reason_code == "answer_validation_unavailable"
    assert not step.outcome.can_resume
    saved.assert_not_awaited()
    assert len(state["prompts"]) == 1


@pytest.mark.asyncio
async def test_independent_reviewer_preserves_supported_measurements_with_optional_bad_helper():
    chart = output("observations.png", "image")
    helper = output("helper.csv", "table", valid=False, reason="inconsistent_table_width")
    runner, flow, step, message, state = scenario([[chart, helper]], [{"kind": "image"}])
    fact = "Observed 12 measurements; median = 4.5 mm."
    agent = with_observed_results(runner, flow, [fact], facts=[fact])

    async def response(messages):
        payload = json.loads(messages[-1].content)
        source = next(item for item in payload["sources"] if item["kind"] == "tool_result")
        return json.dumps({"unsupported_claims": False, "paragraphs": [{"text": fact,
            "kind": "analysis", "evidence": [{"source_id": source["source_id"], "quote": fact}]}],
            "requirement_checks": []})

    real_reviewer(agent, AsyncMock(side_effect=response))
    events = await collect(runner, message)
    assert step.success and step.result == fact
    assert len(state["prompts"]) == 1
    assert all(not issue.blocking for issue in step.outcome.issues)
    assert any(event.message == fact for event in terminal_messages(events))


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
async def test_named_semantically_wrong_artifact_can_be_replaced_without_deleting_original_upload():
    wrong = output("comparison.png", "image")
    correct = output("comparison.png", "image", digest="b")
    protected = output("original-data.csv", "table", digest="c")
    wrong[1].metadata.update(source="sandbox_artifact", session_id="fixture-session")
    requirement = {"kind": "image", "objective": "Compare class distributions",
                   "output_paths": [wrong[0]["path"]]}
    runner, flow, step, message, state = scenario([[wrong, protected], [correct, protected]], [requirement])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT, "The requested comparison was generated."])
    agent.review_delivery_answer = AsyncMock(side_effect=[
        AnswerReviewResult("已生成的图片并未执行要求的分组比较。", "corrected", {}, (0,)),
        AnswerReviewResult("要求的分组比较已完成。", "verified", {}),
    ])
    runner._file_storage = SimpleNamespace(delete_file=AsyncMock())
    events = await collect(runner, message)
    assert len(state["prompts"]) == state["drained"] == 2
    assert step.success and step.outcome.status == "succeeded"
    feedback = state["messages"][1]._artifact_repair_context
    assert {item["path"] for item in feedback["replaceable_working_files"]} == {wrong[0]["path"]}
    assert feedback["replaceable_working_files"][0]["sha256"] == wrong[0]["sha256"]
    assert {item["path"] for item in feedback["protected_files"]} == {protected[0]["path"]}
    assert feedback["constraints"]["overwrite_protected_files"] is False
    assert wrong[1].file_id in runner._analysis_preserved_file_ids
    assert runner._analysis_rejected_versions[wrong[1].file_id].metadata["artifact_sha256"] == wrong[0]["sha256"]
    # Exercise the production storage cleanup gate with a genuinely eligible
    # session artifact, not just an assertion against a test-only collection.
    await runner._delete_replaced_storage_file(wrong[1])
    runner._file_storage.delete_file.assert_not_awaited()
    delivered_ids = {item.file_id for event in terminal_messages(events) for item in event.attachments or []}
    assert correct[1].file_id in delivered_ids and protected[1].file_id in delivered_ids
    assert wrong[1].file_id not in delivered_ids
    assert FALSE_DRAFT not in terminal_payload(events)


@pytest.mark.asyncio
async def test_reviewer_flip_cannot_accept_unchanged_semantically_rejected_bytes():
    wrong = output("comparison.png", "image")
    requirement = {"kind": "image", "objective": "Compare class distributions",
                   "output_paths": [wrong[0]["path"]]}
    runner, flow, step, message, state = scenario([[wrong], [wrong]], [requirement])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT, "REVIEWER_FLIP: the requested comparison was generated."])
    agent.review_delivery_answer = AsyncMock(side_effect=[
        AnswerReviewResult("图片内容未完成分组比较。", "corrected", {}, (0,)),
        AnswerReviewResult("REVIEWER_FLIP: completed after identical bytes.", "verified", {}),
    ])
    events = await collect(runner, message)
    assert len(state["prompts"]) == state["drained"] == 2
    assert not step.success and step.outcome.status == "partial"
    assert step.outcome.reason_code == "analytical_requirements_missing"
    assert step.outcome.missing[0].output_paths == [wrong[0]["path"]]
    assert runner._analysis_delivery_trackers[step.id]._stop_reason == "artifact_repair_no_progress"
    assert not runner._flow._artifact_repair_requests
    assert "REVIEWER_FLIP" not in terminal_payload(events)
    assert wrong[1].file_id in {item.file_id for event in terminal_messages(events)
                               for item in event.attachments or []}


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_basename", [False, True])
async def test_actual_attempted_auxiliary_failure_survives_pure_phantom_claim_cleanup(shared_basename):
    valid = [output("overview.png", "image"), output("quicklook_correlation_32ad.png", "image")]
    attempted = output("helper.png", "image", valid=False, reason="missing_artifact", uploaded=False)
    phantom = output("phantom.png", "image", valid=False, reason="missing_artifact", uploaded=False)
    pairs = valid + [attempted, phantom]
    if shared_basename:
        sibling = output("phantom.png", "image", valid=False, reason="missing_artifact", uploaded=False)
        sibling[0]["path"] = sibling[1].file_path = "/home/ubuntu/output/another-directory/phantom.png"
        pairs.append(sibling)
    runner, flow, step, message, state = scenario([pairs], [{"kind": "image"}])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT], facts=[
        f"{EXECUTED_FACT} Optional rendering of {attempted[0]['path']} failed before writing a file."])
    real_reviewer(agent, AsyncMock(side_effect=inventory_review_response))
    events = await collect(runner, message)
    assert step.success and len(state["prompts"]) == 1
    names = {issue.artifact_name for issue in step.outcome.issues}
    assert "helper.png" in names
    assert ("phantom.png" in names) is shared_basename
    assert all(not issue.blocking for issue in step.outcome.issues)
    assert step.outputs["answer_review"]["discarded_attachment_claim_count"] == (0 if shared_basename else 1)
    assert FALSE_DRAFT not in terminal_payload(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("review_version", [1, None, 0, True])
@pytest.mark.parametrize("compact_history", [False, True])
@pytest.mark.parametrize("selected_scope", ["historical", "different_dataset", "different_file"])
async def test_followup_may_explain_only_versioned_reviewed_history_without_running_new_tools(
        review_version, compact_history, selected_scope, monkeypatch):
    fact = "The inspected sample contains 12 observations, with median 4.5 mm."
    prior_runner, prior_flow, prior_step, prior_message, _ = scenario([[output("previous.png", "image")]],
                                                                   [{"kind": "image"}])
    prior_flow.executor.review_delivery_answer = AsyncMock(return_value=AnswerReviewResult(fact, "verified", {}))
    await collect(prior_runner, prior_message)
    prior_step.outputs["answer_review"]["version"] = review_version
    history = [PlanEvent(status=PlanStatus.COMPLETED, plan=prior_flow.plan.model_copy(deep=True))]

    runner, flow, step, message, state = scenario([[]], [])
    step.inputs["artifact_policy"] = "optional"
    message.message = "Explain the previous measurements in plain language."
    message._session_events_snapshot = ExecutionHistory(latest_plan=history[0]) if compact_history else history
    if selected_scope == "different_dataset":
        message.datasets = [MountedDataset(dataset_id="new-dataset", data_center_id="fixture",
            data_center_name="Synthetic", name="New selection", sandbox_path="/home/ubuntu/datasets/new")]
        monkeypatch.setattr("app.domain.services.analysis_checkpoint.fingerprints", AsyncMock(return_value={}))
    elif selected_scope == "different_file":
        message.attachment_file_ids = ["new-input-file"]
    # Model planning is synthetic; its newly authorized step is real and must
    # not accidentally reuse the already completed historical plan.
    new_plan_event = flow._session_repository.get_events.return_value[0]

    async def create_followup_plan(_message):
        yield new_plan_event

    flow.status = AgentStatus.PLANNING
    flow.planner.create_plan = create_followup_plan
    flow._should_use_dataset_fast_path = lambda _message: False
    flow._normalize_plan_agents = lambda: None
    flow._ensure_vision_step_for_image_message = lambda _message: None
    agent = with_observed_results(runner, flow, ["A plain-language explanation of the prior findings."], emit_tools=False)
    observed_payloads = []

    async def response(messages):
        payload = json.loads(messages[-1].content)
        observed_payloads.append(payload)
        reviewed = [source for source in payload["sources"] if source["kind"] == "prior_review"]
        # Invalid historical provenance must not be salvageable by echoing
        # historical raw text into an invented evidence id.
        source_id = reviewed[0]["source_id"] if reviewed else "prior_review_0001"
        return json.dumps(with_answer_scope_checks(payload, {"unsupported_claims": False, "paragraphs": [{
            "text": fact, "kind": "analysis", "evidence": [{"source_id": source_id, "quote": fact}]}],
            "requirement_checks": []}))

    real_reviewer(agent, AsyncMock(side_effect=response))
    events = await collect(runner, message)
    trusted = type(review_version) is int and review_version == 1 and selected_scope == "historical"
    assert step.success is trusted
    assert not any(isinstance(event, ToolEvent) for event in events)
    assert len(state["prompts"]) == 1
    assert all(source["kind"] != "tool_result" for source in observed_payloads[0]["sources"])
    assert any(source["kind"] == "prior_review" for source in observed_payloads[0]["sources"]) is trusted
    scope = json.loads(observed_payloads[0]["question"])
    assert scope["user_question"] == message.message
    assert scope["current_step"]["id"] == step.id
    if trusted:
        assert step.result == fact
    else:
        assert fact not in terminal_payload(events)
        # This fixture repeats a full-answer response during scoped correction;
        # that is a protocol failure, not a substantive scientific verdict.
        assert step.outcome.reason_code == "answer_validation_unavailable"
        assert step.outputs["answer_review"]["validation_state"] == "unavailable"


@pytest.mark.asyncio
async def test_new_request_resets_old_live_evidence_instead_of_accepting_a_previous_task_claim():
    stale_fact = "STALE_TASK_ONLY: ratio = 97.3 percent."
    old_evidence = AnswerEvidence()
    old_evidence.begin_step("old-task")
    old_evidence.observe(ToolEvent(tool_call_id="stale-tool", tool_name="shell", function_name="shell_run",
        function_args={"command": "old.py"}, status=ToolStatus.CALLED,
        function_result={"success": True, "data": {"exit_code": 0, "output": stale_fact}}))
    runner, flow, step, message, _ = scenario([[]], [])
    step.inputs["artifact_policy"] = "optional"
    runner._analysis_answer_evidence = old_evidence
    agent = with_observed_results(runner, flow, [stale_fact], emit_tools=False)
    payloads = []

    async def response(messages):
        payload = json.loads(messages[-1].content)
        payloads.append(payload)
        return json.dumps({"unsupported_claims": False, "paragraphs": [{
            "text": stale_fact, "kind": "analysis", "evidence": [{
                "source_id": "tool_0001_result", "quote": stale_fact}]}], "requirement_checks": []})

    real_reviewer(agent, AsyncMock(side_effect=response))
    events = await collect(runner, message)
    assert runner._analysis_answer_evidence is not old_evidence
    assert stale_fact not in json.dumps(payloads[0]["sources"])
    assert not step.success and step.outcome.reason_code == "answer_validation_unavailable"
    assert stale_fact not in terminal_payload(events)


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


@pytest.mark.asyncio
async def test_later_summary_cannot_resurrect_corrected_claims_or_missing_attachments():
    valid = [output("overview.png", "image"), output("quicklook_correlation_32ad.png", "image")]
    runner, flow, step, message, state = scenario([valid], [{"kind": "image"}])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT])
    real_reviewer(agent, AsyncMock(side_effect=inventory_review_response))
    ordinary_flow = flow.run

    async def late_summary(current_message):
        async for event in ordinary_flow(current_message):
            if isinstance(event, DoneEvent):
                flow.status = AgentStatus.SUMMARIZING
                yield MessageEvent(message=FALSE_DRAFT, attachments=[
                    output("projection.png", "image")[1]])
                flow.status = AgentStatus.COMPLETED
            yield event

    flow.run = late_summary
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1 and step.success
    assert FALSE_DRAFT not in terminal_payload(events)
    assert all("projection.png" not in item.filename for event in terminal_messages(events)
               for item in event.attachments or [])
    assert all(event.message == CHECKED_TEXT for event in terminal_messages(events))


@pytest.mark.asyncio
async def test_unavailable_answer_review_preserves_files_without_checkpoint_or_replay(monkeypatch):
    chart = output("measured.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT])
    ask = AsyncMock(side_effect=RuntimeError("Provider error with " + PRIVATE_SENTINEL))
    real_reviewer(agent, ask)
    checkpoint = AsyncMock()
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", checkpoint)
    events = await collect(runner, message)
    assert len(state["prompts"]) == state["drained"] == 1
    ask.assert_awaited_once()
    assert not step.success and step.outcome.status == "partial"
    assert step.outcome.reason_code == "answer_validation_unavailable"
    assert not step.outcome.can_resume and step.outcome.resume_from is None
    assert step.attachments == [chart[0]["path"]]
    assert chart[1].file_id in {item.file_id for event in terminal_messages(events)
                               for item in event.attachments or []}
    assert all(item._artifact_repair_context is None for item in state["messages"])
    assert PRIVATE_SENTINEL not in terminal_payload(events)
    assert FALSE_DRAFT not in terminal_payload(events)
    assert "核验" in step.result
    checkpoint.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("correct_reference", [True, False])
async def test_citation_recovery_never_reexecutes_analysis_or_discards_accepted_delivery(correct_reference, monkeypatch):
    chart = output("measured.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}])
    measured = "Observed 12 measurements; median = 4.5 mm."
    delivered = "已生成 `measured.png`，可在附件中查看。"
    agent = with_observed_results(runner, flow, [FALSE_DRAFT], facts=[measured])
    observed_payloads = []

    async def respond(messages):
        payload = json.loads(messages[-1].content)
        observed_payloads.append(payload)
        if len(observed_payloads) == 1:
            inventory = next(item for item in payload["sources"] if item["source_id"] == "verified_files")
            return json.dumps({"unsupported_claims": True, "paragraphs": [
                {"text": delivered, "kind": "delivery", "evidence": [{"source_id": "verified_files", "quote": inventory["text"]}]},
                {"text": measured, "kind": "analysis", "evidence": [{"source_id": "tool_9999_result", "quote": measured}]}],
                "requirement_checks": []})
        assert [item["index"] for item in payload["failed_paragraphs"]] == [1]
        source = next(item for item in payload["sources"] if item["kind"] == "tool_result")
        reference = source["excerpts"][0]["evidence_id"] if correct_reference else "invented:excerpt_0001"
        return json.dumps({"answer_complete": True, "paragraph_corrections": [{"index": 1, "paragraph": {
            "text": measured, "kind": "analysis", "evidence": [reference]}}], "requirement_corrections": []})

    ask = AsyncMock(side_effect=respond)
    real_reviewer(agent, ask)
    checkpoint = AsyncMock(side_effect=AssertionError("Citation repair cannot checkpoint or replay execution"))
    monkeypatch.setattr("app.domain.services.analysis_checkpoint.save_checkpoint", checkpoint)

    events = await collect(runner, message)

    assert len(state["prompts"]) == state["drained"] == 1 and ask.await_count == 2
    assert step.success is correct_reference
    assert step.outcome.status == ("succeeded" if correct_reference else "partial")
    assert delivered in step.result and (measured in step.result) is correct_reference
    assert step.outputs["answer_review"]["citation_repair_attempted"] is True
    assert not step.outcome.can_resume and not runner._flow._artifact_repair_requests
    assert all(item._artifact_repair_context is None for item in state["messages"])
    assert step.attachments == [chart[0]["path"]]
    assert chart[1].file_id in {item.file_id for event in terminal_messages(events) for item in event.attachments or []}
    assert FALSE_DRAFT not in terminal_payload(events) and PRIVATE_SENTINEL not in terminal_payload(events)
    checkpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_analytical_objective_can_repair_without_adopting_phantom_draft_obligation():
    first = output("initial.png", "image")
    completed = output("requested-comparison.png", "image", digest="b")
    requirement = {"kind": "image", "objective": "Compare class distributions"}
    runner, flow, step, message, state = scenario([[first], [first, completed]], [requirement])
    agent = with_observed_results(runner, flow, [FALSE_DRAFT, "The requested comparison was generated."])
    agent.review_delivery_answer = AsyncMock(side_effect=[
        AnswerReviewResult("已保留初步图表，要求的分组比较尚未执行。", "corrected", {"source_count": 2}, (0,)),
        AnswerReviewResult("已完成要求的分组比较。", "verified", {"source_count": 4}),
    ])
    events = await collect(runner, message)
    assert len(state["prompts"]) == state["drained"] == 2
    assert step.success
    assert agent.review_delivery_answer.await_count == 2
    assert state["messages"][1]._artifact_repair_context["constraints"]["replay_original_step"] is False
    feedback = state["messages"][1]._artifact_repair_context
    assert feedback["requirements"][0]["objective"] == requirement["objective"]
    assert feedback["missing"][0]["objective"] == requirement["objective"]
    assert "PCA" not in json.dumps(feedback)
    assert "projection.png" not in json.dumps(feedback)
    assert FALSE_DRAFT not in terminal_payload(events)
    assert all(item.objective == requirement["objective"] for item in step.deliverables)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_key", ["domain_tabular", "domain_geoscience", "configured_execution_alias"])
async def test_grounding_uses_resolved_domain_executor_without_broader_executor_fallback(agent_key):
    chart = output("domain-output.png", "image")
    runner, flow, step, message, state = scenario([[chart]], [{"kind": "image"}], agent_key=agent_key)
    agent = with_observed_results(runner, flow, [FALSE_DRAFT], agent_key=agent_key)
    agent.review_delivery_answer = AsyncMock(return_value=AnswerReviewResult(
        "领域分析产物已生成。", "corrected", {"source_count": 2}))
    flow.executor.review_delivery_answer = AsyncMock(side_effect=AssertionError("Do not cross domain scope"))
    events = await collect(runner, message)
    assert len(state["prompts"]) == 1 and step.success
    agent.review_delivery_answer.assert_awaited_once()
    flow.executor.review_delivery_answer.assert_not_awaited()
    assert FALSE_DRAFT not in terminal_payload(events)


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
