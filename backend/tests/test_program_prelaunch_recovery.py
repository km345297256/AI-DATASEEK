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
    program_command, trusted_program_prelaunch_failure, trusted_program_prerequisites,
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
async def test_missing_prerequisite_then_real_source_creation_recovers_through_agent(directory):
    sandbox = make_sandbox()
    path = directory + "/analysis.py"
    source = None
    directory_exists = False
    launches, receipts = [], {}

    async def post(url, *, json, **kwargs):
        nonlocal source, directory_exists
        if url.endswith("/shell/program-preflight"):
            assert json == {"exec_dir": directory, "script_path": path}
            ready = directory_exists and source is not None
            data = {"version": 1, "status": "ready" if ready else "blocked", "ready": ready,
                    "prerequisite_digest": ("b" if ready else "a") * 64,
                    "cwd": {"state": "ready" if directory_exists else "missing"},
                    "source": {"state": "ready" if source is not None else "missing",
                               "source_digest": hashlib.sha256(source.encode()).hexdigest() if ready else None}}
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
             {"name": "shell_run", "id": "create-directory", "args": {
                 "id": "mkdir", "exec_dir": "/home/ubuntu", "command": "mkdir -p " + directory}},
             {"name": "file_write", "id": "create-source", "args": {
                 "file": path, "content": "print('validated')\n"}},
             run_call("after-source", path=path, directory=directory)]
    agent.ask = AsyncMock(return_value=AIMessage(content="", tool_calls=[calls[0]]))
    agent.ask_with_messages = AsyncMock(side_effect=[
        *(AIMessage(content="", tool_calls=[call]) for call in calls[1:]), AIMessage(content="done"),
    ])
    events = [event async for event in agent.execute("analyze")]
    assert len(launches) == 2
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
            "cwd": {"state": "ready"}, "source": {"state": "ready", "source_digest": "a" * 64}}
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
            "source": {"state": "ready", "source_digest": digest * 64}})

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
