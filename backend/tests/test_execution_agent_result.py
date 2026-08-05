import pytest
import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from openai import APIConnectionError, BadRequestError, InternalServerError

from app.domain.models.event import (
    ErrorEvent,
    McpToolContent,
    MessageEvent,
    PlanEvent,
    PlanStatus,
    StepEvent,
    StepStatus,
    ToolEvent,
    ToolStatus,
)
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.session import SessionStatus
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.agents.base import BaseAgent, LLMServiceUnavailableError
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow
from app.domain.models.memory import Memory
from app.domain.utils.robust_json_parser import parse_json_lenient


def test_parse_json_lenient_prefers_explicit_json_after_a_file_tree_block():
    raw = '''文件组织如下：
```
demo.zip
└── data/
    └── values.csv
```

```json
{"success": true, "result": "found values.csv", "attachments": []}
```'''

    assert parse_json_lenient(raw) == {
        "success": True,
        "result": "found values.csv",
        "attachments": [],
    }


@pytest.mark.asyncio
async def test_null_execution_payload_fails_cleanly_without_validation_exception():
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()

    async def fake_execute(_message):
        yield MessageEvent(message="null")

    agent.execute = fake_execute
    step = Step(description="analyze dataset")

    events = [
        event
        async for event in agent.execute_step(
            Plan(language="zh", steps=[step]),
            step,
            Message(message="analyze dataset"),
        )
    ]

    assert step.status == ExecutionStatus.FAILED
    assert step.success is False
    assert step.error
    assert "validation error for ExecutionResult" not in step.error
    assert any(
        isinstance(event, StepEvent) and event.status == StepStatus.FAILED
        for event in events
    )
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_execution_step_accepts_json_block_after_a_markdown_tree():
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()
    agent.ask_with_messages = AsyncMock(
        side_effect=AssertionError("valid local JSON extraction must not call repair")
    )

    async def fake_execute(_message):
        yield MessageEvent(message='''文件清单：
```
archive.zip
└── data.csv
```
```json
{"success":true,"result":"包含 data.csv","attachments":[]}
```''')

    agent.execute = fake_execute
    step = Step(description="list files")

    events = [
        event
        async for event in agent.execute_step(
            Plan(language="zh", steps=[step]),
            step,
            Message(message="这个数据集包含哪些文件？"),
        )
    ]

    assert step.status == ExecutionStatus.COMPLETED
    assert step.success is True
    assert step.result == "包含 data.csv"
    assert any(
        isinstance(event, MessageEvent) and event.message == "包含 data.csv"
        for event in events
    )
    agent.ask_with_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_result_ignores_llm_status_without_changing_step_state():
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()

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
async def test_no_tool_finalization_does_not_bind_or_advertise_tools():
    final = AIMessage(content='{"success":false,"result":"bounded","attachments":[]}')
    agent, _chain = _agent_with_model_chain([final])
    agent.bind_tools = True
    agent.get_tools = MagicMock(return_value=[MagicMock()])
    runnable = agent._model.bind.return_value

    with patch("app.domain.services.agents.base.RobustJsonParser.from_llm", return_value=object()):
        result = await agent.ask_with_messages([], allow_tools=False)

    assert result is final
    assert agent._model.bind.call_args.kwargs == {"response_format": None}
    runnable.bind_tools.assert_not_called()
    agent.get_tools.assert_not_called()


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


@pytest.mark.asyncio
async def test_successful_file_write_content_is_not_replayed_to_the_model():
    agent = object.__new__(BaseAgent)
    agent.max_iterations = 2
    agent.max_retries = 0
    agent.retry_interval = 0
    source = "print('chart')\n" * 2000
    first = AIMessage(content="", tool_calls=[{
        "name": "file_write",
        "args": {"file": "/home/ubuntu/output/chart.py", "content": source},
        "id": "call-write",
    }])
    final = AIMessage(content="completed")
    tool = SimpleNamespace(toolkit=SimpleNamespace(name="file"))
    responses = []

    async def ask(_request, _format=None):
        return first

    async def ask_with_messages(messages, _format=None):
        responses.append(messages)
        return final

    async def invoke_tool(_tool, _tool_call):
        return ToolMessage(
            tool_call_id="call-write",
            name="file_write",
            content='{"success":true,"data":{"bytes_written":30000}}',
            artifact=SimpleNamespace(success=True),
        )

    agent.ask = ask
    agent.ask_with_messages = ask_with_messages
    agent.get_tool = lambda _name: tool
    agent.invoke_tool = invoke_tool

    _events = [event async for event in agent.execute("write chart")]

    compacted = first.tool_calls[0]["args"]["content"]
    assert source not in compacted
    assert "persisted successfully" in compacted
    assert "sha256" in compacted
    assert responses[0][0].content.startswith("{")


@pytest.mark.asyncio
async def test_successful_shell_run_command_is_compacted_before_next_model_turn():
    agent = object.__new__(BaseAgent)
    agent.max_iterations = 2
    agent.max_retries = 0
    agent.retry_interval = 0
    command = "python -c 'print(1)'\n" * 1000
    first = AIMessage(content="", tool_calls=[{
        "name": "shell_run",
        "args": {"id": "plot", "exec_dir": "/home/ubuntu", "command": command},
        "id": "call-shell-run",
    }])
    final = AIMessage(content="completed")
    tool = SimpleNamespace(toolkit=SimpleNamespace(name="shell"))

    async def ask(_request, _format=None):
        return first

    async def ask_with_messages(_messages, _format=None):
        return final

    async def invoke_tool(_tool, _tool_call):
        return ToolMessage(
            tool_call_id="call-shell-run",
            name="shell_run",
            content="ok",
            artifact=SimpleNamespace(success=True),
        )

    agent.ask = ask
    agent.ask_with_messages = ask_with_messages
    agent.get_tool = lambda _name: tool
    agent.invoke_tool = invoke_tool

    _events = [event async for event in agent.execute("run chart")]

    compacted = first.tool_calls[0]["args"]["command"]
    assert command not in compacted
    assert "command compacted" in compacted
    assert "sha256" in compacted


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


def test_oversized_current_turn_keeps_request_and_latest_complete_tool_exchange():
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="visualize this dataset"),
    ]
    for index in range(20):
        messages.extend([
            AIMessage(content="", tool_calls=[{
                "name": "shell_view",
                "args": {"id": "analysis"},
                "id": f"call-{index}",
            }]),
            ToolMessage(
                tool_call_id=f"call-{index}",
                name="shell_view",
                content=(f"output-{index}:" + "x" * 20_000),
            ),
        ])
    memory = Memory(messages=messages)

    changed = memory.bound(96 * 1024, 16 * 1024)

    assert changed is True
    assert Memory._serialized_size(memory.messages) <= 96 * 1024
    assert any(
        message.type == "human" and message.content == "visualize this dataset"
        for message in memory.messages
    )
    assert any(
        message.type == "tool" and message.tool_call_id == "call-19"
        for message in memory.messages
    )


def test_successful_step_continues_without_replanning():
    succeeded = Step(status=ExecutionStatus.COMPLETED, success=True)
    failed = Step(status=ExecutionStatus.FAILED, success=False)

    assert PlanActFlow._status_after_execution_step(succeeded) == AgentStatus.EXECUTING
    assert PlanActFlow._status_after_execution_step(failed) == AgentStatus.UPDATING


@pytest.mark.asyncio
async def test_successful_step_emits_updated_plan_before_advancing():
    original_plan = Plan(
        title="two-step plan",
        steps=[
            Step(id="step-1", description="create an external artifact"),
            Step(id="step-2", description="summarize the artifact"),
        ],
    )

    class FakeSessionRepository:
        def __init__(self):
            self.session = SimpleNamespace(status=SessionStatus.PENDING)
            self.events = [
                PlanEvent(
                    status=PlanStatus.CREATED,
                    plan=original_plan.model_copy(deep=True),
                )
            ]

        async def find_by_id(self, _session_id):
            return self.session

        async def get_events(self, _session_id):
            return self.events

        async def update_status(self, _session_id, status):
            self.session.status = status

    class FakeSkillRegistry:
        def clear_restriction(self):
            return None

        def reload(self):
            return None

        def restrict_to(self, _names):
            return None

    class FakeExecutor:
        async def execute_step(self, _plan, step, _message):
            step.status = ExecutionStatus.COMPLETED
            step.success = True
            step.result = "artifact created"
            yield StepEvent(status=StepStatus.COMPLETED, step=step)

        async def compact_memory(self):
            return None

    repository = FakeSessionRepository()
    flow = object.__new__(PlanActFlow)
    flow._agent_id = "agent-1"
    flow._session_id = "session-1"
    flow._session_repository = repository
    flow._sandbox = object()
    flow.status = AgentStatus.EXECUTING
    flow.plan = None
    flow.skill_registry = FakeSkillRegistry()
    flow.active_skill_context = ""
    flow.session_context = ""
    flow.dataset_context = ""
    flow.enabled_subagents = {
        "execution": SimpleNamespace(handler_type="execution"),
    }
    flow.executor = FakeExecutor()
    flow.vision = object()

    updated_event = None
    event_stream = flow.run(Message(message="run both steps"))
    async for event in event_stream:
        if isinstance(event, PlanEvent) and event.status == PlanStatus.UPDATED:
            updated_event = event.model_copy(deep=True)
            await event_stream.aclose()
            break

    assert updated_event is not None
    assert updated_event.step.id == "step-1"
    assert updated_event.plan.steps[0].status == ExecutionStatus.COMPLETED
    assert updated_event.plan.steps[0].success is True
    assert updated_event.plan.get_next_step().id == "step-2"


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
