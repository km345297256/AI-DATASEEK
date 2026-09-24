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
        runnable.ainvoke = invoke
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

# Request-local preparation recovery is deliberately distinct from scientific
# evidence and from task/model budgets. All fixtures are offline.
from app.domain.services.analysis_protocol_recovery import (
    TerminalProtocolRecovery, confirmed_shell_success, native_operation_identity,
)
from app.domain.services.execution_evidence import (
    ShellExecutionAttempt, ToolExecutionLedger, shell_command_digest,
)
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.shell import ShellToolkit


def file_call(path="/home/ubuntu/output/analysis.py", *, content="print(1)", call_id="write-1", **extra):
    return AIMessage(content="", tool_calls=[{"name": "file_write", "id": call_id,
        "args": {"file": path, "content": content, **extra}}])


def core_files(agent, *, success=True):
    sandbox = SimpleNamespace(
        file_write=AsyncMock(return_value=ToolResult(success=success, data={})),
        file_read=AsyncMock(return_value=ToolResult(success=True, data={"content": "print(1)"})),
    )
    toolkit = FileToolkit(sandbox)
    agent.toolkits.append(toolkit)
    return sandbox, toolkit


@pytest.mark.asyncio
async def test_confirmed_source_preparation_allows_one_extra_correction_without_science_progress(monkeypatch):
    agent, _, requests, _ = make_agent(monkeypatch, [
        AIMessage(content=DSML), file_call(),
        AIMessage(content="", tool_calls=[{"name": "file_read", "id": "read-back",
            "args": {"file": "/home/ubuntu/output/analysis.py"}}]),
        AIMessage(content=DSML), native_call(), AIMessage(content="Done"),
    ])
    sandbox, _ = core_files(agent)
    events = [event async for event in agent.execute("Analyze")]
    assert sandbox.file_write.await_count == 1 and sandbox.file_read.await_count == 1
    assert len(requests) == 6 and events[-1].message == "Done"
    assert "NOT evidence of analysis execution" in requests[4][-1].content


@pytest.mark.asyncio
async def test_renaming_or_changing_preparation_cannot_grant_a_third_correction(monkeypatch):
    agent, _, requests, _ = make_agent(monkeypatch, [
        AIMessage(content=DSML), file_call(), AIMessage(content=DSML),
        file_call("/home/ubuntu/output/renamed.py", content="print(2)", call_id="write-2"),
        AIMessage(content=DSML),
    ])
    sandbox, _ = core_files(agent)
    events = [event async for event in agent.execute("Analyze")]
    assert sandbox.file_write.await_count == 2 and len(requests) == 5
    assert isinstance(events[-1], ErrorEvent)


@pytest.mark.asyncio
async def test_failed_preparation_does_not_authorize_extra_recovery(monkeypatch):
    agent, _, requests, _ = make_agent(monkeypatch, [
        AIMessage(content=DSML), file_call(), AIMessage(content=DSML),
    ])
    sandbox, _ = core_files(agent, success=False)
    events = [event async for event in agent.execute("Analyze")]
    assert sandbox.file_write.await_count == 1 and len(requests) == 3
    assert isinstance(events[-1], ErrorEvent)


@pytest.mark.asyncio
async def test_correction_suppresses_equivalent_completed_native_file_write(monkeypatch):
    agent, _, requests, _ = make_agent(monkeypatch, [
        file_call(content="print(1)\n"), AIMessage(content=DSML),
        file_call("/home/ubuntu/output/./analysis.py", call_id="new-id", append=None,
                  sudo=False, trailing_newline=True), AIMessage(content="Done"),
    ])
    sandbox, _ = core_files(agent)
    events = [event async for event in agent.execute("Analyze")]
    assert sandbox.file_write.await_count == 1 and len(requests) == 4
    assert any(isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED
        and isinstance(event.function_result, dict)
        and event.function_result.get("blocked_by_policy") == "tool_replay_suppressed" for event in events)


@pytest.mark.asyncio
async def test_recovery_state_does_not_cross_execute_requests(monkeypatch):
    sequence = [AIMessage(content=DSML), file_call(), AIMessage(content=DSML), AIMessage(content="Done")]
    # Each provider response is a fresh value. Reusing the same AIMessage would
    # feed the first request's compacted history receipt back as a new write.
    agent, _, requests, _ = make_agent(monkeypatch, sequence + [item.model_copy(deep=True) for item in sequence])
    sandbox, _ = core_files(agent)
    for _ in range(2):
        events = [event async for event in agent.execute("Analyze")]
        assert events[-1].message == "Done"
    assert sandbox.file_write.await_count == 2 and len(requests) == 8


@pytest.mark.asyncio
async def test_cancellation_during_preparation_correction_is_not_swallowed(monkeypatch):
    agent, _, requests, _ = make_agent(monkeypatch, [
        AIMessage(content=DSML), file_call(), AIMessage(content=DSML), asyncio.CancelledError(),
    ])
    sandbox, _ = core_files(agent)
    with pytest.raises(asyncio.CancelledError):
        _events = [event async for event in agent.execute("Analyze")]
    assert sandbox.file_write.await_count == 1 and len(requests) == 4
    assert agent.last_execution_outcome["code"] == "cancelled"


def add_receipt(ledger, call, *, returncode=0, ordinal=1, sandbox_id="synthetic-sandbox"):
    args = call["args"]
    attempt = ShellExecutionAttempt(call["id"], f"{ordinal:032x}", sandbox_id,
        args.get("id", "shell"), shell_command_digest(args.get("exec_dir", "/work"), args.get("command", "")), AsyncMock())
    ledger.register(attempt)
    assert attempt.observe({"version": 1, "operation_id": attempt.operation_id,
        "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
        "state": "exited", "returncode": returncode, "process_tree_quiescent": True})
    return attempt


@pytest.mark.asyncio
async def test_new_failed_shell_receipts_never_reopen_protocol_correction(monkeypatch):
    def failed_operations(agent):
        # Regression for real 020: different commands and call/operation IDs,
        # each terminal failure, used to increment the strong progress tuple.
        for i in range(3):
            call = {"name": "shell_run", "id": f"failed-{i}", "args": {
                "id": f"shell-{i}", "exec_dir": "/work", "command": f"python -c 'bad_key_{i}'"}}
            add_receipt(agent._tool_execution_ledger, call, returncode=1, ordinal=i + 1)
        return AIMessage(content=DSML)
    agent, toolkit, requests, _ = make_agent(monkeypatch, [AIMessage(content=DSML), failed_operations])
    events = [event async for event in agent.execute("Analyze")]
    assert len(requests) == 2 and toolkit._calls == []
    assert agent._tool_execution_ledger.summary()["confirmed_failed_operation_count"] == 3
    assert isinstance(events[-1], ErrorEvent)


def test_confirmed_shell_progress_excludes_failure_unknown_and_duplicate_ids(monkeypatch):
    make_agent(monkeypatch, [])
    toolkit = ShellToolkit(SimpleNamespace(id="synthetic-sandbox"))
    tool = toolkit.get_tool("shell_run")
    call = {"name": "shell_run", "id": "one", "args": {
        "id": "shell-a", "exec_dir": "/work", "command": "inspect --summary"}}
    ledger, recovery = ToolExecutionLedger(), TerminalProtocolRecovery()
    initial = recovery.progress("read-evidence")
    assert recovery.correction_kind(initial) == "protocol"
    add_receipt(ledger, call, returncode=1)
    assert not confirmed_shell_success(tool, call, ledger)
    assert recovery.correction_kind(initial) is None
    call = {**call, "id": "two", "args": {**call["args"], "id": "shell-b", "timeout_seconds": 60}}
    add_receipt(ledger, call, ordinal=2)
    assert confirmed_shell_success(tool, call, ledger)
    recovery.record_shell_success(native_operation_identity(tool, call))
    assert recovery.correction_kind(recovery.progress("read-evidence")) == "protocol"
    duplicate = {**call, "id": "three", "args": {**call["args"], "id": "shell-c", "exec_dir": "/work/./", "timeout_seconds": 30}}
    add_receipt(ledger, duplicate, ordinal=3)
    recovery.record_shell_success(native_operation_identity(tool, duplicate))
    assert recovery.correction_kind(recovery.progress("read-evidence")) is None
    # Latest unresolved attempt cannot fall back to an older matching success.
    ledger.register(ShellExecutionAttempt("three", "f" * 32, "synthetic-sandbox", "shell-c", "d" * 64, AsyncMock()))
    assert not confirmed_shell_success(tool, duplicate, ledger)


def test_program_success_uses_source_identity_not_operation_or_filename(monkeypatch):
    make_agent(monkeypatch, [])
    recovery = TerminalProtocolRecovery()
    call = {"name": "program_run", "id": "one", "args": {
        "id": "shell-a", "exec_dir": "/work", "script_path": "/work/a.py", "argv": []}}
    assert recovery.correction_kind(recovery.progress("reads")) == "protocol"
    feedback = {"source_digest": "a" * 64, "returncode": 1, "operation_id": "1" * 32}
    recovery.record_program_success(call, feedback)
    assert recovery.correction_kind(recovery.progress("reads")) is None
    feedback["returncode"] = 0
    recovery.record_program_success(call, feedback)
    assert recovery.correction_kind(recovery.progress("reads")) == "protocol"
    call["args"]["script_path"] = "/work/renamed.py"
    feedback["operation_id"] = "2" * 32
    recovery.record_program_success(call, feedback)
    assert recovery.correction_kind(recovery.progress("reads")) is None
    feedback["source_digest"] = "b" * 64
    recovery.record_program_success(call, feedback)
    assert recovery.correction_kind(recovery.progress("reads")) == "protocol"


@pytest.mark.parametrize("new_prerequisites,blocked", [
    (None, True), ({"ready": False, "prerequisite_digest": "b" * 64}, True),
    ({"ready": True, "prerequisite_digest": "a" * 64}, True),
    ({"ready": True, "prerequisite_digest": "b" * 64}, False),
])
def test_program_replay_requires_proven_changed_source_not_missing_preflight(monkeypatch, new_prerequisites, blocked):
    make_agent(monkeypatch, [])
    toolkit = ShellToolkit(SimpleNamespace(id="synthetic-sandbox"))
    tool = toolkit.get_tool("program_run")
    call = {"name": "program_run", "id": "one", "args": {
        "id": "shell-a", "exec_dir": "/work", "script_path": "/work/a.py"}}
    old = {"ready": True, "prerequisite_digest": "a" * 64}
    recovery = TerminalProtocolRecovery()
    recovery.record_completed_write(native_operation_identity(tool, call, program_prerequisites=old), program_preparation=False)
    recovery.record_program_completion(native_operation_identity(tool, call), old)
    changed = {**call, "id": "two", "args": {**call["args"], "id": "new-shell", "argv": None, "timeout_seconds": 99}}
    assert recovery.would_replay_completed_write(
        native_operation_identity(tool, changed, program_prerequisites=new_prerequisites),
        program_base=native_operation_identity(tool, changed), prerequisites=new_prerequisites) is blocked


def test_unknown_tool_business_id_is_not_removed(monkeypatch):
    make_agent(monkeypatch, [])
    call = {"name": "program_run", "id": "one", "args": {"id": "business-a"}}
    assert native_operation_identity(object(), call) != native_operation_identity(object(),
        {**call, "args": {"id": "business-b"}})

@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_source,expected_launches", [(None, 1), ("a", 1), ("b", 2)])
async def test_agent_correction_replays_only_proven_new_program_source(monkeypatch, fresh_source, expected_launches):
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
    from app.domain.services.program_execution import program_command
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "synthetic-sandbox"
    state = {"source": "a"}

    async def preflight(*_args):
        source = state["source"]
        if source is None:
            return ToolResult(success=False)
        return ToolResult(success=True, data={"version": 1, "status": "ready", "ready": True,
            "prerequisite_digest": source * 64, "cwd": {"state": "ready"},
            "source": {"state": "ready", "source_digest": source * 64}})
    sandbox.program_preflight = preflight
    toolkit = ShellToolkit(sandbox)
    first = {"name": "program_run", "id": "program-one", "args": {
        "id": "process-one", "exec_dir": "/work", "script_path": "/work/a.py"}}
    second = {"name": "program_run", "id": "program-two", "args": {
        **first["args"], "id": "process-two", "argv": None, "timeout_seconds": 120}}

    def corrected(_agent):
        state["source"] = fresh_source
        return AIMessage(content="", tool_calls=[second])
    agent, _, requests, _ = make_agent(monkeypatch, [
        AIMessage(content="", tool_calls=[first]), AIMessage(content=DSML), corrected, AIMessage(content="Done")])
    agent.toolkits = [toolkit]
    launches = []

    async def adapter_dispatch(_tool, call):
        # Fake only the trusted adapter I/O boundary; production base, registry,
        # preflight/feedback identity checks and recovery decisions are real.
        launches.append(call["id"])
        args = call["args"]
        attempt = ShellExecutionAttempt(call["id"], f"{len(launches):032x}", sandbox.id, args["id"],
            shell_command_digest(args["exec_dir"], program_command(args["script_path"], args.get("argv") or [])), AsyncMock())
        agent._tool_execution_ledger.register(attempt)
        attempt.observe({"version": 1, "operation_id": attempt.operation_id,
            "command_digest": attempt.command_digest, "server_instance_id": "c" * 32,
            "state": "exited", "returncode": 0, "process_tree_quiescent": True})
        attempt.program_execution = {"version": 1, "script_path": args["script_path"],
            "source_digest": state["source"] * 64, "returncode": 0,
            "failure_fingerprint": None, "diagnostic": None, "output_truncated": False}
        return ToolMessage(content="Completed", tool_call_id=call["id"], name=call["name"],
            status="success", artifact=ToolResult(success=True, data={"returncode": 0}))
    agent.invoke_tool = adapter_dispatch
    events = [event async for event in agent.execute("Analyze")]
    assert len(launches) == expected_launches and len(requests) == 4
    assert events[-1].message == "Done"


@pytest.mark.asyncio
async def test_preparation_cannot_override_new_unknown_side_effect(monkeypatch):
    def unknown_after_preparation(agent):
        agent._tool_execution_ledger.record_unknown("unknown-write")
        return AIMessage(content=DSML)
    agent, _, requests, _ = make_agent(monkeypatch, [
        AIMessage(content=DSML), file_call(), unknown_after_preparation])
    sandbox, _ = core_files(agent)
    events = [event async for event in agent.execute("Analyze")]
    assert len(requests) == 3 and sandbox.file_write.await_count == 1
    assert agent.last_execution_outcome["code"] == "tool_execution_unknown"
    assert isinstance(events[-1], ErrorEvent)

@pytest.mark.asyncio
@pytest.mark.parametrize("completion_path", ["shell_wait", "reconcile"])
async def test_later_confirmed_success_refreshes_original_launch_progress(monkeypatch, completion_path):
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
    from app.domain.services.execution_evidence import register_shell_attempt, bind_shell_observation
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "synthetic-sandbox"
    holder = {}

    def receipt(attempt, completed=False):
        return {"version": 1, "operation_id": attempt.operation_id,
            "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
            "state": "exited" if completed else "running", "returncode": 0 if completed else None,
            "process_tree_quiescent": completed}

    async def launch(identifier, cwd, command):
        attempt = register_shell_attempt(sandbox, identifier, cwd, command)
        assert attempt is not None
        holder["attempt"] = attempt
        attempt.observe(receipt(attempt))
        return ToolResult(success=True, data={"status": "running", "returncode": None})

    async def wait(identifier, seconds):
        attempt = bind_shell_observation(sandbox, identifier)
        assert attempt is holder["attempt"]
        attempt.observe(receipt(attempt, True))
        return ToolResult(success=True, data={"status": "completed", "returncode": 0})

    async def status(identifier, operation):
        attempt = holder["attempt"]
        assert identifier == "shell-long" and operation == attempt.operation_id
        return ToolResult(success=True, data=receipt(attempt, completion_path == "reconcile"))

    sandbox.exec_command = AsyncMock(side_effect=launch)
    sandbox.wait_for_process = AsyncMock(side_effect=wait)
    sandbox.shell_operation_status = AsyncMock(side_effect=status)
    responses = [AIMessage(content=DSML), AIMessage(content="", tool_calls=[{
        "name": "shell_exec", "id": "launch-one", "args": {
            "id": "shell-long", "exec_dir": "/work", "command": "installed-tool --analysis"}}])]
    if completion_path == "shell_wait":
        responses.append(AIMessage(content="", tool_calls=[{
            "name": "shell_wait", "id": "wait-one", "args": {"id": "shell-long", "seconds": 1}}]))
    responses.extend([AIMessage(content=DSML), AIMessage(content="Completed")])
    agent, _, requests, _ = make_agent(monkeypatch, responses)
    agent.toolkits.append(ShellToolkit(sandbox))
    events = [event async for event in agent.execute("Analyze")]
    assert events[-1].message == "Completed" and len(requests) == len(responses)
    assert sandbox.exec_command.await_count == 1
    assert holder["attempt"].confirmed and holder["attempt"].receipt.returncode == 0
    assert sandbox.wait_for_process.await_count == (1 if completion_path == "shell_wait" else 0)

@pytest.mark.asyncio
async def test_same_correction_batch_rechecks_replay_after_pending_launch_finishes(monkeypatch):
    from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
    from app.domain.services.execution_evidence import register_shell_attempt
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "synthetic-sandbox"
    holder = {}

    async def launch(identifier, cwd, command):
        attempt = register_shell_attempt(sandbox, identifier, cwd, command)
        holder["attempt"] = attempt
        attempt.observe({"version": 1, "operation_id": attempt.operation_id,
            "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
            "state": "running", "returncode": None, "process_tree_quiescent": False})
        attempt.observed_at = 0  # Make the normal bounded observation due.
        return ToolResult(success=True, data={"status": "running", "returncode": None})

    async def status(identifier, operation):
        attempt = holder["attempt"]
        assert operation == attempt.operation_id
        return ToolResult(success=True, data={"version": 1, "operation_id": operation,
            "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
            "state": "exited", "returncode": 0, "process_tree_quiescent": True})

    sandbox.exec_command = AsyncMock(side_effect=launch)
    sandbox.shell_operation_status = AsyncMock(side_effect=status)
    calls = [{"name": "shell_exec", "id": f"launch-{i}", "args": {
        "id": f"shell-{i}", "exec_dir": "/work", "command": "installed-tool --write-output"}} for i in range(2)]
    agent, _, requests, _ = make_agent(monkeypatch, [AIMessage(content=DSML),
        AIMessage(content="", tool_calls=calls), AIMessage(content="Completed")])
    agent.toolkits.append(ShellToolkit(sandbox))
    events = [event async for event in agent.execute("Analyze")]
    assert sandbox.exec_command.await_count == 1 and sandbox.shell_operation_status.await_count == 1
    assert len(requests) == 3 and events[-1].message == "Completed"
    assert any(isinstance(event, ToolEvent) and isinstance(event.function_result, dict)
        and event.function_result.get("blocked_by_policy") == "tool_replay_suppressed" for event in events)
