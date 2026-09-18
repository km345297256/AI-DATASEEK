"""Execution-loop regressions with scripted model responses, no model/network."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, ToolMessage

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.analysis_recovery import AnalysisRecoveryContext, analysis_recovery_scope


PROGRAM = "/home/ubuntu/output/profile.py"
SOURCE = "/home/ubuntu/datasets/input.csv"


def invocation(name, identifier, **args):
    return {"name": name, "id": identifier, "args": args}


def scripted_agent(calls):
    agent = object.__new__(BaseAgent)
    agent.max_retries = 0
    agent.get_tool = lambda name: SimpleNamespace(name=name, toolkit=SimpleNamespace(name="test"))
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    responses = [AIMessage(content="", tool_calls=[call]) for call in calls]
    agent.ask = AsyncMock(return_value=responses[0])
    agent.ask_with_messages = AsyncMock(side_effect=[*responses[1:], AIMessage(content="verified result")])
    return agent


@pytest.mark.asyncio
async def test_saved_python_pipeline_is_redirected_before_dispatch():
    agent = scripted_agent([
        invocation("shell_run", "masked", exec_dir="/home/ubuntu/output",
                   command="python3 profile.py 2>&1 | tail -80 && ls -l"),
    ])
    agent.invoke_tool = AsyncMock(side_effect=AssertionError("must not dispatch masked pipeline"))
    events = [event async for event in agent.execute("analyze")]
    agent.invoke_tool.assert_not_awaited()
    result = next(event.function_result for event in events if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED)
    assert result["blocked_by_policy"] == "program_execution_required"
    assert "program_run" in result["message"]
    assert agent.last_execution_outcome["tool_batch_limit"] is None


@pytest.mark.asyncio
async def test_repeated_program_error_redirects_to_source_diagnosis_then_allows_repair(monkeypatch):
    calls = [
        invocation("file_write", "write", file=PROGRAM, content="first program"),
        invocation("program_run", "run-1", script_path=PROGRAM),
        invocation("file_str_replace", "patch-1", file=PROGRAM, old_str="first", new_str="second"),
        invocation("program_run", "run-2", script_path=PROGRAM),
        invocation("file_read", "read-own", file=PROGRAM),
        invocation("file_write", "blind-rewrite", file=PROGRAM, content="third program"),
        invocation("file_read", "diagnose", file=SOURCE, start_line=10, end_line=15),
        invocation("file_str_replace", "informed-patch", file=PROGRAM, old_str="second", new_str="fixed"),
        invocation("program_run", "run-3", script_path=PROGRAM),
    ]
    agent = scripted_agent(calls)
    dispatched = []

    async def invoke(_tool, call):
        dispatched.append(call["id"])
        success = call["id"] not in {"run-1", "run-2"}
        return ToolMessage(tool_call_id=call["id"], name=call["name"], content=f"observed-{call['id']}",
                           artifact=ToolResult(success=success))

    def trusted_feedback(_tool, call, _result, _ledger):
        return {"script_path": PROGRAM, "operation_id": call["id"],
                "source_digest": {"run-1": "a", "run-2": "b", "run-3": "c"}[call["id"]] * 64,
                "returncode": 0 if call["id"] == "run-3" else 1,
                "failure_fingerprint": "normalized-real-error"}

    monkeypatch.setattr("app.domain.services.agents.base.resolved_program_path",
                        lambda tool, call: PROGRAM if tool.name == "program_run" else None)
    monkeypatch.setattr("app.domain.services.agents.base.trusted_program_execution_feedback", trusted_feedback)
    monkeypatch.setattr("app.domain.services.agents.base.resolved_tool_is_read_only", lambda tool: tool.name == "file_read")
    agent.invoke_tool = invoke
    recovery = AnalysisRecoveryContext(review=AsyncMock())
    recovery.progress.read_scope_paths = frozenset({SOURCE})
    with analysis_recovery_scope(recovery):
        events = [event async for event in agent.execute("analyze original dataset")]
    assert dispatched == [call["id"] for call in calls if call["id"] != "blind-rewrite"]
    blocked = [event for event in events if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED
               and event.tool_call_id == "blind-rewrite"]
    assert len(blocked) == 1 and "original input" in blocked[0].function_result["message"]
    assert agent.last_execution_outcome["code"] == "completed"
    assert not recovery.progress.should_stop
    assert len(recovery.progress.evidence) == 1  # Source diagnosis, not the own-script reread.


@pytest.mark.asyncio
async def test_plain_shell_inspection_cannot_clear_program_draft_state():
    calls = [
        invocation("file_write", "write-1", file=PROGRAM, content="first"),
        invocation("file_write", "write-2", file=PROGRAM, content="second"),
        invocation("shell_run", "listing", exec_dir="/home/ubuntu/output", command="ls -l profile.py"),
        invocation("file_write", "write-3", file=PROGRAM, content="third"),
    ]
    agent = scripted_agent(calls)
    dispatched = []

    async def invoke(_tool, call):
        dispatched.append(call["id"])
        return ToolMessage(tool_call_id=call["id"], name=call["name"], content="ok",
                           artifact=ToolResult(success=True))

    agent.invoke_tool = invoke
    _ = [event async for event in agent.execute("analyze")]
    assert dispatched == ["write-1", "write-2", "listing"]


@pytest.mark.asyncio
async def test_prior_program_confirmation_cannot_authorize_missing_source_retry(monkeypatch):
    calls = [invocation("program_run", identifier, script_path=PROGRAM)
             for identifier in ("confirmed", "source-gone", "retry-source-gone")]
    agent = scripted_agent(calls)
    dispatched = []

    async def invoke(_tool, call):
        dispatched.append(call["id"])
        return ToolMessage(tool_call_id=call["id"], name="program_run", content="source missing",
                           artifact=ToolResult(success=call["id"] == "confirmed"))

    def feedback(_tool, call, _result, _ledger):
        if call["id"] != "confirmed":
            return None
        return {"script_path": PROGRAM, "operation_id": "prior", "source_digest": "a" * 64, "returncode": 0}

    monkeypatch.setattr("app.domain.services.agents.base.resolved_program_path", lambda *args: PROGRAM)
    monkeypatch.setattr("app.domain.services.agents.base.trusted_program_execution_feedback", feedback)
    agent.invoke_tool = invoke
    events = [event async for event in agent.execute("analyze")]
    assert dispatched == ["confirmed", "source-gone"]
    blocked = next(event for event in events if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED
                   and event.tool_call_id == "retry-source-gone")
    assert blocked.function_result["success"] is False
    assert "already failed" in blocked.function_result["message"]


@pytest.mark.asyncio
async def test_trusted_code_read_opens_only_one_same_error_correction_trial(monkeypatch):
    calls = [
        invocation("program_run", "run-1", script_path=PROGRAM),
        invocation("file_str_replace", "patch-1", file=PROGRAM, old_str="bad", new_str="different"),
        invocation("program_run", "run-2", script_path=PROGRAM),
        invocation("file_read", "diagnostic-1", file=PROGRAM),
        invocation("file_str_replace", "patch-2", file=PROGRAM, old_str="different", new_str="almost"),
        invocation("program_run", "run-3", script_path=PROGRAM),
        invocation("file_read", "diagnostic-repeat", file=PROGRAM),
        invocation("file_str_replace", "blind-patch", file=PROGRAM, old_str="almost", new_str="still blind"),
        invocation("file_read", "diagnostic-new", file=PROGRAM, start_line=10, end_line=20),
        invocation("file_str_replace", "informed-patch", file=PROGRAM, old_str="almost", new_str="fixed"),
        invocation("program_run", "run-4", script_path=PROGRAM),
    ]
    agent = scripted_agent(calls)
    dispatched = []

    async def invoke(_tool, call):
        dispatched.append(call["id"])
        return ToolMessage(tool_call_id=call["id"], name=call["name"], content=call["id"],
                           artifact=ToolResult(success=call["id"] not in {"run-1", "run-2", "run-3"}))

    def feedback(_tool, call, _result, _ledger):
        return {"script_path": PROGRAM, "operation_id": call["id"], "source_digest": call["id"],
                "returncode": 0 if call["id"] == "run-4" else 1, "failure_fingerprint": "same-name-error",
                "diagnostic": {"exception_type": "NameError"}}

    monkeypatch.setattr("app.domain.services.agents.base.resolved_program_path", lambda *args: PROGRAM)
    monkeypatch.setattr("app.domain.services.agents.base.trusted_program_execution_feedback", feedback)
    monkeypatch.setattr("app.domain.services.agents.base.resolved_tool_is_read_only", lambda tool: tool.name == "file_read")
    monkeypatch.setattr("app.domain.services.agents.base.program_diagnostic_read_digest",
                        lambda tool, call, result: (
                            "fresh-code-section" if call["id"] == "diagnostic-new" else "same-code-section"
                        ) if call["name"] == "file_read" else None)
    agent.invoke_tool = invoke
    events = [event async for event in agent.execute("analyze")]
    assert dispatched == [call["id"] for call in calls if call["id"] != "blind-patch"]
    assert not agent._analysis_progress.evidence
    assert agent.last_execution_outcome["code"] == "completed"
    assert any(isinstance(event, ToolEvent) and event.tool_call_id == "blind-patch"
               and event.status == ToolStatus.CALLED and event.function_result["success"] is False for event in events)
