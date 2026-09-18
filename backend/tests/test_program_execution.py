import asyncio
import copy
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.execution_evidence import (
    ShellExecutionAttempt, ToolExecutionLedger, shell_command_digest,
    tool_execution_scope,
)
from app.domain.services.program_execution import (
    consume_program_feedback, program_command, resolved_program_path,
    trusted_program_execution_feedback,
)
from app.domain.services.tools.analysis_job import JOB_CORE_TOOLS
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def feedback(**changes):
    return {"version": 1, "script_path": "/tmp/analysis.py", "source_digest": "b" * 64,
            "returncode": 1, "failure_fingerprint": "c" * 64,
            "diagnostic": {"exception_type": "ValueError", "function": "parse", "line": 12},
            "output_truncated": False, **changes}


class ProgramSandbox:
    def __init__(self, *, running=False, waiting=False):
        self.exec_program = AsyncMock(return_value=ToolResult(
            success=running, data={"status": "running" if running else "completed", "returncode": None if running else 1,
                                  "program_execution": feedback(returncode=None if running else 1)},
        ))
        self.exec_command = AsyncMock(side_effect=AssertionError("Must not use bash"))
        self.wait_for_process = AsyncMock(return_value=ToolResult(
            success=waiting, data={"status": "running" if waiting else "completed", "returncode": None if waiting else 1},
        ))
        self.view_shell = AsyncMock(return_value=ToolResult(
            success=True, data={"output": "ValueError: bad input", "program_execution": feedback()},
        ))
        self.kill_process = AsyncMock(return_value=ToolResult(success=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_program_tool_never_uses_shell_and_preserves_real_failure(running):
    sandbox = ProgramSandbox(running=running)
    tool = ShellToolkit(sandbox).get_tool("program_run")
    message = await tool.ainvoke({"name": "program_run", "id": "call", "args": {
        "id": "run", "exec_dir": "/tmp", "script_path": "/tmp/analysis.py",
        "argv": ["| tail -80 && true"],
    }})
    result = message.artifact
    assert message.status == "error"
    sandbox.exec_program.assert_awaited_once_with("run", "/tmp", "/tmp/analysis.py", ["| tail -80 && true"])
    sandbox.exec_command.assert_not_awaited()
    assert result.success is False
    assert result.data["returncode"] == 1
    assert result.data["program_execution"] == feedback()


@pytest.mark.asyncio
async def test_program_observation_window_does_not_cancel_running_analysis(monkeypatch):
    sandbox = ProgramSandbox(running=True, waiting=True)
    monkeypatch.setattr("app.domain.services.tools.shell._PROGRAM_RETRY_DELAY_SECONDS", 0)
    sandbox.wait_for_process.side_effect = [
        ToolResult(success=True, data={"status": "running", "returncode": None}),
        ToolResult(success=False, data={"status": "completed", "returncode": 1}),
    ]
    message = await ShellToolkit(sandbox).get_tool("program_run").ainvoke({
        "name": "program_run", "id": "call", "args": {
            "id": "run", "exec_dir": "/tmp", "script_path": "/tmp/analysis.py",
            "argv": None, "timeout_seconds": 1,
        },
    })
    result = message.artifact
    assert message.status == "error"
    assert result.success is False
    assert result.data["status"] == "completed"
    assert result.data["returncode"] == 1
    sandbox.exec_program.assert_awaited_once_with("run", "/tmp", "/tmp/analysis.py", [])
    assert sandbox.wait_for_process.await_count == 2
    sandbox.kill_process.assert_not_awaited()


def setup_trusted(argv=None):
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "sandbox"
    tool = ShellToolkit(sandbox).get_tool("program_run")
    call = {"name": "program_run", "id": "call", "args": {
        "id": "run", "exec_dir": "/tmp", "script_path": "/tmp/analysis.py", "argv": argv,
    }}
    ledger = ToolExecutionLedger()
    attempt = ShellExecutionAttempt(tool_call_id="call", operation_id=uuid4().hex,
                                    sandbox_id="sandbox", shell_id="run",
                                    command_digest=shell_command_digest("/tmp", program_command("/tmp/analysis.py", argv or [])),
                                    query=AsyncMock())
    ledger.register(attempt)
    attempt.observe({"version": 1, "operation_id": attempt.operation_id,
                     "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
                     "state": "exited", "returncode": 1, "process_tree_quiescent": True})
    return tool, call, ledger, attempt


def test_public_or_model_program_feedback_is_not_evidence():
    tool, call, ledger, attempt = setup_trusted()
    result = ToolResult(success=False, data={"program_execution": feedback()})
    assert resolved_program_path(tool, call) == "/tmp/analysis.py"
    assert trusted_program_execution_feedback(tool, call, result, ledger) is None
    consume_program_feedback(result, attempt)
    trusted = trusted_program_execution_feedback(tool, call, result, ledger)
    assert trusted["source_digest"] == "b" * 64
    assert trusted["operation_id"] == attempt.operation_id
    # Forged public contents cannot replace adapter-bound source identity.
    result.data["program_execution"]["source_digest"] = "f" * 64
    assert trusted_program_execution_feedback(tool, call, result, ledger)["source_digest"] == "b" * 64


@pytest.mark.parametrize("mutate", [
    lambda call, attempt: call.update(id="other-call"),
    lambda call, attempt: call["args"].update(id="other-shell"),
    lambda call, attempt: call["args"].update(argv=["--different"]),
    lambda call, attempt: call["args"].update(script_path="/tmp/other.py"),
    lambda call, attempt: setattr(attempt, "program_execution", feedback(returncode=0)),
])
def test_mismatched_operation_or_exit_never_attests_progress(mutate):
    tool, call, ledger, attempt = setup_trusted([])
    consume_program_feedback(ToolResult(success=False, data={"program_execution": feedback()}), attempt)
    mutate(call, attempt)
    assert trusted_program_execution_feedback(tool, call, None, ledger) is None


def test_plugin_named_program_run_cannot_claim_core_program_identity():
    tool = SimpleNamespace(name="program_run", toolkit=SimpleNamespace())
    assert resolved_program_path(tool, {"name": "program_run", "args": {"script_path": "/tmp/analysis.py"}}) is None


def test_production_node_wrapper_preserves_trusted_program_identity():
    from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox
    tool, call, ledger, attempt = setup_trusted()
    tool.toolkit.sandbox = NodeBoundSandbox(tool.toolkit.sandbox, SimpleNamespace(node_id="local"))
    consume_program_feedback(ToolResult(success=False, data={"program_execution": feedback()}), attempt)
    assert trusted_program_execution_feedback(tool, call, None, ledger)["source_digest"] == "b" * 64


def test_arbitrary_wrapper_is_not_a_trusted_sandbox():
    tool, call, ledger, attempt = setup_trusted()
    tool.toolkit.sandbox = SimpleNamespace(sandbox=tool.toolkit.sandbox)
    assert resolved_program_path(tool, call) is None


def test_program_registered_as_cancellable_analysis_job():
    assert "program_run" in JOB_CORE_TOOLS
    command = program_command("/tmp/analysis.py", ["; true"])
    assert json.loads(command)["args"] == ["; true"]


@pytest.mark.asyncio
@pytest.mark.parametrize("argument_fields", [{}, {"argv": None}, {"argv": []}, {"argv": ["--validate-only", "/tmp/data with spaces", "| tail -80 && true"]}])
@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_public_schema_to_pipeline_to_adapter_binds_private_program_receipt(argument_fields, returncode, wrapped):
    """Exercise the model-facing ABI, not just the underlying Python coroutine.

    ``args`` was a Python parameter accepted by direct ``_arun`` tests but
    LangChain advertised it as ``v__args``. This path must preserve the exact
    published argument name through validation, invocation and receipt identity.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from app.domain.models.analysis_job import AnalysisJobStatus
    from app.domain.services.analysis_job_service import AnalysisJobService
    from app.domain.services.tools.analysis_job import AnalysisJobInterceptor
    from app.domain.services.tools.interceptors import ToolConcurrencyInterceptor
    from app.domain.services.tools.pipeline import ToolExecutionPipeline
    from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox
    from test_analysis_job_service import InMemoryAnalysisJobRepository

    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "sandbox"
    sandbox.base_url = "http://sandbox"
    argv = argument_fields.get("argv") or []
    caller_task = asyncio.current_task()
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    statuses = []
    async def collect_status(view, _context):
        statuses.append(view.status)

    async def post(url, *, json):
        # The production job worker executes in its own Task and must inherit
        # the private ledger ContextVar from the owning Agent invocation.
        assert asyncio.current_task() is not caller_task
        assert url.endswith("/shell/program")
        assert "command" not in json
        assert json["args"] == argv
        data = {"status": "completed", "returncode": returncode,
                "program_execution": feedback(returncode=returncode),
                "execution_receipt": {
                    "version": 1, "operation_id": json["operation_id"],
                    "command_digest": shell_command_digest(json["exec_dir"], program_command(json["script_path"], json["args"])),
                    "server_instance_id": "a" * 32, "state": "exited", "returncode": returncode,
                    "process_tree_quiescent": True,
                }}
        return SimpleNamespace(json=lambda: {"success": True, "data": data})
    sandbox.client = SimpleNamespace(post=AsyncMock(side_effect=post))
    tool = ShellToolkit(NodeBoundSandbox(sandbox, SimpleNamespace(node_id="local")) if wrapped else sandbox).get_tool("program_run")
    tool.toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        AnalysisJobInterceptor(
            service, user_id="user-1", session_id="session-1",
            identity_provider=lambda: {"task_id": "task-1"}, event_sink=collect_status,
        ),
        ToolConcurrencyInterceptor(),
    ])
    schema = convert_to_openai_tool(tool)["function"]["parameters"]
    assert set(schema["properties"]) == {"id", "exec_dir", "script_path", "argv", "timeout_seconds"}
    assert tool.args_schema.model_json_schema()["additionalProperties"] is False
    call = {"name": "program_run", "id": "call", "args": {
        "id": "run", "exec_dir": "/tmp", "script_path": "/tmp/analysis.py", **argument_fields,
    }}
    original = copy.deepcopy(call)
    ledger = ToolExecutionLedger()
    with tool_execution_scope(ledger, "call"):
        message = await tool.ainvoke(call)
    result = message.artifact
    assert call == original
    assert message.status == ("success" if returncode == 0 else "error")
    assert result.success is (returncode == 0)
    sandbox.client.post.assert_awaited_once()
    assert "execution_receipt" not in result.data
    assert trusted_program_execution_feedback(tool, call, result, ledger)["returncode"] == returncode
    assert statuses == [
        AnalysisJobStatus.QUEUED, AnalysisJobStatus.RUNNING,
        AnalysisJobStatus.SUCCEEDED if returncode == 0 else AnalysisJobStatus.FAILED,
    ]
    assert next(iter(repository.records.values())).status == statuses[-1]


def test_every_shell_tool_schema_matches_its_executable_signature():
    """New declarations must publish exactly the argument names they accept."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    toolkit = ShellToolkit(ProgramSandbox())
    for tool in toolkit.get_tools():
        public_fields = set(convert_to_openai_tool(tool)["function"]["parameters"]["properties"])
        signature = inspect.signature(tool._tool.coroutine)
        executable_fields = set(signature.parameters) - {"self"}
        assert public_fields == executable_fields, tool.name
        assert set(tool.args_schema.model_fields) == executable_fields, tool.name
        # Exercise Python binding without invoking any command or sandbox API.
        signature.bind(toolkit, **{name: object() for name in public_fields})


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_argv", [{"v__args": ["--validate-only"]}, {"args": []}, {"argv": "--validate-only"}, {"argv": [3]}])
async def test_program_public_contract_rejects_mismatched_arguments_before_launch(invalid_argv):
    from app.domain.services.tools.tool_contract import ToolContractError

    sandbox = ProgramSandbox()
    tool = ShellToolkit(sandbox).get_tool("program_run")
    with pytest.raises(ToolContractError) as error:
        await tool.ainvoke({"name": "program_run", "id": "call", "args": {
            "id": "run", "exec_dir": "/tmp", "script_path": "/tmp/analysis.py", **invalid_argv,
        }})
    assert error.value.side_effect_state == "not_started"
    sandbox.exec_program.assert_not_awaited()
