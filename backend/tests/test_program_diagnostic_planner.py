"""Host recovery reads remain scoped, receipt-bound and normally dispatched."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_program_diagnostics import (
    ProgramDiagnosticPlanner, program_diagnostic_read_digest, program_diagnostic_read_lines,
)
from app.domain.services.analysis_progress import AnalysisProgressGuard, program_identity
from app.domain.services.execution_evidence import (
    ShellExecutionAttempt, ToolExecutionLedger, shell_command_digest,
)
from app.domain.services.execution_identity import private_identity_hmac
from app.domain.services.program_execution import (
    consume_program_feedback, program_command, trusted_program_execution_feedback,
)
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


PROGRAM = "/home/ubuntu/report.py"
INPUT = "/home/ubuntu/datasets/example/records.jsonl"
OTHER = "/home/ubuntu/datasets/example/unrelated.jsonl"


def record_failure(sandbox, runner, guard, ledger, *, source, index=0, error="c" * 64,
                   line=3, argv=None):
    call = {"name": "program_run", "id": f"call-{index}", "args": {
        "id": f"shell-{index}", "exec_dir": "/home/ubuntu", "script_path": PROGRAM, "argv": argv or [],
    }}
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = {"version": 1, "encoding": "utf-8", "size_bytes": len(source.encode()),
                "sha256": digest, "content": source}
    attempt = ShellExecutionAttempt(tool_call_id=call["id"], operation_id=uuid4().hex,
        sandbox_id=sandbox.id, shell_id=call["args"]["id"],
        command_digest=shell_command_digest("/home/ubuntu", program_command(PROGRAM, argv or [])),
        query=AsyncMock())
    ledger.register(attempt)
    attempt.observe({"version": 1, "operation_id": attempt.operation_id,
        "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
        "state": "exited", "returncode": 1, "process_tree_quiescent": True})
    consume_program_feedback(ToolResult(success=False, data={"program_execution": {
        "version": 1, "script_path": PROGRAM, "source_digest": digest, "returncode": 1,
        "failure_fingerprint": error, "diagnostic": {"exception_type": "IndexError", "line": line},
        "source_snapshot": snapshot, "output_truncated": False,
    }}), attempt)
    feedback = trusted_program_execution_feedback(runner, call, None, ledger)
    guard.record_program_execution(path=PROGRAM, operation_id=feedback["operation_id"],
        source_digest=digest, returncode=1, failure_fingerprint=error, call=call,
        diagnostic=feedback["diagnostic"], source_snapshot=feedback["source_snapshot"])
    return call


def setup(*, source=None, scope=None, argv=None):
    source = source or f'import json\nrows = [json.loads(s) for s in open({INPUT!r})]\nvalues = [r["labels"][0] for r in rows]\n'
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "diagnostic-test"
    runner = ShellToolkit(sandbox).get_tool("program_run")
    reader = FileToolkit(sandbox).get_tool("file_read")
    ledger = ToolExecutionLedger()
    guard = AnalysisProgressGuard()
    guard.read_scope_paths = frozenset(scope if scope is not None else {INPUT, OTHER})
    for index in range(3):
        call = record_failure(sandbox, runner, guard, ledger, source=source, index=index, argv=argv)
    return SimpleNamespace(sandbox=sandbox, runner=runner, reader=reader, ledger=ledger,
                           guard=guard, call=call, source=source)


def plan(fixture, planner=None, *, tools=None):
    return (planner or ProgramDiagnosticPlanner()).plan(program_tool=fixture.runner,
        program_call=fixture.call, ledger=fixture.ledger, progress=fixture.guard,
        tools=tools if tools is not None else [fixture.runner, fixture.reader])


def test_plan_is_bound_to_current_failed_program_inputs_not_the_whole_request():
    fixture = setup()
    fixture.sandbox.file_read = AsyncMock()
    planner = ProgramDiagnosticPlanner()
    result = plan(fixture, planner)
    assert result is not None
    assert [call["args"]["file"] for call in result.tool_calls] == [INPUT, PROGRAM]
    assert result.tool_calls[0]["args"] == {"file": INPUT, "sudo": False}
    assert result.tool_calls[1]["args"] == {"file": PROGRAM, "sudo": False,
                                           "start_line": 0, "end_line": 6}
    assert len({call["id"] for call in result.tool_calls}) == 2
    assert all(call["name"] == "file_read" and call["type"] == "tool_call" for call in result.tool_calls)
    assert "dependent accesses" in result.instruction and "not proof" in result.instruction
    assert fixture.guard.programs[program_identity(PROGRAM)].diagnostic_required
    fixture.sandbox.file_read.assert_not_awaited()
    assert INPUT not in repr(planner._attempted) and PROGRAM not in repr(planner._attempted)


def test_repeated_plan_and_cosmetic_revisions_cannot_refresh_failed_or_denied_reads():
    fixture = setup()
    planner = ProgramDiagnosticPlanner()
    assert plan(fixture, planner)
    assert plan(fixture, planner) is None
    fixture.call = record_failure(fixture.sandbox, fixture.runner, fixture.guard, fixture.ledger,
        source=fixture.source + "# wording changed, no different failure or input\n", index=4)
    assert plan(fixture, planner) is None
    # A genuinely different confirmed failure may need another inspection.
    fixture.call = record_failure(fixture.sandbox, fixture.runner, fixture.guard, fixture.ledger,
        source=fixture.source, index=5, error="d" * 64)
    assert plan(fixture, planner)


@pytest.mark.parametrize("fault", ["no-reader", "fake-reader", "unregistered-reader", "other-sandbox",
    "ambiguous-reader", "no-snapshot", "wrong-call", "old-operation", "pending", "not-required",
    "corrupt-snapshot", "oversized-snapshot", "wrong-line", "bool-line", "no-line", "public-success"])
def test_no_impostor_stale_unknown_or_unavailable_execution_can_plan_reads(fault):
    fixture = setup()
    tools = [fixture.runner, fixture.reader]
    if fault == "no-reader": tools = [fixture.runner]
    elif fault == "fake-reader": tools = [SimpleNamespace(name="file_read", toolkit=fixture.reader.toolkit)]
    elif fault == "unregistered-reader": fixture.reader.toolkit.tools.clear()
    elif fault == "other-sandbox": fixture.reader.toolkit.sandbox = SimpleNamespace(id=fixture.sandbox.id)
    elif fault == "ambiguous-reader": tools.append(SimpleNamespace(name="file_read"))
    elif fault == "no-snapshot": next(reversed(fixture.ledger._attempts.values())).program_execution.pop("source_snapshot")
    elif fault == "wrong-call": fixture.call["args"]["argv"] = ["--different"]
    elif fault == "old-operation": fixture.guard.programs[program_identity(PROGRAM)].last_operation = "older"
    elif fault == "pending": fixture.ledger.record_unknown("unconfirmed-write")
    elif fault == "not-required": fixture.guard.programs[program_identity(PROGRAM)].diagnostic_required = False
    else:
        feedback = next(reversed(fixture.ledger._attempts.values())).program_execution
        if fault == "corrupt-snapshot": feedback["source_snapshot"]["content"] = "unbound replacement"
        elif fault == "oversized-snapshot": feedback["source_snapshot"]["size_bytes"] = 24001
        elif fault == "wrong-line": feedback["diagnostic"]["line"] = 9999
        elif fault == "bool-line": feedback["diagnostic"]["line"] = True
        elif fault == "no-line": feedback["diagnostic"].pop("line")
        else: feedback["returncode"] = 0
    assert plan(fixture, tools=tools) is None


@pytest.mark.parametrize("scope", [set(), {OTHER}, None])
def test_unbound_inputs_and_unknown_scope_do_not_acquire_read_authority(scope):
    fixture = setup(scope=set())
    fixture.guard.read_scope_paths = None if scope is None else frozenset(scope)
    assert plan(fixture) is None


def test_argv_bindings_and_sampling_are_bounded_without_input_line_ranges():
    paths = [f"/home/ubuntu/datasets/example/input-{index}.jsonl" for index in range(4)]
    fixture = setup(source="import sys\nrows = []\nprint(rows[0])\n", scope=paths, argv=paths)
    result = plan(fixture)
    assert [call["args"]["file"] for call in result.tool_calls] == paths[:2] + [PROGRAM]
    assert all(set(call["args"]) == {"file", "sudo"} for call in result.tool_calls[:-1])


def test_previous_version_input_cannot_justify_auto_read_in_current_version():
    fixture = setup()
    fixture.call = record_failure(fixture.sandbox, fixture.runner, fixture.guard, fixture.ledger,
        source=f'rows = []\nopen({OTHER!r}).read()\nprint(rows[0])\n', index=4)
    assert fixture.guard.programs[program_identity(PROGRAM)].input_targets == frozenset({program_identity(INPUT), program_identity(OTHER)})
    assert [call["args"]["file"] for call in plan(fixture).tool_calls] == [OTHER, PROGRAM]


@pytest.mark.parametrize("path", ["/home/ubuntu/datasets/example/../other.jsonl",
                                "/home/ubuntu//datasets/example/input.jsonl",
                                "/home/ubuntu/datasets/example/evil\x00.jsonl"])
def test_even_scope_membership_does_not_authorize_noncanonical_auto_paths(path):
    fixture = setup(source=f"import json\nrows = list(open({path!r}))\nprint(rows[0])\n", scope={path})
    assert plan(fixture) is None


@pytest.mark.asyncio
async def test_real_checked_read_results_allow_one_joint_diagnosis_without_forgiving_failures():
    fixture = setup()
    input_text = '{"labels": []}\n{"labels": ["algebra"]}\n'

    async def read(file, start_line=None, end_line=None, sudo=False):
        text = fixture.source if file == PROGRAM else input_text
        if start_line is not None or end_line is not None:
            text = "\n".join(text.splitlines()[start_line:end_line])
        return ToolResult(success=True, data={"file": file, "content": text})

    fixture.sandbox.file_read = AsyncMock(side_effect=read)
    planner = ProgramDiagnosticPlanner()
    # The original input had already been observed: only a paired, exact failed
    # source observation can justify this diagnosis, not fake fresh call IDs.
    input_digest = private_identity_hmac({
        "purpose": "program-diagnostic-read/v1", "content": input_text})
    fixture.guard.read_observations.add(private_identity_hmac({
        "purpose": "analysis-read-evidence/v1", "call": program_identity(INPUT), "result": input_digest}))
    calls = plan(fixture, planner).tool_calls
    for call in calls:
        result = await fixture.reader.ainvoke(call)
        digest = program_diagnostic_read_digest(fixture.reader, call, result)
        fixture.guard.record(call, succeeded=True, read_only=True, result_digest="not-authoritative",
                             read_content_digest=digest)
        fixture.guard.record_program_diagnostic(path=call["args"]["file"], content_digest=digest,
            line_observation=program_diagnostic_read_lines(fixture.reader, call, result))
    state = fixture.guard.programs[program_identity(PROGRAM)]
    assert state.diagnostic_required is False and len(state.joint_diagnostic_trials) == 1
    assert state.unsuccessful_executions == 3
    assert fixture.sandbox.file_read.await_count == 2
    fixture.call = record_failure(fixture.sandbox, fixture.runner, fixture.guard, fixture.ledger,
                                 source=fixture.source, index=4)
    assert state.diagnostic_required is True and plan(fixture, planner) is None


def test_shell_output_has_no_path_to_the_planner():
    fixture = setup()
    shell = fixture.runner.toolkit.get_tool("shell_exec")
    shell_call = {"name": "shell_exec", "id": "shell-diagnosis", "args": {
        "id": "shell", "exec_dir": "/home/ubuntu", "command": "echo '65 empty lists observed'"}}
    assert ProgramDiagnosticPlanner().plan(program_tool=shell, program_call=shell_call,
        ledger=fixture.ledger, progress=fixture.guard, tools=[fixture.reader]) is None
