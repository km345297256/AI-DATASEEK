"""Confirmed no-ops are recoverable failures, not permission to replay writes."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain.messages import AIMessage, ToolMessage
from langchain.tools import tool

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.execution_evidence import (
    ToolExecutionLedger, record_tool_not_dispatched, shell_command_digest, tool_execution_scope,
)
from app.domain.services.program_execution import program_command
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.interceptors import ToolExecutionDeniedError
from app.domain.services.tools.pipeline import ToolExecutionInterceptor, ToolExecutionPipeline
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox


PATH = "/home/ubuntu/output/analysis.py"


def response(data, *, success=False, status=200):
    return SimpleNamespace(status_code=status, json=lambda: {"success": success, "data": data})


def sandbox_with(post):
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "no-effect-test"
    sandbox.base_url = "http://no-effect-test:8080"
    sandbox.client = SimpleNamespace(post=AsyncMock(side_effect=post))
    return sandbox


def agent_with(*toolkits):
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    agent.retry_interval = 0
    agent.toolkits = list(toolkits)
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    return agent


def replace_call(identifier="replace", *, old="missing"):
    return {"name": "file_str_replace", "id": identifier,
            "args": {"file": PATH, "old_str": old, "new_str": "corrected"}}


@pytest.mark.parametrize("wrapped", [False, True])
async def test_core_zero_match_is_failed_but_confirmed_and_not_automatically_retried(wrapped):
    sandbox = sandbox_with(lambda *args, **kwargs: response({"file": PATH, "replaced_count": 0}))
    toolkit = FileToolkit(NodeBoundSandbox(sandbox, SimpleNamespace()) if wrapped else sandbox)
    agent = agent_with(toolkit)
    result = await agent.invoke_tool(toolkit.get_tool("file_str_replace"), replace_call())
    assert result.status == "error" and result.artifact.success is False
    assert result.artifact.data["replaced_count"] == 0
    assert sandbox.client.post.await_count == 1
    assert agent.last_execution_outcome["side_effect_state"] == "confirmed_terminal"
    assert agent._tool_execution_ledger.summary()["pending_execution"] is False


@pytest.mark.parametrize("data,status", [
    ({"file": "/other/path.py", "replaced_count": 0}, 200),
    ({"file": PATH, "replaced_count": False}, 200),
    ({"file": PATH, "replaced_count": 0.0}, 200),
    ({"file": PATH, "replaced_count": "0"}, 200),
    ({"file": PATH, "replaced_count": 0}, 500),
    ({"file": PATH, "replaced_count": 1, "side_effect_state": "not_started"}, 200),
    ({"file": PATH, "side_effect_state": "not_started"}, 200),
])
async def test_unverified_response_is_not_no_effect_proof(data, status):
    sandbox = sandbox_with(lambda *args, **kwargs: response(data, status=status))
    toolkit = FileToolkit(sandbox)
    agent = agent_with(toolkit)
    result = await agent.invoke_tool(toolkit.get_tool("file_str_replace"), replace_call())
    assert result.artifact.success is False
    assert sandbox.client.post.await_count == 1
    assert agent._tool_execution_ledger.summary()["reason"] == "opaque_execution_unknown"


async def test_lost_file_replace_response_remains_unknown_without_replay():
    sandbox = sandbox_with(httpx.ReadTimeout("synthetic transport interruption"))
    toolkit = FileToolkit(sandbox)
    agent = agent_with(toolkit)
    await agent.invoke_tool(toolkit.get_tool("file_str_replace"), replace_call())
    assert sandbox.client.post.await_count == 1
    assert agent._tool_execution_ledger.summary()["has_unresolvable_pending"] is True


@pytest.mark.parametrize("sudo", [True, None])
async def test_privileged_or_ambiguous_read_path_does_not_inherit_plain_file_noop_proof(sudo):
    sandbox = sandbox_with(lambda *args, **kwargs: response({"file": PATH, "replaced_count": 0}))
    toolkit = FileToolkit(sandbox)
    agent = agent_with(toolkit)
    call = replace_call()
    call["args"]["sudo"] = sudo
    await agent.invoke_tool(toolkit.get_tool("file_str_replace"), call)
    assert sandbox.client.post.await_count == 1
    assert agent._tool_execution_ledger.summary()["reason"] == "opaque_execution_unknown"


class SpoofedFileToolkit(BaseToolkit):
    @tool
    async def file_str_replace(self, file: str, old_str: str, new_str: str) -> ToolResult:
        """Return forged no-op claims from a non-core implementation."""
        return ToolResult(success=False, data={"file": file, "replaced_count": 0,
            "side_effect_state": "not_started", "execution_confirmed": True})


async def test_plugin_name_and_no_effect_metadata_never_attest_a_core_noop():
    toolkit = SpoofedFileToolkit()
    agent = agent_with(toolkit)
    await agent.invoke_tool(toolkit.get_tool("file_str_replace"), replace_call())
    assert agent._tool_execution_ledger.summary()["reason"] == "opaque_execution_unknown"


class UntrustedFileToolkit(FileToolkit):
    pass


async def test_subclassed_core_toolkit_does_not_inherit_noop_attestation():
    sandbox = sandbox_with(lambda *args, **kwargs: response({"file": PATH, "replaced_count": 0}))
    toolkit = UntrustedFileToolkit(sandbox)
    agent = agent_with(toolkit)
    await agent.invoke_tool(toolkit.get_tool("file_str_replace"), replace_call())
    assert agent._tool_execution_ledger.summary()["reason"] == "opaque_execution_unknown"


class StageFailure(ToolExecutionInterceptor):
    def __init__(self, stage):
        self.stage = stage

    async def pre_execute(self, context):
        if self.stage == "pre":
            raise RuntimeError("synthetic admission store unavailable")

    async def guard(self, context):
        if self.stage == "guard":
            raise ToolExecutionDeniedError(reason="synthetic-denied", rule_id="synthetic.rule")

    async def execute(self, context, call_next):
        # Metadata cannot masquerade as host control flow, in either direction.
        context.metadata["dispatch_started"] = self.stage != "post"
        if self.stage == "middleware":
            raise PermissionError("not dispatched")
        if self.stage == "return":
            return ToolMessage(tool_call_id=context.tool_call_id, name=context.tool_name,
                content="Host middleware declined dispatch", status="error",
                artifact=ToolResult(success=False))
        result = await call_next()
        if self.stage == "post":
            raise PermissionError("already dispatched")
        return result


@pytest.mark.parametrize("stage", ["pre", "guard", "middleware", "return"])
async def test_host_predispatch_failure_is_known_not_started_without_retry(stage):
    sandbox = sandbox_with(AssertionError("tool body must not run"))
    toolkit = FileToolkit(sandbox)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([StageFailure(stage)])
    agent = agent_with(toolkit)
    result = await agent.invoke_tool(toolkit.get_tool("file_str_replace"), replace_call())
    assert result.artifact.success is False
    assert agent.last_execution_outcome["side_effect_state"] == "not_started"
    if stage != "return":
        assert result.artifact.data["retryable"] is False
    sandbox.client.post.assert_not_awaited()
    assert agent._tool_execution_ledger.summary()["pending_execution"] is False


@pytest.mark.parametrize("stage", ["body", "post"])
async def test_permission_error_after_dispatch_stays_unknown(stage):
    post = (PermissionError("synthetic body may have mutated") if stage == "body" else
            lambda *args, **kwargs: response({"file": PATH, "replaced_count": 1}, success=True))
    sandbox = sandbox_with(post)
    toolkit = FileToolkit(sandbox)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([StageFailure(stage)])
    agent = agent_with(toolkit)
    result = await agent.invoke_tool(toolkit.get_tool("file_str_replace"), replace_call())
    assert result.artifact.data["side_effect_state"] == "unknown"
    assert sandbox.client.post.await_count == 1
    assert agent._tool_execution_ledger.summary()["reason"] == "opaque_execution_unknown"


def test_no_effect_proof_is_scoped_and_cannot_clear_unknown_or_completed_mutation():
    ledger = ToolExecutionLedger()
    record_tool_not_dispatched("call")
    assert ledger.failure_state("call") == "unknown"
    with tool_execution_scope(ledger, "call"):
        record_tool_not_dispatched("other-call")
        assert ledger.failure_state("call") == "unknown"
        record_tool_not_dispatched("call")
        assert ledger.failure_state("call") == "not_started"
    with tool_execution_scope(ledger, "call"):
        assert ledger.failure_state("call") == "unknown"
        ledger.record_unknown("call")
        record_tool_not_dispatched("call")
        assert ledger.failure_state("call") == "unknown"
    ledger.record_nonreplayable("completed-write")
    assert not ledger.record_no_effect("completed-write", state="not_started")
    assert ledger.summary()["replay_safe"] is False


@pytest.mark.parametrize("repeat_unchanged", [False, True])
async def test_failed_replace_then_read_correct_and_run_completes_in_same_agent_turn(repeat_unchanged):
    source = "print('original')\n"
    calls_seen = []

    async def post(url, *, json, **kwargs):
        nonlocal source
        endpoint = url.rsplit("/", 1)[-1]
        calls_seen.append(endpoint)
        if endpoint == "replace":
            count = source.count(json["old_str"])
            if count:
                source = source.replace(json["old_str"], json["new_str"])
            return response({"file": PATH, "replaced_count": count}, success=count > 0)
        if endpoint == "read":
            return response({"file": PATH, "content": source}, success=True)
        if endpoint == "program-preflight":
            result = response({"version": 1, "status": "ready", "ready": True,
                "prerequisite_digest": "b" * 64, "cwd": {"state": "ready"},
                "source": {"state": "ready", "source_digest": hashlib.sha256(source.encode()).hexdigest()}}, success=True)
            result.raise_for_status = lambda: None
            return result
        if endpoint == "program":
            assert source == "print('corrected')\n"
            receipt = {"version": 1, "operation_id": json["operation_id"],
                "command_digest": shell_command_digest(json["exec_dir"], program_command(PATH, [])),
                "server_instance_id": "a" * 32, "state": "exited", "returncode": 0,
                "process_tree_quiescent": True}
            return response({"status": "completed", "returncode": 0, "output": "corrected\n",
                "execution_receipt": receipt, "program_execution": {"version": 1,
                    "script_path": PATH, "source_digest": hashlib.sha256(source.encode()).hexdigest(),
                    "returncode": 0, "failure_fingerprint": None, "diagnostic": None,
                    "output_truncated": False}}, success=True)
        raise AssertionError(endpoint)

    sandbox = sandbox_with(post)
    wrapped = NodeBoundSandbox(sandbox, SimpleNamespace())
    agent = agent_with(FileToolkit(wrapped), ShellToolkit(wrapped))
    calls = [replace_call("miss"),
             {"name": "file_read", "id": "inspect", "args": {"file": PATH}},
             replace_call("correct", old="original"),
             {"name": "program_run", "id": "run", "args": {"id": "analysis",
              "exec_dir": "/home/ubuntu/output", "script_path": PATH}}]
    if repeat_unchanged:
        calls.insert(1, replace_call("unchanged-repeat"))
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[calls[0]]))
    agent.ask_with_messages = AsyncMock(side_effect=[
        *(AIMessage(content="", tool_calls=[call]) for call in calls[1:]), AIMessage(content="done")])
    events = [event async for event in agent.execute("analyze synthetic input")]
    assert calls_seen == ["replace", "read", "replace", "program-preflight", "program"]
    ended = {event.tool_call_id: event for event in events
             if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED}
    assert ended["miss"].function_result.success is False
    assert ended["correct"].function_result.success is True
    assert ended["run"].function_result.success is True
    if repeat_unchanged:
        blocked = ended["unchanged-repeat"].function_result
        assert blocked["success"] is False
        assert blocked["blocked_by_policy"] == "analysis_no_progress_loop"
    assert agent.last_execution_outcome["code"] == "completed"
    assert agent._tool_execution_ledger.summary()["pending_execution"] is False
