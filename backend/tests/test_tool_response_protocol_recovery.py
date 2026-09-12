import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools import tool
from pydantic import PrivateAttr

from app.domain.models.event import ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.memory import Memory
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionResult, Plan
from app.domain.models.tool_result import ToolResult
from app.domain.services import execution_identity
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.tools.base import BaseToolkit, tool_execution_contract
from app.domain.services.tools.pipeline import ToolExecutionInterceptor, ToolExecutionPipeline
from app.domain.utils.tool_response_protocol import textual_tool_envelope_reason


DSML = ('{"type":"json_object"}\n\n<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="file_write">\n'
        '<｜｜DSML｜｜ parameter name="file" string="true">private-not-executed.py')


@pytest.mark.parametrize("content", [
    DSML,
    '<｜DSML｜function_calls><｜DSML｜invoke name="shell_exec">',
    '<|DSML|function_calls><|DSML|invoke name="shell_exec">',
    ' \n<||DSML|| calls>\n<||DSML|| invoke name="file_write">',
    '<tool_call>{"name":"shell_exec","arguments":{}}</tool_call>',
    '<tool_calls><tool_call name="shell_exec"/></tool_calls>',
    '<function_calls><invoke name="shell_exec"/></function_calls>',
    '<function=shell_exec>{"command":"not executed"}</function>',
    [{"type": "text", "text": DSML}],
])
def test_detects_only_explicit_top_level_textual_envelopes(content):
    assert textual_tool_envelope_reason(content) == "textual_tool_envelope"


@pytest.mark.parametrize("content", [
    "Finished the analysis.", "", None, 3,
    'The token <｜｜DSML｜｜ calls> is a protocol marker.',
    'Example:\n<tool_call>{"name":"shell_exec"}</tool_call>',
    '> <｜｜DSML｜｜ calls>\n> quoted example',
    '```xml\n<tool_call>{"name":"shell_exec"}</tool_call>\n```',
    '`<｜｜DSML｜｜ calls>`',
    json.dumps({"success": True, "result": DSML, "attachments": []}),
    json.dumps({"tool_calls": [{"name": "a", "arguments": {"example": DSML}}]}),
    json.dumps([DSML]), json.dumps(DSML),
    '{"type":"json_object"}', '<tool_catalog>not an invocation</tool_catalog>',
    [{"type": "reasoning", "text": DSML}], {"text": DSML},
])
def test_plain_text_and_quoted_json_or_code_are_not_tool_protocol(content):
    assert textual_tool_envelope_reason(content) is None


class ProbeToolkit(BaseToolkit):
    name: str = "probe"
    _calls: list[str] = PrivateAttr(default_factory=list)

    @tool_execution_contract(effects=("sandbox_read",))
    @tool
    async def read_probe(self, value: str) -> ToolResult:
        """Read a deterministic synthetic observation."""
        self._calls.append(value)
        return ToolResult(success=True, data={"observation": value})


class RecordingGuard(ToolExecutionInterceptor):
    def __init__(self, *, reject=False):
        self.calls = []
        self.reject = reject

    async def guard(self, context):
        self.calls.append((context.tool_name, dict(context.arguments)))
        if self.reject:
            raise PermissionError("synthetic authorization rejection")


def native_call(value="native-only", *, name="read_probe", call_id="native-1", content=""):
    return AIMessage(content=content, tool_calls=[{"name": name, "args": {"value": value}, "id": call_id}])


def make_agent(monkeypatch, responses, *, guard=None, agent_type=BaseAgent):
    """Exercise production ask/execute/registry/pipeline with only model I/O fake."""
    monkeypatch.setattr(execution_identity, "get_settings", lambda: SimpleNamespace(
        execution_snapshot_identity_key="synthetic-protocol-test-key"))
    monkeypatch.setattr("app.domain.services.agents.base.RobustJsonParser.from_llm", lambda _model: object())
    agent = object.__new__(agent_type)
    agent.name = "protocol-test"
    agent._agent_id = "synthetic-agent"
    agent._model_name = "synthetic-model"
    agent._repository = SimpleNamespace(save_memory=AsyncMock())
    agent.memory = Memory()
    agent.dynamic_system_prompt_provider = None
    agent._record_token_usage = AsyncMock()
    agent.max_retries = 0
    agent._current_plan = Plan(language="zh")
    agent._current_message = Message(message="分析合成数据")
    agent._dataset_fast_path_mode = False
    agent._dataset_intent = "analysis"
    agent._allow_terminal_quicklook = False
    agent._prefer_quicklook_evidence = False
    toolkit = ProbeToolkit()
    if guard is not None:
        toolkit.tool_execution_pipeline = ToolExecutionPipeline([guard])
    agent.toolkits = [toolkit]
    requests = []
    bindings = []
    response_iter = iter(responses)

    def bind(**options):
        binding = {"options": options, "tools": []}
        bindings.append(binding)
        runnable = MagicMock()

        def bind_tools(tools):
            binding["tools"] = list(tools)
            return runnable

        async def invoke(context):
            requests.append(list(context))
            value = next(response_iter)
            if callable(value):
                value = value(agent)
            if isinstance(value, BaseException):
                raise value
            return value

        runnable.bind_tools.side_effect = bind_tools
        runnable.__or__.return_value = SimpleNamespace(ainvoke=invoke)
        return runnable

    agent._model = SimpleNamespace(bind=bind)
    return agent, toolkit, requests, bindings


@pytest.mark.asyncio
async def test_native_recovery_keeps_registry_guard_ledger_memory_and_format_scope(monkeypatch, caplog):
    guard = RecordingGuard()
    agent, toolkit, requests, bindings = make_agent(monkeypatch, [
        AIMessage(content=DSML), native_call(), AIMessage(content="Actual observed result"),
    ], guard=guard)
    events = [event async for event in agent.execute("Analyze synthetic data", format="json_object")]
    assert toolkit._calls == ["native-only"]
    assert guard.calls == [("read_probe", {"value": "native-only"})]
    assert agent._tool_execution_ledger.summary()["pending_execution"] is False
    assert len(requests) == 3
    assert [item["options"]["response_format"] for item in bindings] == [
        {"type": "json_object"}, None, {"type": "json_object"},
    ]
    assert all([tool.name for tool in item["tools"]] == ["read_probe"] for item in bindings)
    assert "NOT executed" in requests[1][-1].content
    assert "private-not-executed" not in requests[1][-1].content
    assert any(isinstance(item, ToolMessage) and item.tool_call_id == "native-1" for item in requests[2])
    assert agent._record_token_usage.await_count == 3
    assert [event.message for event in events if isinstance(event, MessageEvent)] == ["Actual observed result"]
    assert len([event for event in events if isinstance(event, ToolEvent)]) == 2
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert "private-not-executed" not in caplog.text


@pytest.mark.asyncio
async def test_native_tool_channel_wins_even_when_content_mentions_an_envelope(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [native_call(content=DSML), AIMessage(content="Done")])
    events = [event async for event in agent.execute("Read synthetic data")]
    assert toolkit._calls == ["native-only"] and len(requests) == 2
    assert events[-1].message == "Done"


@pytest.mark.asyncio
async def test_repeated_protocol_failure_stops_without_publishing_or_executing_text(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [AIMessage(content=DSML), AIMessage(content=DSML)])
    events = [event async for event in agent.execute("Analyze")]
    assert len(requests) == 2 and toolkit._calls == []
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)
    assert agent.last_execution_outcome["code"] == "tool_protocol_error"
    assert DSML not in events[0].error


@pytest.mark.asyncio
async def test_unavailable_registry_does_not_grant_execution_from_text(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [AIMessage(content=DSML)])
    agent.toolkits = []
    events = [event async for event in agent.execute("Analyze")]
    assert len(requests) == 1 and toolkit._calls == []
    assert isinstance(events[-1], ErrorEvent)


@pytest.mark.asyncio
async def test_recovery_does_not_bypass_a_guard_rejection(monkeypatch):
    guard = RecordingGuard(reject=True)
    agent, toolkit, requests, _ = make_agent(monkeypatch, [
        AIMessage(content=DSML), native_call(), AIMessage(content=DSML),
    ], guard=guard)
    events = [event async for event in agent.execute("Analyze")]
    assert guard.calls == [("read_probe", {"value": "native-only"})]
    assert toolkit._calls == [] and len(requests) == 3
    assert isinstance(events[-1], ErrorEvent)
    assert any(isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED for event in events)


@pytest.mark.asyncio
async def test_unknown_pending_execution_is_not_replayed_or_reprompted(monkeypatch):
    def unresolved(agent):
        agent._tool_execution_ledger.record_unknown("prior-unknown-write")
        return AIMessage(content=DSML)
    agent, toolkit, requests, _ = make_agent(monkeypatch, [unresolved])
    events = [event async for event in agent.execute("Analyze")]
    assert toolkit._calls == [] and len(requests) == 1
    assert agent.last_execution_outcome["code"] == "tool_execution_unknown"
    assert agent._tool_execution_ledger.summary()["pending_execution"] is True
    assert isinstance(events[-1], ErrorEvent)


@pytest.mark.asyncio
async def test_cancellation_during_recovery_propagates_without_execution(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [AIMessage(content=DSML), asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        _events = [event async for event in agent.execute("Analyze")]
    assert toolkit._calls == [] and len(requests) == 2
    assert agent.last_execution_outcome["code"] == "cancelled"


@pytest.mark.asyncio
async def test_new_confirmed_read_progress_allows_a_later_independent_protocol_correction(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [
        AIMessage(content=DSML), native_call("first"), AIMessage(content=DSML),
        native_call("second", call_id="native-2"), AIMessage(content="Done"),
    ])
    agent.max_iterations = 1  # Removed task quotas cannot be reinstated by recovery.
    events = [event async for event in agent.execute("Analyze", max_iterations=1)]
    assert toolkit._calls == ["first", "second"] and len(requests) == 5
    assert events[-1].message == "Done"
    assert agent.last_execution_outcome["tool_batch_limit"] is None


@pytest.mark.asyncio
async def test_direct_compilation_or_explanation_ask_is_not_execution_recovery(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [AIMessage(content=DSML)])
    message = await agent.ask_with_messages([HumanMessage(content="Explain a protocol example")], allow_tools=False)
    assert message.content == DSML and len(requests) == 1 and toolkit._calls == []


@pytest.mark.asyncio
async def test_subclass_result_contract_uses_evidence_only_correction(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [AIMessage(content=""), native_call(), AIMessage(content="Done")])
    original = agent._terminal_response_problem
    agent._terminal_response_problem = lambda message: original(message) or (
        "invalid_execution_result" if not message.tool_calls and not message.content else None)
    events = [event async for event in agent.execute("Analyze")]
    assert "success" in requests[1][-1].content and "do not invent" in requests[1][-1].content
    assert toolkit._calls == ["native-only"] and events[-1].message == "Done"


@pytest.mark.asyncio
async def test_fault_finalization_cannot_reopen_tools_or_publish_an_envelope(monkeypatch):
    agent, toolkit, requests, bindings = make_agent(monkeypatch, [
        native_call(name="unregistered_tool"),
        native_call(name="unregistered_tool", call_id="missing-2"),
        AIMessage(content=DSML),
    ])
    events = [event async for event in agent.execute("Analyze")]
    assert len(requests) == 3 and toolkit._calls == []
    assert bindings[-1]["tools"] == [], "terminal fault synthesis must stay tool-free"
    assert isinstance(events[-1], ErrorEvent)
    assert agent.last_execution_outcome["code"] == "invalid_final_result"
    assert not any(isinstance(event, MessageEvent) for event in events)


@pytest.mark.asyncio
async def test_deterministic_completion_cannot_publish_a_textual_invocation(monkeypatch):
    agent, toolkit, requests, _ = make_agent(monkeypatch, [native_call()])
    agent._completion_from_tool_batch = lambda _results: DSML
    events = [event async for event in agent.execute("Analyze")]
    assert toolkit._calls == ["native-only"] and len(requests) == 1
    assert isinstance(events[-1], ErrorEvent)
    assert not any(isinstance(event, MessageEvent) for event in events)


@pytest.mark.asyncio
async def test_successful_tool_free_synthesis_does_not_restart_execution(monkeypatch):
    agent, toolkit, requests, bindings = make_agent(monkeypatch, [native_call(), AIMessage(content=DSML)])
    agent._tool_free_completion_instruction = lambda _results: "Summarize only verified observations."
    events = [event async for event in agent.execute("Analyze")]
    assert toolkit._calls == ["native-only"] and len(requests) == 2
    assert bindings[-1]["tools"] == []
    assert isinstance(events[-1], ErrorEvent)
    assert not any(isinstance(event, MessageEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "null", "", 'print("script only, not executed")',
    '{"success":true,"attachments":["/home/ubuntu/output/not-created.png"]}',
    '{"success":true,"result":" ","attachments":[]}',
    '{"success":true,"result":"TODO","attachments":[]}',
    DSML,
])
async def test_execution_agent_real_override_recovers_unfinished_result_in_native_loop(monkeypatch, invalid):
    final = ExecutionResult(success=True, result="已读取合成观测值 native-only。", attachments=[]).model_dump_json()
    agent, toolkit, requests, bindings = make_agent(monkeypatch, [
        AIMessage(content=invalid), native_call(), AIMessage(content=final),
    ], agent_type=ExecutionAgent)
    agent._parse_json = AsyncMock(side_effect=AssertionError("No model-assisted JSON repair is allowed"))
    events = [event async for event in agent.execute("分析合成数据")]
    assert toolkit._calls == ["native-only"] and len(requests) == 3
    assert all([tool.name for tool in binding["tools"]] == ["read_probe"] for binding in bindings)
    assert [binding["options"]["response_format"] for binding in bindings] == [
        {"type": "json_object"}, None, {"type": "json_object"},
    ]
    agent._parse_json.assert_not_awaited()
    answers = [event.message for event in events if isinstance(event, MessageEvent)]
    assert answers == [final]
    decoded = await agent._decode_execution_result(answers[0])
    assert decoded.success is True and decoded.attachments == []
    assert agent.last_execution_outcome["code"] == "completed"


@pytest.mark.asyncio
async def test_execution_agent_repeated_invalid_final_never_publishes_claimed_files(monkeypatch):
    claimed = '{"success":true,"attachments":["/home/ubuntu/output/not-created.png"]}'
    agent, toolkit, requests, bindings = make_agent(monkeypatch, [
        AIMessage(content="null"), AIMessage(content=claimed),
    ], agent_type=ExecutionAgent)
    events = [event async for event in agent.execute("分析合成数据")]
    assert toolkit._calls == [] and len(requests) == 2
    assert all(binding["tools"] for binding in bindings)
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)
    assert "not-created" not in events[0].error and "模型" in events[0].error
    assert agent.last_execution_outcome["code"] == "invalid_execution_result"


@pytest.mark.asyncio
async def test_execution_agent_preserves_quoted_dsml_in_a_valid_result(monkeypatch):
    final = ExecutionResult(success=True, result=f"以下仅为引用：{DSML}", attachments=[]).model_dump_json()
    agent, toolkit, requests, _ = make_agent(monkeypatch, [AIMessage(content=final)], agent_type=ExecutionAgent)
    events = [event async for event in agent.execute("解释合成观测中的文本")]
    assert toolkit._calls == [] and len(requests) == 1
    assert [event.message for event in events if isinstance(event, MessageEvent)] == [final]


class ShellProbeToolkit(BaseToolkit):
    name: str = "shell"
    _calls: list[str] = PrivateAttr(default_factory=list)

    @tool
    async def shell_run(self, command: str) -> ToolResult:
        """Return a synthetic successful shell receipt without running a process."""
        self._calls.append(command)
        return ToolResult(success=True, data={"status": "completed", "returncode": 0,
                                              "output": "records: 12\nvalid: 12"})


@pytest.mark.asyncio
@pytest.mark.parametrize("final_response", [AIMessage(content=DSML), RuntimeError("synthetic unavailable model")])
async def test_execution_stdout_fallback_preserves_observed_results_without_reopening_tools(monkeypatch, final_response):
    native = AIMessage(content="", tool_calls=[{"name": "shell_run", "args": {"command": "synthetic"}, "id": "shell-1"}])
    agent, _unused, requests, bindings = make_agent(monkeypatch, [native, final_response], agent_type=ExecutionAgent)
    toolkit = ShellProbeToolkit()
    agent.toolkits = [toolkit]
    agent._current_message = Message(message="执行脚本并展示标准输出")
    events = [event async for event in agent.execute("执行脚本并展示标准输出")]
    assert toolkit._calls == ["synthetic"] and len(requests) == 2
    assert bindings[-1]["tools"] == []
    answers = [event.message for event in events if isinstance(event, MessageEvent)]
    assert len(answers) == 1
    decoded = ExecutionResult.model_validate_json(answers[0])
    assert "12" in decoded.result and decoded.attachments == []
    assert "DSML" not in decoded.result and "synthetic unavailable" not in decoded.result
