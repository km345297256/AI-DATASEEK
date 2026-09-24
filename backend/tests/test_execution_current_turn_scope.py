"""Current-turn scope reaches real execution entry points without another gate.

The model loop is an offline result double: these tests verify delivered context
and host control flow, not that a particular model obeys natural-language rules.
"""
import json
from collections import Counter
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.dataset import DatasetFile, MountedDataset
from app.domain.models.event import MessageEvent, StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.prompts.execution import (
    CUSTOM_PARSER_EXECUTION_POLICY,
    EXECUTION_PROMPT,
    SCIENTIFIC_COUNTING_POLICY,
)


def structured(request, tag):
    return json.loads(request.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])


def fixture_agent(*, expected_request, attachments=(), result="The requested response is ready."):
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()
    agent.invoke_tool = AsyncMock(side_effect=AssertionError("The host must not add unrequested tool work"))
    captured = []

    async def execute(request):
        captured.append(request)
        boundary = structured(request, "current_turn_scope")
        assert boundary["current_user_request"] == expected_request
        assert boundary["scope_authority"] == "current_user_request"
        yield MessageEvent(message=json.dumps({"success": True, "result": result,
                                              "attachments": list(attachments)}))

    agent.execute = execute
    return agent, captured


def dataset():
    return MountedDataset(dataset_id="synthetic", name="Synthetic records", data_center_id="local",
        data_center_name="Local", sandbox_path="/home/ubuntu/datasets/synthetic",
        files=[DatasetFile(path="records.dat", size=64, content_type="text/plain")])


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["registered", "upload", "general"])
@pytest.mark.parametrize("user_text", [
    "先核对数据字段和缺失情况，把能否预测目标值列为后续研究问题。本轮不训练模型或出图。",
    "Confirm which variables could support a later comparison; do not perform that comparison now.",
    "只解释上一轮的结果，机制检验作为后续课题。",
])
async def test_narrow_request_and_no_artifact_intent_reach_execution_despite_broader_plan_hints(source, user_text):
    message = Message(message=user_text, controller_requires_artifacts=False,
        datasets=[dataset()] if source == "registered" else [],
        attachments=["/home/ubuntu/input/records.dat"] if source == "upload" else [])
    earlier = Step(id="earlier", description="Earlier work", status=ExecutionStatus.COMPLETED, success=True,
        result="Previously inspected observations.", attachments=["/home/ubuntu/output/previous.png"])
    step = Step(id="current", description="Long-term study: compare models and prepare visualizations", inputs={
        "execution_mode": "dataset_fast_path" if source == "registered" else "general",
        "dataset_intent": "analysis", "artifact_policy": "required",
        "user_question": "A broader historical project objective", "requested_dimensions": ["comparison", "visualization"]})
    plan = Plan(language="zh", goal="Explore possible models and plots", steps=[earlier, step])
    agent, captured = fixture_agent(expected_request=user_text)
    events = [event async for event in agent.execute_step(plan, step, message)]
    assert len(captured) == 1 and step.status == ExecutionStatus.COMPLETED and step.success
    assert step.attachments == [] and step.deliverables == []
    assert any(isinstance(event, StepEvent) and event.status == StepStatus.COMPLETED for event in events)
    scope = structured(captured[0], "current_turn_scope")
    assert scope["requires_artifact_delivery"] is False and scope["requested_deliverables"] == []
    assert scope["current_step_id"] == "current" and scope["host_authorized_continuation"] is False
    # Retain useful continuity; do not erase the old result or pretend it is a
    # current request. It lives in a separate structured workflow block.
    prior = captured[0].split("<execution_step_context>", 1)[1].split("</execution_step_context>", 1)[0]
    assert "Previously inspected observations." in prior
    assert "not independent authorization" in prior
    assert "The following plan state is authoritative" not in prior
    assert CUSTOM_PARSER_EXECUTION_POLICY in captured[0]
    assert captured[0].count(SCIENTIFIC_COUNTING_POLICY) == 1
    assert "mandatory coverage checklist" not in captured[0]
    agent.reset_context.assert_awaited_once()
    agent.invoke_tool.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_current_calculation_and_chart_requests_are_preserved_without_extra_approval():
    request = "Compute the requested comparison now and return a chart plus the summary table."
    requested = [DeliverableRequirement(kind="image"), DeliverableRequirement(kind="table")]
    message = Message(message=request, controller_requires_artifacts=True, deliverables=requested)
    step = Step(id="current", description="Perform requested comparison", deliverables=requested,
                inputs={"dataset_intent": "analysis", "artifact_policy": "required"})
    paths = ["/home/ubuntu/output/current.png", "/home/ubuntu/output/current.csv"]
    agent, captured = fixture_agent(expected_request=request, attachments=paths)
    events = [event async for event in agent.execute_step(Plan(language="en", steps=[step]), step, message)]
    assert len(captured) == 1 and step.success and step.status == ExecutionStatus.COMPLETED
    scope = structured(captured[0], "current_turn_scope")
    assert scope["requires_artifact_delivery"] is True
    assert [item["kind"] for item in scope["requested_deliverables"]] == ["image", "table"]
    assert step.attachments == paths and step.deliverables == requested
    assert not any(getattr(event, "event", None) == "wait" for event in events)
    agent.invoke_tool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True, None])
async def test_custom_fallback_keeps_same_current_scope_and_optional_validation_policy(flag):
    request = "Check required columns and report the data-quality counts for this input."
    message = Message(message=request, datasets=[dataset()], controller_requires_artifacts=flag)
    current = Step(id="fallback", description="Prepare a broader study", status=ExecutionStatus.RUNNING,
        inputs={"user_question": "Old goal", "artifact_policy": "optional", "target_files": ["records.dat"]})
    agent, captured = fixture_agent(expected_request=request)
    agent._current_plan = Plan(language="en", goal="A larger research program", steps=[current])
    agent._dataset_fast_path_mode = False
    agent._dataset_intent = "existing-mode"
    events = [event async for event in agent._execute_dataset_general_analysis("Inspect the available records.",
        message=message, target_files=["records.dat"], dataset_intent="analysis", artifact_policy="optional")]
    assert len(events) == len(captured) == 1
    scope = structured(captured[0], "current_turn_scope")
    assert scope["requires_artifact_delivery"] is flag and scope["current_step_id"] == "fallback"
    assert captured[0].count(CUSTOM_PARSER_EXECUTION_POLICY) == 1
    assert captured[0].count(SCIENTIFIC_COUNTING_POLICY) == 1
    registered = structured(captured[0], "registered_fallback_scope")
    assert [item["path"] for item in registered["datasets"][0]["files"]] == ["records.dat"]
    assert registered["datasets"][0]["scope_restricted_to_targets"] is True
    assert agent._dataset_fast_path_mode is False and agent._dataset_intent == "existing-mode"
    assert current.inputs["user_question"] == "Old goal", "Rendering context must not rewrite persisted routing inputs"
    agent.invoke_tool.assert_not_called()


@pytest.mark.parametrize("kind", ["resume", "artifact_repair"])
def test_continuation_is_explicit_private_context_not_inferred_from_user_words(kind):
    message = Message(message="Continue only the unfinished part.")
    step = Step(id="unfinished")
    initial = structured(ExecutionAgent._render_current_turn_scope(step, message), "current_turn_scope")
    assert initial["host_authorized_continuation"] is False
    if kind == "resume": message._resume_checkpoint = {"progress": {"last_step": "unfinished"}}
    else: message._artifact_repair_context = {"missing": [{"kind": "table"}]}
    rendered = ExecutionAgent._render_current_turn_scope(step, message)
    assert structured(rendered, "current_turn_scope")["host_authorized_continuation"] is True
    assert "original unfinished work" in rendered and "do not broaden" in rendered
    assert "host_authorized_continuation" not in message.model_dump()


def test_scope_projection_is_bounded_and_uses_the_latest_request_not_router_or_plan_text():
    request = "本轮范围" * 2000
    message = Message(message=request, controller_requires_artifacts=False)
    step = Step(id="narrow", inputs={"user_question": "Stale broader request"})
    scope = structured(ExecutionAgent._render_current_turn_scope(step, message), "current_turn_scope")
    assert len(scope["current_user_request"].encode()) <= ExecutionAgent.MAX_STEP_RESULT_BYTES
    assert scope["current_user_request"].startswith("本轮范围") and "truncated" in scope["current_user_request"]
    assert "Stale broader request" not in scope["current_user_request"]
    assert message.message == request and step.inputs["user_question"] == "Stale broader request"


def test_single_pass_and_two_phase_validation_are_distinguished_without_flag_based_guarantees():
    rendered = EXECUTION_PROMPT.format(step="Inspect", message="Check the schema", attachments="", language="en", dataset_contract="")
    assert rendered.count(CUSTOM_PARSER_EXECUTION_POLICY) == 1
    assert "one program_run invocation" in rendered
    assert "checks may run first within the same invocation" in rendered
    assert "A command-line label such as --validate-only is not execution isolation" in rendered
    assert "then return BEFORE any analysis-only branch, plotting, exporting, or writing deliverables" in rendered
    assert "If that branch does not exist, do not pass the flag" in rendered
    assert "Keep a reusable parser and a validation-only entry point" not in rendered
    assert "run that small check with `program_run` before the full analysis" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["registered", "upload", "general"])
async def test_formulating_research_question_is_a_complete_text_result_not_an_instruction_to_run_it(source):
    request = "核对测量字段，将两种测量方式的稳定性差异定义为研究问题，并提出检验思路。"
    answer = "研究问题：两种测量方式的稳定性是否不同？检验思路：先明确重复测量结构，再选择相应的比较方法。"
    message = Message(message=request, controller_requires_artifacts=False,
        datasets=[dataset()] if source == "registered" else [],
        attachments=["/home/ubuntu/input/records.dat"] if source == "upload" else [])
    step = Step(id="formulation", description="Research methods", inputs={
        "execution_mode": "dataset_fast_path" if source == "registered" else "general",
        "dataset_intent": "analysis", "artifact_policy": "optional"})
    agent, captured = fixture_agent(expected_request=request, result=answer)
    events = [event async for event in agent.execute_step(Plan(language="zh", steps=[step]), step, message)]
    assert step.success and step.status == ExecutionStatus.COMPLETED and step.result == answer
    assert step.attachments == [] and step.deliverables == []
    assert len(captured) == 1 and not any(getattr(event, "event", None) == "wait" for event in events)
    assert "that text is itself the final result" in captured[0]
    assert "a heuristic to answer that proposed question is downstream" in captured[0]
    assert "Deliver the final result to user not the todo list, advice or plan" not in captured[0]
    if source == "registered":
        assert "Only when the current request asks for analysis/export" in captured[0]
        assert "Prefer one analysis/export run after input validation" not in captured[0]
    agent.invoke_tool.assert_not_called()


def duplicate_evidence(records, equality_fields):
    """Independent stdlib oracle for a synthetic, explicitly scoped example.

    This constructs observed evidence, not a runtime implementation that could
    confer trust on model-authored counts. The execution double below verifies
    context/result handling; live model adherence is a separate acceptance test.
    """
    keys = [tuple(row[field] for field in equality_fields) for row in records]
    sizes = Counter(keys)
    groups = [{"group_size": size,
               "source_row_ids": [index for index, actual in enumerate(keys, 1) if actual == key]}
              for key, size in sizes.items() if size > 1]
    extra = sum(group["group_size"] - 1 for group in groups)
    return {
        "statistical_unit": "input rows",
        "inspected_row_count": len(records),
        "equality_fields": list(equality_fields),
        "missing_value_policy": "None equals None; no approximate comparison or normalization",
        "row_numbering": "one-based input order",
        "duplicate_extra_rows": extra,
        "duplicate_member_rows": sum(group["group_size"] for group in groups),
        "duplicate_group_count": len(groups),
        "extra_row_rate": {"numerator": extra, "denominator": len(records)},
        "groups": groups,
        "group_examples_truncated": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [list(range(7)), [6, 4, 2, 0, 5, 3, 1]])
@pytest.mark.parametrize("equality_fields", [("measurement", "group"), ("group", "measurement")])
async def test_quality_count_example_distinguishes_triplicate_pair_and_members_without_transforming_input(
    order, equality_fields,
):
    # Two repeated-value groups of unequal sizes make all three counting units
    # distinguishable. No fixed dataset, species, filename or known benchmark.
    generated = [{"measurement": value, "group": group}
                 for value, group, count in [(2.25, "alpha", 3), (7.5, "beta", 2),
                                              (12.0, "alpha", 1), (None, "gamma", 1)]
                 for _ in range(count)]
    records = [generated[index] for index in order]
    original = deepcopy(records)
    evidence = duplicate_evidence(records, equality_fields)
    assert evidence["duplicate_extra_rows"] == 3
    assert evidence["duplicate_member_rows"] == 5
    assert evidence["duplicate_group_count"] == 2
    assert evidence["duplicate_member_rows"] == evidence["duplicate_extra_rows"] + evidence["duplicate_group_count"]
    assert evidence["extra_row_rate"] == {"numerator": 3, "denominator": 7}
    assert sorted(group["group_size"] for group in evidence["groups"]) == [2, 3]
    for group in evidence["groups"]:
        assert len(group["source_row_ids"]) == group["group_size"]
        observed = [records[row_id - 1] for row_id in group["source_row_ids"]]
        assert all(row == observed[0] for row in observed)
    assert records == original, "Marking duplicates must not delete, merge, reorder, or normalize observations"
    request = "核查记录和重复情况，标明计数口径与重复行位置，不删除或合并原始观测。"
    message = Message(message=request, datasets=[dataset()], controller_requires_artifacts=False)
    step = Step(id="quality", inputs={"execution_mode": "dataset_fast_path", "dataset_intent": "analysis",
                                     "artifact_policy": "optional"})
    agent, captured = fixture_agent(expected_request=request, result=json.dumps(evidence, ensure_ascii=False))
    _ = [event async for event in agent.execute_step(Plan(language="zh", steps=[step]), step, message)]
    assert step.success and json.loads(step.result) == evidence and step.attachments == []
    assert captured[0].count(SCIENTIFIC_COUNTING_POLICY) == 1
    assert all(name in captured[0] for name in (
        "duplicate_extra_rows", "duplicate_member_rows", "duplicate_group_count", "denominator",
        "source row identifiers", "preserve multiplicities"))
    agent.invoke_tool.assert_not_called()
