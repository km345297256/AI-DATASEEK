"""Terminal recovery is explicit, durable, and does not replay work."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from langchain_core.messages import AIMessage

import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import MessageEvent, DoneEvent, ErrorEvent
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.models.session import SessionStatus
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.analysis_answer_review import AnswerReviewResult
from app.domain.services.input_delivery import InputLeaseLost
from app.domain.services.tools.analysis_job import AnalysisJobCancelled, AnalysisJobInterrupted
from app.domain.utils.robust_json_parser import ToolCallParseError
from test_input_delivery import MemoryInputs


def fixture_runner(error, *, existing_verdict=None, before_analysis=False, step_result=False):
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = "agent", "session", "user"
    repository = MemoryInputs()
    repository.increment_unread_message_count = AsyncMock()
    runner._session_repository = repository
    runner._sandbox = SimpleNamespace(ensure_api_ready=AsyncMock())
    runner._dataset_service = object()
    runner._protected_dataset_roots = set()
    runner._remember_mounted_dataset_paths = Mock()
    runner._capture_artifact_baseline = AsyncMock()
    runner._sync_message_attachments_to_sandbox = AsyncMock()
    # No sandbox content enters this test. Exercise the real durable publisher.
    runner._durable_event_projection = lambda event: event
    runner._bound_event_payload = lambda event: event
    if before_analysis:
        runner._capture_artifact_baseline.side_effect = error
    calls = []

    async def flow(*args, **kwargs):
        calls.append("started")
        runner._analysis_turn_started = True
        if existing_verdict:
            yield MessageEvent(message="prior verified result", metadata={"analysis_outcome": existing_verdict,
                **({"step_id": "first-step", "artifact_delivery": True} if step_result else {})})
        raise error
        yield  # Keep this an asynchronous generator without executing I/O.

    runner._run_flow = flow
    event = MessageEvent(id="input", seq=1, role="user", message="synthetic analysis")
    task = SimpleNamespace(
        pop_input_or_close=AsyncMock(side_effect=[("input", event.model_dump_json()), (None, None)]),
        output_stream=SimpleNamespace(put=AsyncMock(return_value="transport-id")))
    return runner, task, repository, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason,terminal", [
    (AnalysisJobInterrupted(), "execution_interrupted", DoneEvent),
    (AnalysisJobCancelled(), "request_cancelled", DoneEvent),
    (asyncio.CancelledError(), "execution_interrupted", DoneEvent),
    (RuntimeError("synthetic private detail"), "execution_failed", ErrorEvent),
])
async def test_worker_terminal_has_durable_outcome_before_sse_terminal_without_replay(error, reason, terminal):
    runner, task, repository, calls = fixture_runner(error)
    await runner._run_with_model_budget(task)
    assert calls == ["started"]
    assert len(repository.events) == 2
    result = repository.events[0]
    assert isinstance(result, MessageEvent) and isinstance(repository.events[1], terminal)
    assert result.metadata["analysis_outcome"]["reason_code"] == reason
    assert result.metadata["analysis_outcome"]["status"] == "failed"
    assert result.metadata["execution_stage"] == "execution" and result.metadata["analysis_started"] is True
    assert "synthetic private detail" not in result.model_dump_json()
    assert repository.session.status == SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_pre_analysis_error_does_not_claim_analysis_started():
    runner, task, repository, calls = fixture_runner(RuntimeError("preparation"), before_analysis=True)
    await runner._run_with_model_budget(task)
    assert calls == []
    assert repository.events[0].metadata["analysis_started"] is False
    assert repository.events[0].metadata["execution_stage"] == "input_preparation"


@pytest.mark.asyncio
async def test_lease_loser_does_not_publish_a_competing_terminal():
    runner, task, repository, calls = fixture_runner(InputLeaseLost())
    await runner._run_with_model_budget(task)
    assert calls == ["started"] and repository.events == []


@pytest.mark.asyncio
async def test_cleanup_cancellation_cannot_overwrite_an_already_committed_verdict():
    verdict = AnalysisOutcome(status="partial", reason_code="artifacts_missing").model_dump()
    runner, task, repository, calls = fixture_runner(AnalysisJobInterrupted(), existing_verdict=verdict)
    await runner._run_with_model_budget(task)
    outcomes = [event.metadata["analysis_outcome"] for event in repository.events
                if isinstance(event, MessageEvent) and (event.metadata or {}).get("analysis_outcome")]
    assert outcomes == [verdict] and calls == ["started"]
    assert isinstance(repository.events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_first_successful_step_does_not_hide_a_later_worker_interruption():
    verdict = AnalysisOutcome(status="succeeded", reason_code="completed").model_dump()
    runner, task, repository, calls = fixture_runner(AnalysisJobInterrupted(), existing_verdict=verdict, step_result=True)
    await runner._run_with_model_budget(task)
    assert repository.events[0].metadata["analysis_outcome"] == verdict
    final = repository.events[1].metadata["analysis_outcome"]
    assert final["status"] == "partial" and final["reason_code"] == "execution_interrupted"
    assert calls == ["started"] and isinstance(repository.events[-1], DoneEvent)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["none", "step", "final"])
async def test_exhausted_native_arguments_have_safe_durable_protocol_outcome(prior):
    error = ToolCallParseError(
        message="private provider excerpt /Users/example/secret sk-secret-value",
        invalid_message=AIMessage(content="private invalid model response"),
        error_details=["private parser detail"],
    )
    verdict = AnalysisOutcome(status="succeeded", reason_code="completed").model_dump()
    runner, task, repository, calls = fixture_runner(
        error, existing_verdict=verdict if prior != "none" else None, step_result=prior == "step")
    await runner._run_with_model_budget(task)
    outcomes = [event.metadata["analysis_outcome"] for event in repository.events
                if isinstance(event, MessageEvent) and (event.metadata or {}).get("analysis_outcome")]
    if prior == "final":
        assert outcomes == [verdict]
    else:
        assert outcomes[-1]["reason_code"] == "tool_protocol_error"
        assert outcomes[-1]["status"] == ("partial" if prior == "step" else "failed")
        assert outcomes[-1]["can_resume"] is False
    assert isinstance(repository.events[-1], ErrorEvent)
    assert "完整、有效的工具参数" in repository.events[-1].error
    public = " ".join(event.model_dump_json() for event in repository.events)
    assert all(value not in public for value in ("private", "/Users", "sk-secret-value", "Task error"))
    assert repository.session.status == SessionStatus.COMPLETED
    assert calls == ["started"]


@pytest.mark.parametrize("reason", ["answer_validation_rejected", "answer_objectives_missing"])
def test_review_reason_message_does_not_invent_preserved_files(reason):
    from app.domain.services.analysis_completion import outcome_message
    text = outcome_message(AnalysisOutcome(status="failed", reason_code=reason), delivered_files=[])
    assert "具体原因暂未确认" not in text
    assert "已交付" not in text and "保留" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata,expected", [
    ({"validation_state": "rejected", "reason": "invalid_citations"}, "answer_validation_rejected"),
    ({"validation_state": "rejected", "reason": "answer_coverage_incomplete"}, "answer_objectives_missing"),
    ({"validation_state": "unavailable", "reason": "answer_coverage_unverified"}, "answer_validation_unavailable"),
    ({"validation_state": "unavailable", "reason": "provider_timeout"}, "answer_validation_unavailable"),
])
async def test_review_failure_reason_is_distinct_without_adding_repair_obligations(metadata, expected):
    runner = object.__new__(AgentTaskRunner)
    reviewed = AnswerReviewResult(status="unavailable", text="safe review result", metadata=metadata)
    reviewer = AsyncMock(return_value=reviewed)
    runner._flow = SimpleNamespace(executor=SimpleNamespace(review_delivery_answer=reviewer))
    step = Step(id="step", description="synthetic objective")
    outcome = AnalysisOutcome(status="succeeded", reason_code="completed")
    result = await runner._review_analysis_answer(step, Message(message="synthetic request"), [], [], outcome)
    assert outcome.reason_code == expected and outcome.status == "failed"
    assert outcome.missing == [] and result.missing_requirement_indices == ()
    reviewer.assert_awaited_once()
