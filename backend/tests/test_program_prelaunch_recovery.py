"""Real core wrapper/adapter/ledger with deterministic model and HTTP boundary."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.analysis_progress import AnalysisProgressGuard
from app.domain.services.execution_evidence import ShellExecutionAttempt, ToolExecutionLedger, shell_command_digest
from app.domain.services.program_execution import (
    program_command, trusted_program_prelaunch_failure, trusted_program_prerequisites, program_preparation_failure,
)
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox


PATH = "/home/ubuntu/output/work/analysis.py"
DIRECTORY = "/home/ubuntu/output/work"


def make_sandbox():
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "test-sandbox"
    sandbox.base_url = "http://test-sandbox:8080"
    return sandbox


def run_call(identifier="run", *, path=PATH, directory=DIRECTORY):
    return {"name": "program_run", "id": identifier, "args": {
        "id": "analysis", "exec_dir": directory, "script_path": path, "argv": ["--validate-only"],
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", [DIRECTORY, "/home/ubuntu/scripts", "/tmp/analysis-work/programs"])
@pytest.mark.parametrize("initial_problem", ["missing", "syntax"])
async def test_missing_prerequisite_then_real_source_creation_recovers_through_agent(directory, initial_problem):
    sandbox = make_sandbox()
    path = directory + "/analysis.py"
    source = "print(\n" if initial_problem == "syntax" else None
    directory_exists = initial_problem == "syntax"
    launches, receipts = [], {}

    async def post(url, *, json, **kwargs):
        nonlocal source, directory_exists
        if url.endswith("/shell/program-preflight"):
            assert json == {"exec_dir": directory, "script_path": path}
            syntax_valid = source is not None and source != "print(\n"
            ready = directory_exists and syntax_valid
            data = {"version": 1, "status": "ready" if ready else "blocked", "ready": ready,
                    "prerequisite_digest": ("b" if ready else "a") * 64,
                    "cwd": {"state": "ready" if directory_exists else "missing"},
                    "source": {"state": "ready" if source is not None else "missing",
                               "source_digest": hashlib.sha256(source.encode()).hexdigest() if source else None},
                    "syntax": {"state": "ready" if syntax_valid else "invalid" if source else "not_checked",
                               "exception_type": "SyntaxError" if source and not syntax_valid else None,
                               "line": 1 if source and not syntax_valid else None,
                               "offset": 6 if source and not syntax_valid else None}}
            body = {"success": True, "data": data}
        elif url.endswith("/shell/exec"):
            assert json["command"] == "mkdir -p " + directory
            directory_exists = True
            receipt = {"version": 1, "operation_id": json["operation_id"],
                       "command_digest": shell_command_digest(json["exec_dir"], json["command"]),
                       "server_instance_id": "a" * 32, "state": "exited", "returncode": 0,
                       "process_tree_quiescent": True}
            receipts[json["operation_id"]] = receipt
            body = {"success": True, "data": {"status": "completed", "returncode": 0,
                                              "execution_receipt": receipt}}
        elif url.endswith("/file/write"):
            assert directory_exists and json["file"] == path
            source = json["content"]
            body = {"success": True, "data": {"file": path, "bytes_written": len(source)}}
        elif url.endswith("/shell/program"):
            launches.append(json)
            receipt = {"version": 1, "operation_id": json["operation_id"],
                       "command_digest": shell_command_digest(json["exec_dir"], program_command(json["script_path"], json["args"])),
                       "server_instance_id": "a" * 32, "state": "not_started" if source is None else "exited",
                       "returncode": None if source is None else 0, "process_tree_quiescent": True}
            receipts[json["operation_id"]] = receipt
            if source is None:
                # Match the real HTTP error: terminal receipt arrives only
                # through later independent reconciliation, not public prose.
                body = {"success": False, "message": "Execution directory does not exist", "data": None}
            else:
                body = {"success": True, "data": {"status": "completed", "returncode": 0, "output": "validated",
                    "execution_receipt": receipt, "program_execution": {"version": 1, "script_path": path,
                    "source_digest": hashlib.sha256(source.encode()).hexdigest(), "returncode": 0,
                    "failure_fingerprint": None, "diagnostic": None, "output_truncated": False}}}
        elif url.endswith("/shell/operation-status"):
            body = {"success": True, "data": receipts[json["operation_id"]]}
        else:
            raise AssertionError(url)
        return SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

    sandbox.client = SimpleNamespace(post=AsyncMock(side_effect=post))
    wrapped = NodeBoundSandbox(sandbox, SimpleNamespace(node_id="local"))
    agent = object.__new__(BaseAgent)
    agent.max_retries = 0
    agent.toolkits = [ShellToolkit(wrapped), FileToolkit(wrapped)]
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    calls = [run_call("before-source", path=path, directory=directory),
             {"name": "shell_run", "id": "wrong-shell-runner", "args": {
                 "id": "wrong", "exec_dir": "/home/ubuntu", "command": "ls -l " + path + "; python3 " + path}},
             run_call("retry-without-preparation", path=path, directory=directory),
             {"name": "shell_run", "id": "create-directory", "args": {
                 "id": "mkdir", "exec_dir": "/home/ubuntu", "command": "mkdir -p " + directory}},
             *([run_call("directory-ready-source-missing", path=path, directory=directory),
                {"name": "file_write", "id": "write-incomplete-source", "args": {
                    "file": path, "content": "print(\n"}},
                run_call("source-ready-syntax-invalid", path=path, directory=directory)]
               if initial_problem == "missing" else []),
             {"name": "file_write", "id": "create-source", "args": {
                 "file": path, "content": "print('validated')\n"}},
             run_call("after-source", path=path, directory=directory)]
    agent._add_to_memory = AsyncMock()  # Model/memory I/O is outside this recovery fixture.
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[calls[0]]))
    agent.ask_with_messages = AsyncMock(side_effect=[
        *(AIMessage(content="", tool_calls=[call]) for call in calls[1:]), AIMessage(content="done"),
    ])
    events = [event async for event in agent.execute("analyze")]
    assert len(launches) == 1  # Missing prerequisites never reach the launch endpoint.
    before = next(event for event in events if isinstance(event, ToolEvent)
                  and event.tool_call_id == "before-source" and event.status == ToolStatus.CALLED)
    assert before.dispatch_status == "not_started"
    assert before.function_result["error_code"] == ("cwd_missing" if initial_problem == "missing" else "script_syntax_error")
    assert {issue["code"] for issue in before.function_result["program_preflight"]["issues"]} == (
        {"cwd_missing", "script_missing"} if initial_problem == "missing" else {"script_syntax_error"})
    retry = next(event for event in events if isinstance(event, ToolEvent)
                 and event.tool_call_id == "retry-without-preparation" and event.status == ToolStatus.CALLED)
    assert "DIAGNOSTIC RECOVERY" in retry.function_result["message"]
    if initial_problem == "missing":
        observed = {event.tool_call_id: event.function_result for event in events if isinstance(event, ToolEvent)
                    and event.status == ToolStatus.CALLED}
        assert observed["directory-ready-source-missing"]["error_code"] == "script_missing"
        assert observed["source-ready-syntax-invalid"]["error_code"] == "script_syntax_error"
    assert all(event.dispatch_status == "not_started" for event in events if isinstance(event, ToolEvent)
               and event.tool_call_id in {"before-source", "wrong-shell-runner", "retry-without-preparation"}
               and event.status == ToolStatus.CALLED)
    assert not agent._analysis_progress.should_stop
    assert agent._tool_execution_ledger.summary()["pending_execution"] is False
    assert agent.last_execution_outcome["code"] == "completed"
    completed = next(event for event in events if isinstance(event, ToolEvent)
                     and event.tool_call_id == "after-source" and event.status == ToolStatus.CALLED)
    assert completed.function_result.success is True


def test_only_newest_equivalent_private_launch_can_prove_not_started():
    sandbox = make_sandbox()
    tool = ShellToolkit(NodeBoundSandbox(sandbox, SimpleNamespace())).get_tool("program_run")
    ledger = ToolExecutionLedger()
    call = run_call()
    command = shell_command_digest(DIRECTORY, program_command(PATH, ["--validate-only"]))
    first = ShellExecutionAttempt("original", "a" * 32, sandbox.id, "analysis", command, AsyncMock())
    ledger.register(first)
    first.observe({"version": 1, "operation_id": first.operation_id, "command_digest": command,
                   "server_instance_id": "b" * 32, "state": "not_started", "returncode": None,
                   "process_tree_quiescent": True})
    assert trusted_program_prelaunch_failure(tool, call, ledger)["tool_call_id"] == "original"
    ledger.register(ShellExecutionAttempt("later-unknown", "c" * 32, sandbox.id, "analysis", command, AsyncMock()))
    assert trusted_program_prelaunch_failure(tool, call, ledger) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["unavailable", "partial", "bad_digest", "ready_mismatch", "changed", "error", "version"])
async def test_unverified_or_partial_source_observations_never_unlock(invalid):
    sandbox = make_sandbox()
    data = {"version": 1, "status": "ready", "ready": True, "prerequisite_digest": "b" * 64,
            "cwd": {"state": "ready"}, "source": {"state": "ready", "source_digest": "a" * 64},
            "syntax": {"state": "ready"}}
    if invalid == "unavailable": data["status"] = "unavailable"
    if invalid == "partial": data.pop("source")
    if invalid == "bad_digest": data["prerequisite_digest"] = "not a hash"
    if invalid == "ready_mismatch": data["ready"] = False
    if invalid == "changed": data["source"]["state"] = "unavailable"
    if invalid == "version": data["version"] = 99
    sandbox.program_preflight = AsyncMock(return_value=ToolResult(success=invalid != "error", data=data))
    tool = ShellToolkit(sandbox).get_tool("program_run")
    assert await trusted_program_prerequisites(tool, run_call()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["not_started", "running", "unknown", "later_unknown"])
@pytest.mark.parametrize("changed", [False, True])
async def test_scripts_probe_changes_need_exact_private_not_started_receipt(state, changed):
    path, directory = "/home/ubuntu/scripts/analysis.py", "/home/ubuntu/scripts"
    sandbox = make_sandbox()
    tool = ShellToolkit(NodeBoundSandbox(sandbox, SimpleNamespace())).get_tool("program_run")
    initial = run_call("failed-launch", path=path, directory=directory)
    retry = run_call("retry-launch", path=path, directory=directory)
    ledger, guard = ToolExecutionLedger(), AnalysisProgressGuard()

    def observation(digest):
        return ToolResult(success=True, data={"version": 1, "status": "ready", "ready": True,
            "prerequisite_digest": digest * 64, "cwd": {"state": "ready"},
            "source": {"state": "ready", "source_digest": digest * 64}, "syntax": {"state": "ready"}})

    sandbox.program_preflight = AsyncMock(side_effect=[observation("a"), observation("b" if changed else "a")])
    before = await trusted_program_prerequisites(tool, initial)
    assert before is not None
    guard.record_program_prerequisites(path=path, **before)
    guard.record(initial, succeeded=False, program_path=path)
    command = shell_command_digest(directory, program_command(path, initial["args"]["argv"]))
    attempt = ShellExecutionAttempt(initial["id"], "a" * 32, sandbox.id, "analysis", command, AsyncMock())
    ledger.register(attempt)
    if state != "unknown":
        attempt.observe({"version": 1, "operation_id": attempt.operation_id, "command_digest": command,
                         "server_instance_id": "b" * 32,
                         "state": "not_started" if state in {"not_started", "later_unknown"} else "running",
                         "returncode": None, "process_tree_quiescent": state != "running"})
    if state == "later_unknown":
        ledger.register(ShellExecutionAttempt("newer-launch", "c" * 32, sandbox.id, "analysis", command, AsyncMock()))
    guard.record({"name": "file_write", "id": "save", "args": {"file": path, "content": "changed"}}, succeeded=True)
    assert guard.before_call(retry, program_path=path)  # A public write success alone is not authority.
    after = await trusted_program_prerequisites(tool, retry)
    guard.record_program_prerequisites(path=path, **after)
    receipt = trusted_program_prelaunch_failure(tool, retry, ledger)
    if receipt:
        guard.record_program_prelaunch_failure(call={**retry, "id": receipt["tool_call_id"]}, path=path,
                                               operation_id=receipt["operation_id"])
    assert (guard.before_call(retry, program_path=path) is None) is (state == "not_started" and changed)
    assert not guard.evidence  # Availability is not scientific evidence.


@pytest.mark.asyncio
@pytest.mark.parametrize("path,cwd", [("relative.py", "/home/ubuntu"),
    ("/home/ubuntu/scripts/../secret.py", "/home/ubuntu"),
    ("/home/ubuntu/scripts/a.py", "relative"),
    ("/home/ubuntu/scripts/a.py", "/home/ubuntu/../tmp"),
    ("/home/ubuntu/scripts/a.py", "/home/ubuntu/\n"),
    ("/home/ubuntu/scripts/a\\b.py", "/home/ubuntu"),
    ("/home/ubuntu/scripts/a.sh", "/home/ubuntu")])
async def test_program_preflight_never_probes_malformed_paths(path, cwd):
    sandbox = make_sandbox()
    sandbox.program_preflight = AsyncMock(side_effect=AssertionError("No unsafe path probe"))
    tool = ShellToolkit(sandbox).get_tool("program_run")
    assert await trusted_program_prerequisites(tool, run_call(path=path, directory=cwd)) is None
    sandbox.program_preflight.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cwd_state,source_state,syntax_state,expected", [
    ("ready", "missing", "not_checked", "script_missing"),
    ("missing", "ready", "ready", "cwd_missing"),
    ("not_directory", "ready", "ready", "cwd_not_directory"),
    ("not_accessible", "ready", "ready", "cwd_not_accessible"),
    ("ready", "not_regular", "not_checked", "script_not_regular"),
    ("ready", "not_readable", "not_checked", "script_not_readable"),
    ("ready", "too_large", "not_checked", "script_too_large"),
    ("ready", "ready", "invalid", "script_syntax_error"),
])
async def test_preflight_produces_safe_structured_action_without_launch(cwd_state, source_state, syntax_state, expected):
    sandbox = make_sandbox()
    syntax = {"state": syntax_state, "exception_type": "SyntaxError" if syntax_state == "invalid" else None,
              "line": 3 if syntax_state == "invalid" else None, "offset": 1 if syntax_state == "invalid" else None,
              "source_line": "NEVER EXPOSE THIS", "message": "untrusted error text"}
    sandbox.program_preflight = AsyncMock(return_value=ToolResult(success=True, data={
        "version": 1, "status": "blocked", "ready": False, "prerequisite_digest": "a" * 64,
        "cwd": {"state": cwd_state}, "source": {"state": source_state, "source_digest": "b" * 64},
        "syntax": syntax}))
    tool = ShellToolkit(sandbox).get_tool("program_run")
    observation = await trusted_program_prerequisites(tool, run_call())
    result = program_preparation_failure(tool, run_call(), observation)
    assert result["error_code"] == expected
    assert result["side_effect_state"] == "not_started"
    assert result["program_preflight"]["issues"][0]["action"]
    assert "NEVER EXPOSE" not in str(result) and "untrusted error text" not in str(result)
    assert "source_digest" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("syntax", [None, {"state": "ready", "line": True},
    {"state": "not_checked"}, {"state": "ready", "exception_type": "anything"},
    {"state": "invalid", "offset": 0}])
async def test_unverifiable_syntax_probe_cannot_authorize_execution(syntax):
    sandbox = make_sandbox()
    sandbox.program_preflight = AsyncMock(return_value=ToolResult(success=True, data={
        "version": 1, "status": "ready", "ready": True, "prerequisite_digest": "a" * 64,
        "cwd": {"state": "ready"}, "source": {"state": "ready", "source_digest": "b" * 64},
        "syntax": syntax}))
    tool = ShellToolkit(sandbox).get_tool("program_run")
    observation = await trusted_program_prerequisites(tool, run_call())
    assert observation is None
    assert program_preparation_failure(tool, run_call(), observation)["error_code"] == "program_preflight_unavailable"


@pytest.mark.asyncio
async def test_repeating_unchanged_preparation_after_diagnostic_checkpoint_does_not_spin():
    sandbox = make_sandbox()
    sandbox.program_preflight = AsyncMock(return_value=ToolResult(success=True, data={
        "version": 1, "status": "blocked", "ready": False, "prerequisite_digest": "a" * 64,
        "cwd": {"state": "ready"}, "source": {"state": "missing"}, "syntax": {"state": "not_checked"}}))
    sandbox.exec_program = AsyncMock(side_effect=AssertionError("Missing source must never launch"))
    agent = object.__new__(BaseAgent)
    agent.max_retries = 0
    agent.toolkits = [ShellToolkit(sandbox)]
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    agent._add_to_memory = AsyncMock()
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[run_call("first")]))
    agent.ask_with_messages = AsyncMock(side_effect=[AIMessage(content="", tool_calls=[run_call("repeat")]),
                                                   AIMessage(content="", tool_calls=[run_call("ignore-diagnosis")])])
    agent._ask_without_tools = AsyncMock(return_value=AIMessage(content="Source is still absent; no program started."))
    events = [event async for event in agent.execute("analyze")]
    assert agent.last_execution_outcome["code"] == "analysis_no_progress_loop"
    assert agent.ask_with_messages.await_count == 2
    sandbox.exec_program.assert_not_awaited()
    assert not agent._tool_execution_ledger.summary()["pending_execution"]
    blocked = [event for event in events if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED]
    assert len(blocked) == 3 and all(event.dispatch_status == "not_started" for event in blocked)
    assert "DIAGNOSTIC RECOVERY" in blocked[1].function_result["message"]


def test_preparation_cannot_opt_in_a_plugin_or_forgive_unknown_launch():
    fake = SimpleNamespace(name="program_run", toolkit=SimpleNamespace(name="plugin"))
    assert program_preparation_failure(fake, run_call(), None) is None
    guard = AnalysisProgressGuard()
    launch = run_call()
    guard.record_program_prerequisites(path=PATH, prerequisite_digest="a" * 64, ready=False)
    guard.record(launch, succeeded=False, program_path=PATH)
    guard.record_blocked(launch, "missing source", program_path=PATH, recovery_kind="program_preparation")
    guard.record_blocked(launch, "missing source", program_path=PATH, recovery_kind="program_preparation")
    assert not guard.should_stop
    guard.record_program_prerequisites(path=PATH, prerequisite_digest="b" * 64, ready=True)
    assert not guard.should_stop
    assert guard.before_call(launch, program_path=PATH)  # No private not_started receipt: never replay.


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_state", ["not_started", "unknown"])
async def test_source_race_recovery_requires_exact_not_started_receipt(receipt_state):
    sandbox = make_sandbox()
    source = "print('validated')\n"
    original_source = source
    launches, receipts = [], {}

    async def post(url, *, json, **kwargs):
        nonlocal source
        if url.endswith("/shell/program-preflight"):
            ready = source is not None
            body = {"success": True, "data": {"version": 1, "status": "ready" if ready else "blocked",
                "ready": ready, "prerequisite_digest": ("a" if ready else "b") * 64,
                "cwd": {"state": "ready"},
                "source": {"state": "ready" if ready else "missing", "source_digest": "c" * 64 if ready else None},
                "syntax": {"state": "ready" if ready else "not_checked"}}}
        elif url.endswith("/shell/program"):
            launches.append(json)
            if len(launches) == 1:
                source = None  # Disappears only after the trusted ready probe.
                state, returncode = receipt_state, None
            else:
                assert source == original_source
                state, returncode = "exited", 0
            receipt = {"version": 1, "operation_id": json["operation_id"],
                "command_digest": shell_command_digest(json["exec_dir"], program_command(json["script_path"], json["args"])),
                "server_instance_id": "a" * 32, "state": state, "returncode": returncode,
                "process_tree_quiescent": state != "unknown"}
            receipts[json["operation_id"]] = receipt
            body = {"success": state == "exited", "data": {
                "status": "completed" if state == "exited" else "failed",
                "returncode": returncode, "execution_receipt": receipt,
                # Public metadata is identical in both cases and cannot attest
                # no side effects when the private receipt is unknown.
                "program_preparation": {"version": 1, "phase": "not_started", "reason": "script_missing"}}}
        elif url.endswith("/file/write"):
            source = json["content"]
            body = {"success": True, "data": {"file": PATH, "bytes_written": len(source)}}
        elif url.endswith("/shell/operation-status"):
            body = {"success": True, "data": receipts[json["operation_id"]]}
        else:
            raise AssertionError(url)
        return SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

    sandbox.client = SimpleNamespace(post=AsyncMock(side_effect=post))
    agent = object.__new__(BaseAgent)
    agent.max_retries = 0
    agent.toolkits = [ShellToolkit(sandbox), FileToolkit(sandbox)]
    agent._compact_tool_call_arguments = lambda *args: None
    agent._tool_result_for_memory = lambda result, *args: result
    agent._add_to_memory = AsyncMock()
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[run_call("racing-source")]))
    agent.ask_with_messages = AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[{"name": "file_write", "id": "restore-source",
                                          "args": {"file": PATH, "content": original_source}}]),
        AIMessage(content="", tool_calls=[run_call("repaired-source")]), AIMessage(content="done")])
    agent._ask_without_tools = AsyncMock(return_value=AIMessage(content="The execution state is unresolved."))
    events = [event async for event in agent.execute("analyze")]
    first = next(event for event in events if isinstance(event, ToolEvent)
                 and event.status == ToolStatus.CALLED and event.tool_call_id == "racing-source")
    assert first.dispatch_status == ("not_started" if receipt_state == "not_started" else None)
    assert first.program_attempt is None
    assert len(launches) == (2 if receipt_state == "not_started" else 1)
    assert agent.last_execution_outcome["code"] == ("completed" if receipt_state == "not_started" else "tool_execution_unknown")
