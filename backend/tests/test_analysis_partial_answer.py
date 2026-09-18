"""Preserve grounded explanations when a real user request is only partly fulfilled."""
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import MessageEvent, ToolEvent, ToolStatus
from app.domain.services.analysis_answer_review import review_answer
from test_analysis_repair_flow import collect, output, scenario, terminal_messages

FACT = "value count=6 mean=7 min=2 max=12; missing_measurement column is absent, so its output was not generated."
CHECKED = "value 列已完成统计：count=6、mean=7、min=2、max=12。missing_measurement 列不存在，因此未生成该列的统计文件。"
DRAFT = "WRONG_MEAN_999：所有列已完成；缺失文件为 /home/ubuntu/output/private-repair-dir/missing_summary.csv。"


def partial_scenario(*, available=True, unknown=False, changed_protected=False):
    value = output("value_summary.csv", "table")
    missing = output("missing_summary.csv", "table", valid=False, reason="missing_artifact", uploaded=False)
    missing[0].update(sha256=None, size=None)
    values = [value, missing] if available else [missing]
    requirements = ([{"kind": "table", "formats": ["csv"], "output_paths": [value[0]["path"]],
                     "objective": "Compute the observed value summary"}] if available else [])
    requirements.append({"kind": "table", "formats": ["csv"], "output_paths": [missing[0]["path"]],
                         "objective": "Compute missing_measurement summary if the column exists"})
    repaired = [output("value_summary.csv", "table", digest="b"), missing] if changed_protected else values
    if changed_protected == "deleted":
        deleted = output("value_summary.csv", "table", valid=False, reason="missing_artifact", uploaded=False)
        deleted[0].update(sha256=None, size=None)
        repaired = [deleted, missing]
    runner, flow, step, message, state = scenario([values, repaired], requirements, unknown=unknown)
    original = flow.executor._execute_with_tool_scope
    runner._handle_tool_event = AsyncMock()

    async def execute(prompt, **kwargs):
        async for event in original(prompt, **kwargs):
            if isinstance(event, MessageEvent):
                if len(state["prompts"]) == 1:
                    yield ToolEvent(tool_call_id="actual-analysis", tool_name="program", function_name="program_run",
                        function_args={"script_path": "/home/ubuntu/analyze.py"}, status=ToolStatus.CALLED,
                        function_result={"success": True, "data": {"returncode": 0, "output": FACT}})
                elif changed_protected:
                    yield ToolEvent(tool_call_id="changed-analysis", tool_name="program", function_name="program_run",
                        function_args={"script_path": "/home/ubuntu/changed.py"}, status=ToolStatus.CALLED,
                        function_result={"success": True, "data": {"returncode": 0, "output": "CHANGED_FACT: value mean=999"}})
                payload = json.loads(event.message)
                payload["result"] = DRAFT
                event = event.model_copy(update={"message": json.dumps(payload)})
            yield event

    flow.executor._execute_with_tool_scope = execute
    return runner, flow, step, message, state


def ground_partial_review(flow, *, unavailable=False, checks_met=False):
    requests = []

    async def ask(messages):
        if unavailable:
            raise TimeoutError("provider unavailable")
        payload = json.loads(messages[-1].content)
        requests.append(payload)
        source = next(item for item in payload["sources"] if item["kind"] == "tool_result" and FACT in item["text"])
        quote = {"source_id": source["source_id"], "quote": FACT}
        return json.dumps({"unsupported_claims": True,
            "paragraphs": [{"kind": "analysis", "text": CHECKED, "evidence": [quote]}],
            "requirement_checks": [{"index": item["index"],
                "status": "confirmed_not_performed" if "missing_measurement" in item["objective"] and not checks_met else "met",
                "evidence": [quote]} for item in payload["requirements"]]})

    async def review(**arguments):
        return await review_answer(ask=ask, **arguments)

    flow.executor.review_delivery_answer = AsyncMock(side_effect=review)
    return requests


@pytest.mark.asyncio
async def test_missing_column_keeps_measured_statistics_and_explains_uncreated_required_file():
    runner, flow, step, message, state = partial_scenario()
    requests = ground_partial_review(flow)
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2  # No further computation for final-answer repair.
    assert len(requests) == 1 and flow.executor.review_delivery_answer.await_count == 1
    assert step.outcome.status == "partial" and step.outcome.reason_code == "artifacts_missing"
    assert [item.output_paths[0].rsplit("/", 1)[-1] for item in step.outcome.missing] == ["missing_summary.csv"]
    assert CHECKED in step.result
    assert "`missing_summary.csv`" in step.result
    assert step.result.startswith("本次分析部分完成。")
    assert "WRONG_MEAN_999" not in step.result
    assert not step.outcome.can_resume
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert {info.filename for info in delivered} == {"value_summary.csv"}
    assert "missing_summary.csv" not in next(item["text"] for item in requests[0]["sources"]
                                            if item["source_id"] == "verified_files")


@pytest.mark.asyncio
@pytest.mark.parametrize("checks_met", [False, True])
async def test_partial_review_can_never_erase_the_host_missing_file_verdict(checks_met):
    runner, flow, step, message, _ = partial_scenario()
    ground_partial_review(flow, checks_met=checks_met)
    await collect(runner, message)
    assert not step.success and step.outcome.status == "partial"
    assert step.outcome.reason_code == "artifacts_missing" and len(step.outcome.missing) == 1
    assert CHECKED in step.result


@pytest.mark.asyncio
async def test_unavailable_partial_review_keeps_original_reason_files_and_safe_missing_filename():
    runner, flow, step, message, _ = partial_scenario()
    ground_partial_review(flow, unavailable=True)
    await collect(runner, message)
    flow.executor.review_delivery_answer.assert_awaited_once()
    assert step.outcome.status == "partial" and step.outcome.reason_code == "artifacts_missing"
    assert "WRONG_MEAN_999" not in step.result and "`missing_summary.csv`" in step.result
    assert len(runner._generated_files) == 1 and not step.outcome.can_resume


@pytest.mark.asyncio
async def test_unknown_operation_does_not_gain_completion_or_prose_from_partial_review():
    runner, flow, step, message, _ = partial_scenario(unknown=True)
    ground_partial_review(flow)
    await collect(runner, message)
    assert step.outcome.reason_code == "tool_execution_unknown" and not step.success
    flow.executor.review_delivery_answer.assert_not_awaited()
    assert "WRONG_MEAN_999" not in step.result


@pytest.mark.asyncio
async def test_no_deliverable_failure_retains_verified_input_limitation_without_becoming_partial():
    runner, flow, step, message, _ = partial_scenario(available=False)
    ground_partial_review(flow)
    await collect(runner, message)
    assert not step.success and step.outcome.status == "failed"
    assert "missing_measurement 列不存在" in step.result
    assert "`missing_summary.csv`" in step.result
    assert not runner._generated_files


@pytest.mark.asyncio
async def test_absent_terminal_tool_evidence_never_promotes_a_bare_partial_draft():
    value = output("value_summary.csv", "table")
    missing = output("missing_summary.csv", "table", valid=False, reason="missing_artifact", uploaded=False)
    missing[0].update(sha256=None, size=None)
    runner, flow, step, message, _ = scenario([[value, missing], [value, missing]], [
        {"kind": "table", "min_count": 2}])
    ground_partial_review(flow)
    await collect(runner, message)
    flow.executor.review_delivery_answer.assert_not_awaited()
    assert step.outcome.status == "partial" and "Measured findings" not in step.result


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_protected", [True, "deleted"])
async def test_protected_file_change_keeps_original_facts_without_reviewing_replacement_evidence(changed_protected):
    runner, flow, step, message, state = partial_scenario(changed_protected=changed_protected)
    requests = ground_partial_review(flow, checks_met=True)
    events = await collect(runner, message)
    assert len(state["prompts"]) == 2
    assert len(requests) == 1 and flow.executor.review_delivery_answer.await_count == 1
    assert not step.success and step.outcome.status == "partial"
    assert step.outcome.reason_code == "execution_failed" and len(step.outcome.missing) == 1
    assert CHECKED in step.result and "`missing_summary.csv`" in step.result
    assert "已保留此前核验通过的版本" in step.result
    assert "WRONG_MEAN_999" not in step.result and "CHANGED_FACT" not in step.result
    assert not step.outcome.can_resume
    assert all("CHANGED_FACT" not in source["text"] for source in requests[0]["sources"])
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert len(delivered) == 1 and delivered[0].metadata["artifact_sha256"] == "a" * 64
    assert step.outputs["answer_review"]["partial_delivery_review"] is True
    assert step.outputs["answer_review"]["preserved_evidence_review"] is True


@pytest.mark.asyncio
async def test_protected_file_change_cannot_use_current_evidence_when_original_snapshot_is_unavailable(monkeypatch):
    from app.domain.services.analysis_answer_review import AnswerEvidence
    def unavailable_snapshot(*_args, **_kwargs):
        raise ValueError("invalid_answer_evidence_checkpoint")
    monkeypatch.setattr(AnswerEvidence, "checkpoint_snapshot", unavailable_snapshot)
    runner, flow, step, message, _ = partial_scenario(changed_protected=True)
    ground_partial_review(flow)
    events = await collect(runner, message)
    flow.executor.review_delivery_answer.assert_not_awaited()
    assert step.outcome.status == "partial" and step.outcome.reason_code == "execution_failed"
    assert CHECKED not in step.result and "WRONG_MEAN_999" not in step.result
    assert "已保留此前核验通过的版本" in step.result
    delivered = [info for event in terminal_messages(events) for info in event.attachments or []]
    assert len(delivered) == 1 and delivered[0].metadata["artifact_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_protected_file_change_review_outage_keeps_original_delivery_and_failure():
    runner, flow, step, message, _ = partial_scenario(changed_protected=True)
    ground_partial_review(flow, unavailable=True)
    await collect(runner, message)
    flow.executor.review_delivery_answer.assert_awaited_once()
    assert step.outcome.status == "partial" and step.outcome.reason_code == "execution_failed"
    assert len(step.outcome.missing) == 1 and "`missing_summary.csv`" in step.result
    assert "WRONG_MEAN_999" not in step.result and "CHANGED_FACT" not in step.result
    assert "已保留此前核验通过的版本" in step.result
    assert len(runner._generated_files) == 1 and not step.outcome.can_resume
