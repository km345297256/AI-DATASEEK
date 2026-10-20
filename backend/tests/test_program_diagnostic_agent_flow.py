"""Host-selected diagnostics use the real agent/tool/receipt lifecycle offline."""
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, ToolMessage

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.services.analysis_progress import program_identity
from app.domain.services.analysis_recovery import AnalysisRecoveryContext, analysis_recovery_scope
from app.domain.services.execution_evidence import shell_command_digest
from app.domain.services.program_execution import program_command
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.pipeline import ToolExecutionInterceptor, ToolExecutionPipeline
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from test_tool_response_protocol_recovery import make_agent


PROGRAM = "/home/ubuntu/output/analyze.py"
INPUT = "/home/ubuntu/datasets/synthetic/records.jsonl"
UNRELATED = "/home/ubuntu/datasets/synthetic/unrelated.jsonl"
SOURCE = (f"import json\nrows = [json.loads(line) for line in open({INPUT!r})]\n"
          'values = [row["labels"][0] for row in rows]\nprint(values)\n')
INPUT_CONTENT = '{"labels": []}\n{"labels": ["example"]}\n'


def native(name, identifier, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": identifier, "args": args}])


def run(identifier):
    return native("program_run", identifier, id="analysis", exec_dir="/home/ubuntu/output",
                  script_path=PROGRAM, argv=[])


class RecordingPermission(ToolExecutionInterceptor):
    def __init__(self, deny_host_reads=False, cancel_host_reads=False):
        self.calls = []
        self.denied = []
        self.deny_host_reads = deny_host_reads
        self.cancel_host_reads = cancel_host_reads

    async def guard(self, context):
        self.calls.append((context.tool_call_id, context.tool_name, dict(context.arguments)))
        if self.cancel_host_reads and str(context.tool_call_id).startswith("host-diagnostic-"):
            raise asyncio.CancelledError("synthetic user cancellation")
        if self.deny_host_reads and str(context.tool_call_id).startswith("host-diagnostic-"):
            self.denied.append(context.tool_call_id)
            raise PermissionError("synthetic read permission denied")


def runtime(agent, *, deny_host_reads=False, cancel_host_reads=False):
    """Fake only sandbox HTTP; keep the first-party adapter/ledger contracts."""
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "host-diagnostic-flow"
    sandbox.base_url = "http://synthetic-sandbox:8080"
    files = {PROGRAM: SOURCE, INPUT: INPUT_CONTENT}
    reads, launches, writes, receipts = [], [], [], {}

    async def post(url, *, json, **kwargs):
        if url.endswith("/shell/program-preflight"):
            source_digest = hashlib.sha256(files[PROGRAM].encode()).hexdigest()
            body = {"success": True, "data": {"version": 1, "status": "ready", "ready": True,
                "prerequisite_digest": source_digest, "cwd": {"state": "ready"},
                "source": {"state": "ready", "source_digest": source_digest},
                "syntax": {"state": "ready", "exception_type": None, "line": None, "offset": None}}}
        elif url.endswith("/shell/program"):
            launches.append(dict(json))
            source = files[PROGRAM]
            success = 'if row.get("labels")' in source
            code = 0 if success else 1
            source_digest = hashlib.sha256(source.encode()).hexdigest()
            receipt = {"version": 1, "operation_id": json["operation_id"],
                "command_digest": shell_command_digest(json["exec_dir"],
                    program_command(json["script_path"], json["args"])),
                "server_instance_id": "a" * 32, "state": "exited", "returncode": code,
                "process_tree_quiescent": True}
            receipts[json["operation_id"]] = receipt
            body = {"success": success, "data": {"status": "completed", "returncode": code,
                "output": "example" if success else "IndexError: list index out of range",
                "execution_receipt": receipt, "program_execution": {
                    "version": 1, "script_path": PROGRAM, "source_digest": source_digest,
                    "returncode": code, "failure_fingerprint": None if success else "c" * 64,
                    "diagnostic": None if success else {"exception_type": "IndexError", "line": 3},
                    "output_truncated": False, "source_snapshot": {"version": 1,
                        "encoding": "utf-8", "size_bytes": len(source.encode()),
                        "sha256": source_digest, "content": source}}}}
        elif url.endswith("/shell/operation-status"):
            body = {"success": True, "data": receipts[json["operation_id"]]}
        elif url.endswith("/file/read"):
            reads.append(dict(json))
            text = files[json["file"]]
            if json.get("start_line") is not None or json.get("end_line") is not None:
                text = "\n".join(text.splitlines()[json.get("start_line"):json.get("end_line")])
            body = {"success": True, "data": {"file": json["file"], "content": text}}
        elif url.endswith("/file/replace"):
            writes.append(dict(json))
            assert json["old_str"] in files[json["file"]]
            files[json["file"]] = files[json["file"]].replace(json["old_str"], json["new_str"])
            body = {"success": True, "data": {"file": json["file"]}}
        else:
            raise AssertionError(f"Unexpected sandbox endpoint: {url}")
        return SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

    sandbox.client = SimpleNamespace(post=AsyncMock(side_effect=post))
    permission = RecordingPermission(deny_host_reads=deny_host_reads, cancel_host_reads=cancel_host_reads)
    toolkits = [ShellToolkit(sandbox), FileToolkit(sandbox)]
    for toolkit in toolkits:
        toolkit.tool_execution_pipeline = ToolExecutionPipeline([permission])
    agent.toolkits.extend(toolkits)
    recovery = AnalysisRecoveryContext(review=AsyncMock())
    recovery.progress.read_scope_paths = frozenset({INPUT, UNRELATED})
    return SimpleNamespace(files=files, reads=reads, writes=writes, launches=launches,
                           recovery=recovery, permission=permission)


def host_events(events):
    return [event for event in events if isinstance(event, ToolEvent)
            and event.tool_call_id.startswith("host-diagnostic-")]


@pytest.mark.asyncio
async def test_host_diagnostic_reads_restore_one_correction_through_real_agent_pipeline(monkeypatch):
    checkpoint = {}

    def informed_patch(agent):
        state = agent._analysis_progress.programs[program_identity(PROGRAM)]
        checkpoint.update(required=state.diagnostic_required, failures=state.unsuccessful_executions,
                          trials=len(state.joint_diagnostic_trials))
        return native("file_str_replace", "repair", file=PROGRAM, old_str='for row in rows]',
                      new_str='for row in rows if row.get("labels")]')

    agent, _, requests, _ = make_agent(monkeypatch, [
        native("file_read", "original-read", file=INPUT), run("run-1"), run("run-2"),
        informed_patch, run("repaired-run"), AIMessage(content="The saved program completed after repairing empty-list handling."),
    ])
    context = runtime(agent)
    with analysis_recovery_scope(context.recovery):
        events = [event async for event in agent.execute("Analyze synthetic records")]

    assert len(context.launches) == 3 and len(context.writes) == 1
    assert [read["file"] for read in context.reads] == [INPUT, INPUT, PROGRAM]
    assert checkpoint == {"required": False, "failures": 2, "trials": 1}
    assert len(requests) == 6  # Host inspection consumes no model round.
    paired = host_events(events)
    assert len(paired) == 4
    ids = {event.tool_call_id for event in paired}
    assert len(ids) == 2
    assert all({event.status for event in paired if event.tool_call_id == identifier}
               == {ToolStatus.CALLING, ToolStatus.CALLED} for identifier in ids)
    assert {identifier for identifier, _, _ in context.permission.calls if identifier in ids} == ids
    assert all(event.function_result.success for event in paired if event.status == ToolStatus.CALLED)
    memory = agent.memory.get_messages()
    assert any(isinstance(item, AIMessage) and item.additional_kwargs.get("host_diagnostic") for item in memory)
    assert {item.tool_call_id for item in memory if isinstance(item, ToolMessage) and item.tool_call_id in ids} == ids
    assert all(sum(isinstance(item, ToolMessage) and item.tool_call_id == identifier for item in memory) == 1
               for identifier in ids)
    assert agent._tool_execution_ledger.summary()["pending_execution"] is False
    assert agent._analysis_progress.programs[program_identity(PROGRAM)].unsuccessful_executions == 0
    assert agent.last_execution_outcome["code"] == "completed"


@pytest.mark.asyncio
async def test_same_failure_does_not_schedule_repeated_host_sampling_after_cosmetic_repair(monkeypatch):
    agent, _, requests, _ = make_agent(monkeypatch, [
        native("file_read", "original-read", file=INPUT), run("run-1"), run("run-2"),
        native("file_str_replace", "cosmetic-repair", file=PROGRAM, old_str="print(values)", new_str="print(values)  # unchanged access"),
        run("same-cause-again"), run("blind-retry"),
        AIMessage(content="The same input assumption still needs correction; no result is claimed."),
    ])
    context = runtime(agent)
    with analysis_recovery_scope(context.recovery):
        events = [event async for event in agent.execute("Analyze synthetic records")]
    assert len(context.launches) == 3 and len(context.writes) == 1
    assert len(host_events(events)) == 4
    assert [read["file"] for read in context.reads] == [INPUT, INPUT, PROGRAM]
    assert len(requests) == 7
    retry = next(event for event in events if isinstance(event, ToolEvent)
                 and event.tool_call_id == "blind-retry" and event.status == ToolStatus.CALLED)
    assert retry.dispatch_status == "not_started"
    assert context.recovery.progress.programs[program_identity(PROGRAM)].diagnostic_required
    assert len(context.recovery.diagnostics._attempted) == 1


@pytest.mark.asyncio
async def test_host_diagnostic_reads_do_not_bypass_pipeline_permission_denial(monkeypatch):
    agent, _, _, _ = make_agent(monkeypatch, [
        native("file_read", "original-read", file=INPUT), run("run-1"), run("run-2"),
        native("file_str_replace", "unauthorized-repair", file=PROGRAM, old_str='for row in rows]',
               new_str='for row in rows if row.get("labels")]'),
        AIMessage(content="Diagnostic reads were denied; the program was not rerun."),
    ])
    context = runtime(agent, deny_host_reads=True)
    with analysis_recovery_scope(context.recovery):
        events = [event async for event in agent.execute("Analyze synthetic records")]
    assert len(context.permission.denied) == 2
    assert [read["file"] for read in context.reads] == [INPUT]
    assert len(context.launches) == 2 and context.writes == []
    assert len(host_events(events)) == 4
    assert all(event.function_result.success is False for event in host_events(events)
               if event.status == ToolStatus.CALLED)
    state = context.recovery.progress.programs[program_identity(PROGRAM)]
    assert state.diagnostic_required and not state.joint_diagnostic_trials
    assert agent._tool_execution_ledger.summary()["pending_execution"] is False


@pytest.mark.asyncio
async def test_cancelled_host_inspection_settles_unstarted_calls_without_continuing(monkeypatch):
    agent, _, requests, _ = make_agent(monkeypatch, [
        native("file_read", "original-read", file=INPUT), run("run-1"), run("run-2"),
        AssertionError("No further model request after cancellation"),
    ])
    context = runtime(agent, cancel_host_reads=True)
    events = []
    with analysis_recovery_scope(context.recovery), pytest.raises(asyncio.CancelledError):
        async for event in agent.execute("Analyze synthetic records"):
            events.append(event)
    assert len(requests) == 3 and len(context.launches) == 2
    assert [read["file"] for read in context.reads] == [INPUT]
    assert context.writes == []
    ids = {event.tool_call_id for event in host_events(events)}
    assert len(ids) == 2
    terminal = [event for event in host_events(events) if event.status == ToolStatus.CALLED]
    assert {event.tool_call_id for event in terminal} == ids
    assert all(sum(event.tool_call_id == identifier for event in terminal) == 1 for identifier in ids)
    assert len([call for call in context.permission.calls if call[0] in ids]) == 1
    memory = agent.memory.get_messages()
    assert all(sum(isinstance(item, ToolMessage) and item.tool_call_id == identifier for item in memory) == 1
               for identifier in ids)
    assert context.recovery.progress.programs[program_identity(PROGRAM)].diagnostic_required
    assert agent.last_execution_outcome["code"] == "cancelled"
