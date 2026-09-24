import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import ErrorEvent, MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, ToolEvent, ToolStatus
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services import completion_advice_service as module


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    monkeypatch.setattr(module, "get_settings", lambda: object())


def analysis_events(status):
    return [
        MessageEvent(role="user", message="比较数据并生成图表"),
        ToolEvent(tool_call_id="read", tool_name="file", function_name="file_read", function_args={}, status=ToolStatus.CALLED),
        ToolEvent(tool_call_id="run", tool_name="program", function_name="program_run", function_args={}, status=ToolStatus.CALLED),
        StepEvent(status=StepStatus.COMPLETED if status == "succeeded" else StepStatus.FAILED,
                  step=Step(id="analysis", success=status == "succeeded", attachments=["structure.pdb"],
                            outcome=AnalysisOutcome(status=status, reason_code="analysis_completed" if status == "succeeded" else "answer_validation_rejected"))),
    ]


@pytest.mark.parametrize("status", ["failed", "partial"])
@pytest.mark.asyncio
async def test_rejected_analysis_keeps_preview_but_never_promotes_conclusion_or_workflow(monkeypatch, status):
    def unexpected_model(*args, **kwargs):
        raise AssertionError("Incomplete analysis advice must be deterministic and model-free")
    monkeypatch.setattr(module, "create_chat_model", unexpected_model)
    service = module.CompletionAdviceService()
    events = analysis_events(status)
    for advice in [service.analyze_fast(events), await service.analyze(events)]:
        assert not advice.is_skill_candidate
        assert advice.molecular_preview_available
        assert advice.recommendations == ["列出本次已完成和未完成的内容", "说明哪些结论还缺少证据", "列出继续分析需要补充的资料"]


def test_previous_failed_turn_cannot_replace_current_successful_advice():
    service = module.CompletionAdviceService()
    previous = analysis_events("failed")
    previous.append(ErrorEvent(error="old failure"))
    current = analysis_events("succeeded")
    advice = service.analyze_fast([*previous, *current])
    assert advice == service.analyze_fast(current)
    assert advice.is_skill_candidate


def test_latest_step_terminal_replaces_earlier_state():
    service = module.CompletionAdviceService()
    events = analysis_events("failed")
    events.append(analysis_events("succeeded")[-1])
    assert service.analyze_fast(events).is_skill_candidate
    events.append(analysis_events("partial")[-1])
    assert not service.analyze_fast(events).is_skill_candidate


def test_legacy_failure_without_outcome_and_terminal_error_get_recovery_advice():
    service = module.CompletionAdviceService()
    cases = [
        [MessageEvent(role="user", message="分析数据"), StepEvent(status=StepStatus.FAILED, step=Step(id="legacy"))],
        [ErrorEvent(error="invalid_execution_result")],
    ]
    for events in cases:
        assert service.analyze_fast(events).recommendations[0] == "列出本次已完成和未完成的内容"


def test_preview_does_not_leak_from_a_previous_turn():
    service = module.CompletionAdviceService()
    events = [*analysis_events("succeeded"), MessageEvent(role="user", message="另一个问题"), ErrorEvent(error="failed")]
    assert not service.analyze_fast(events).molecular_preview_available


def test_final_plan_failure_without_failed_step_event_prevents_workflow_advice():
    service = module.CompletionAdviceService()
    events = analysis_events("succeeded")
    events.append(PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(status=ExecutionStatus.FAILED, steps=[
        Step(id="analysis", status=ExecutionStatus.COMPLETED, success=True),
        Step(id="unexecuted", status=ExecutionStatus.FAILED, success=False),
    ])))
    assert not service.analyze_fast(events).is_skill_candidate
    # Later complete plan is authoritative, not a union of obsolete failures.
    events.append(PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(status=ExecutionStatus.COMPLETED, steps=[
        Step(id="analysis", status=ExecutionStatus.COMPLETED, success=True),
        Step(id="unexecuted", status=ExecutionStatus.COMPLETED, success=True),
    ])))
    assert service.analyze_fast(events).is_skill_candidate
