import pytest
import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from openai import APIConnectionError, BadRequestError, InternalServerError

from app.domain.models.event import McpToolContent, MessageEvent, StepEvent, StepStatus, ToolEvent, ToolStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.agents.base import BaseAgent, LLMServiceUnavailableError
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.models.memory import Memory


@pytest.mark.asyncio
async def test_execution_result_ignores_llm_status_without_changing_step_state():
    agent = object.__new__(ExecutionAgent)

    async def fake_execute(_message):
        yield MessageEvent(message='{"success": true, "result": "done"}')

    async def fake_parse_json(_message):
        return {
            "success": True,
            "result": "done",
            "attachments": ["/tmp/chart.png"],
            "status": "available",
        }

    agent.execute = fake_execute
    agent._parse_json = fake_parse_json
    step = Step(description="generate chart")

    events = [
        event
        async for event in agent.execute_step(
            Plan(steps=[step]),
            step,
            Message(message="generate chart"),
        )
    ]

    assert step.status == ExecutionStatus.COMPLETED
    assert step.success is True
    assert step.result == "done"
    assert step.attachments == ["/tmp/chart.png"]
    assert any(
        isinstance(event, StepEvent) and event.status == StepStatus.COMPLETED
        for event in events
    )


@pytest.mark.asyncio
async def test_llm_connection_error_retries_without_replaying_tool_calls():
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    agent.retry_interval = 0
    agent.bind_tools = False
    agent.tool_choice = None
    agent.dynamic_system_prompt_provider = None
    agent.memory = SimpleNamespace(get_messages=lambda: [])
    agent._add_to_memory = AsyncMock()
    agent._record_token_usage = AsyncMock()
    model = MagicMock()
    runnable = MagicMock()
    chain = MagicMock()
    chain.ainvoke = AsyncMock()
    runnable.bind_tools.return_value = runnable
    runnable.__or__.return_value = chain
    model.bind.return_value = runnable
    chain.ainvoke.side_effect = [
        APIConnectionError(
            message="TLS connection dropped",
            request=httpx.Request("POST", "https://api.example.test/v1/chat/completions"),
        ),
        AIMessage(content="recovered"),
    ]
    agent._model = model

    with patch("app.domain.services.agents.base.RobustJsonParser.from_llm", return_value=object()):
        result = await agent.ask_with_messages([])

    assert result.content == "recovered"
    assert chain.ainvoke.call_count == 2
    agent._record_token_usage.assert_awaited_once_with(result)


def _openai_status_error(error_type, status_code: int, message: str):
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return error_type(message, response=response, body={"error": {"message": message}})


def _agent_with_model_chain(side_effect, *, retry_attempts: int = 3):
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    agent.retry_interval = 0
    agent._llm_retry_attempts = retry_attempts
    agent._llm_retry_base_seconds = 0
    agent._llm_retry_max_seconds = 0
    agent.bind_tools = False
    agent.tool_choice = None
    agent.dynamic_system_prompt_provider = None
    agent.memory = SimpleNamespace(get_messages=lambda: [])
    agent._add_to_memory = AsyncMock()
    agent._record_token_usage = AsyncMock()
    model = MagicMock()
    runnable = MagicMock()
    chain = MagicMock()
    chain.ainvoke = AsyncMock(side_effect=side_effect)
    runnable.bind_tools.return_value = runnable
    runnable.__or__.return_value = chain
    model.bind.return_value = runnable
    agent._model = model
    return agent, chain


@pytest.mark.asyncio
async def test_llm_503_retries_then_recovers():
    busy = _openai_status_error(
        InternalServerError,
        503,
        "Service is too busy; upstream details must not reach the task event",
    )
    recovered = AIMessage(content="recovered after provider congestion")
    agent, chain = _agent_with_model_chain([busy, recovered])

    with patch("app.domain.services.agents.base.RobustJsonParser.from_llm", return_value=object()):
        result = await agent.ask_with_messages([])

    assert result is recovered
    assert chain.ainvoke.call_count == 2
    agent._record_token_usage.assert_awaited_once_with(recovered)


@pytest.mark.asyncio
async def test_llm_503_retry_exhaustion_raises_friendly_error_without_upstream_details():
    failures = [
        _openai_status_error(
            InternalServerError,
            503,
            "Service is too busy; secret-upstream-diagnostic",
        )
        for _ in range(3)
    ]
    agent, chain = _agent_with_model_chain(failures, retry_attempts=3)

    with (
        patch("app.domain.services.agents.base.RobustJsonParser.from_llm", return_value=object()),
        pytest.raises(LLMServiceUnavailableError) as exc_info,
    ):
        await agent.ask_with_messages([])

    message = str(exc_info.value)
    assert "模型服务暂时繁忙" in message
    assert "Service is too busy" not in message
    assert "secret-upstream-diagnostic" not in message
    assert chain.ainvoke.call_count == 3
    agent._record_token_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_transient_llm_status_error_is_not_retried():
    bad_request = _openai_status_error(BadRequestError, 400, "invalid request")
    agent, chain = _agent_with_model_chain([bad_request])

    with (
        patch("app.domain.services.agents.base.RobustJsonParser.from_llm", return_value=object()),
        pytest.raises(BadRequestError),
    ):
        await agent.ask_with_messages([])

    assert chain.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_unknown_tool_call_receives_tool_message_before_next_model_turn():
    agent = object.__new__(BaseAgent)
    agent.max_iterations = 2
    agent.max_retries = 0
    agent.retry_interval = 0
    first = AIMessage(content="", tool_calls=[{
        "name": "removed_tool",
        "args": {"query": "test"},
        "id": "call-unknown",
    }])
    final = AIMessage(content="completed")
    responses = []

    async def ask(_request, _format=None):
        return first

    async def ask_with_messages(messages, _format=None):
        responses.append(messages)
        return final

    agent.ask = ask
    agent.ask_with_messages = ask_with_messages
    agent.get_tool = lambda _name: None

    events = [event async for event in agent.execute("run task")]

    assert any(event.error == "Unknown tool: removed_tool" for event in events if hasattr(event, "error"))
    assert len(responses) == 1
    assert len(responses[0]) == 1
    assert isinstance(responses[0][0], ToolMessage)
    assert responses[0][0].tool_call_id == "call-unknown"


@pytest.mark.asyncio
async def test_tool_result_is_bound_to_the_active_tool_call_id():
    agent = object.__new__(BaseAgent)
    agent.max_iterations = 2
    agent.max_retries = 0
    agent.retry_interval = 0
    first = AIMessage(content="", tool_calls=[{
        "name": "available_tool",
        "args": {},
        "id": "call-active",
    }])
    final = AIMessage(content="completed")
    tool = SimpleNamespace(toolkit=SimpleNamespace(name="test"))
    responses = []

    async def ask(_request, _format=None):
        return first

    async def ask_with_messages(messages, _format=None):
        responses.append(messages)
        return final

    async def invoke_tool(_tool, _tool_call):
        return ToolMessage(tool_call_id="", name="available_tool", content="ok")

    agent.ask = ask
    agent.ask_with_messages = ask_with_messages
    agent.get_tool = lambda _name: tool
    agent.invoke_tool = invoke_tool

    _events = [event async for event in agent.execute("run task")]

    assert responses[0][0].tool_call_id == "call-active"


@pytest.mark.asyncio
async def test_large_tool_result_is_bounded_before_agent_memory_is_saved():
    agent = object.__new__(BaseAgent)
    agent.max_iterations = 2
    agent.max_retries = 0
    agent.retry_interval = 0
    first = AIMessage(content="", tool_calls=[{
        "name": "available_tool",
        "args": {},
        "id": "call-large",
    }])
    final = AIMessage(content="completed")
    tool = SimpleNamespace(toolkit=SimpleNamespace(name="test"))
    responses = []

    async def ask(_request, _format=None):
        return first

    async def ask_with_messages(messages, _format=None):
        responses.append(messages)
        return final

    async def invoke_tool(_tool, _tool_call):
        return ToolMessage(
            tool_call_id="call-large",
            name="available_tool",
            content="x" * (BaseAgent.MAX_TOOL_MESSAGE_CONTENT_BYTES + 1),
            artifact={"raw": "x" * (BaseAgent.MAX_TOOL_MESSAGE_CONTENT_BYTES + 1)},
        )

    agent.ask = ask
    agent.ask_with_messages = ask_with_messages
    agent.get_tool = lambda _name: tool
    agent.invoke_tool = invoke_tool

    _events = [event async for event in agent.execute("run task")]

    memory_result = responses[0][0]
    assert len(memory_result.content.encode("utf-8")) <= BaseAgent.MAX_TOOL_MESSAGE_CONTENT_BYTES
    assert "truncated" in memory_result.content
    assert memory_result.artifact is None


def test_oversized_tool_event_is_bounded_before_session_persistence():
    runner = object.__new__(AgentTaskRunner)
    runner._agent_id = "agent-large"
    oversized_payload = "x" * (AgentTaskRunner.MAX_EVENT_PAYLOAD_BYTES * 4 + 1)
    event = ToolEvent(
        status=ToolStatus.CALLED,
        tool_call_id="call-large",
        tool_name="mcp",
        function_name="large_result",
        function_args={"payload": oversized_payload},
        function_result={"payload": oversized_payload},
        tool_content=McpToolContent(result={"payload": oversized_payload}),
    )

    bounded = runner._bound_event_payload(event)

    assert len(bounded.model_dump_json().encode("utf-8")) <= AgentTaskRunner.MAX_EVENT_PAYLOAD_BYTES
    assert bounded.function_result["truncated"] is True
    assert bounded.tool_content.result["truncated"] is True


def test_agent_memory_is_bounded_before_one_mongo_document_can_overflow():
    messages = [SystemMessage(content="system")]
    for index in range(20):
        messages.extend([
            HumanMessage(content=f"question {index}: " + "x" * 150_000),
            AIMessage(content=f"answer {index}: " + "y" * 150_000),
        ])
    memory = Memory(messages=messages)

    changed = memory.bound(1024 * 1024)

    assert changed is True
    assert Memory._serialized_size(memory.messages) <= 1024 * 1024
    assert memory.messages[0].type == "system"
    assert any(message.content.startswith("question 19:") for message in memory.messages)


@pytest.mark.asyncio
async def test_incomplete_tool_call_history_is_repaired_before_model_invocation():
    agent = object.__new__(BaseAgent)
    agent.max_retries = 1
    agent.retry_interval = 0
    agent.bind_tools = False
    agent.tool_choice = None
    agent.dynamic_system_prompt_provider = None
    agent._agent_id = "agent-1"
    agent.name = "base"
    agent.memory = Memory(messages=[
        SystemMessage(content="system"),
        AIMessage(content="", tool_calls=[{
            "name": "interrupted_tool",
            "args": {},
            "id": "call-interrupted",
        }]),
        HumanMessage(content="continue"),
    ])
    agent._add_to_memory = AsyncMock()
    agent._record_token_usage = AsyncMock()
    agent._repository = SimpleNamespace(save_memory=AsyncMock())
    model = MagicMock()
    runnable = MagicMock()
    chain = MagicMock()
    chain.ainvoke = AsyncMock(return_value=AIMessage(content="recovered"))
    runnable.bind_tools.return_value = runnable
    runnable.__or__.return_value = chain
    model.bind.return_value = runnable
    agent._model = model

    with patch("app.domain.services.agents.base.RobustJsonParser.from_llm", return_value=object()):
        result = await agent.ask_with_messages([])

    assert result.content == "recovered"
    context = chain.ainvoke.await_args.args[0]
    assert [message.type for message in context] == ["system", "ai", "tool", "human"]
    repaired = context[2]
    assert isinstance(repaired, ToolMessage)
    assert repaired.tool_call_id == "call-interrupted"
