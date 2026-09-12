from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.analysis_job import AnalysisJobStatus
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.model_runtime import ModelBudgetStopped
from app.domain.services.safety.reviewer import SafetyReviewAgent
from app.domain.services.tools.analysis_job import AnalysisJobInterceptor
from app.domain.services.tools.pipeline import ToolExecutionContext


@pytest.mark.asyncio
async def test_execution_protocol_correction_preserves_runtime_cancellation() -> None:
    agent = object.__new__(ExecutionAgent)
    agent.ask = AsyncMock(return_value=AIMessage(content="null"))
    agent.get_tools = lambda: [object()]
    agent.ask_with_messages = AsyncMock(
        side_effect=ModelBudgetStopped("task_call_budget_exceeded")
    )

    with pytest.raises(ModelBudgetStopped) as caught:
        _events = [event async for event in agent.execute("Analyze synthetic data")]

    assert caught.value.code == "task_call_budget_exceeded"
    assert agent.last_execution_outcome["code"] == "task_call_budget_exceeded"
    agent.ask_with_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_safety_model_timeout_boundary_preserves_budget_stop() -> None:
    class EmptyPolicyStore:
        async def list_enabled(self):
            return []

    class StoppedModel:
        def bind(self, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            raise ModelBudgetStopped("task_token_budget_exceeded")

    reviewer = object.__new__(SafetyReviewAgent)
    reviewer._policy_store = EmptyPolicyStore()
    reviewer._model = StoppedModel()
    reviewer._timeout_seconds = 1

    with pytest.raises(ModelBudgetStopped) as caught:
        await reviewer.review("请分析数据")

    assert caught.value.code == "task_token_budget_exceeded"


@pytest.mark.asyncio
async def test_analysis_job_preserves_model_budget_stop_from_worker() -> None:
    class Service:
        async def create(self, **_kwargs):
            return SimpleNamespace(job_id="job-1", status=AnalysisJobStatus.QUEUED)

        async def bind(self, _job_id, _worker):
            return None

        async def get_for_owner(self, *_args):
            raise AssertionError("budget cancellation must not be reclassified")

    interceptor = AnalysisJobInterceptor(
        Service(),
        user_id="user-1",
        session_id="session-1",
        identity_provider=lambda: {"task_id": "task-1"},
    )
    context = ToolExecutionContext(
        tool=SimpleNamespace(name="shell_run"),
        tool_call={"name": "shell_run", "id": "call-1", "args": {}},
    )

    async def stopped_call():
        raise ModelBudgetStopped("task_token_budget_exceeded")

    with pytest.raises(ModelBudgetStopped) as caught:
        await interceptor.execute(context, stopped_call)

    assert caught.value.code == "task_token_budget_exceeded"
