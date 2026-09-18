"""Real adapter/pipeline regression: poll one private operation, never relaunch."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.models.analysis_job import AnalysisJobStatus
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.services.execution_evidence import ShellExecutionAttempt, ToolExecutionLedger, shell_command_digest, tool_execution_scope
from app.domain.services.program_execution import program_command
from app.domain.services.tools.analysis_job import AnalysisJobInterceptor
from app.domain.services.tools.interceptors import ToolConcurrencyInterceptor, ToolExecutionTimeoutError, ToolTimeoutInterceptor
from app.domain.services.tools.pipeline import ToolExecutionContext, ToolExecutionPipeline
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from test_analysis_job_service import InMemoryAnalysisJobRepository


class ProgramTransport:
    def __init__(self, running_windows=3):
        self.running_windows = running_windows
        self.waits = self.launches = self.kills = self.queries = 0
        self.operation_id = None
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False
        self.status_failures = 0
        self.wait_failures = 0
        self.view_failures = 0
        self.launch_timeout = False
        self.replace_operation = False
        self.bad_wait = None
        self.child_running = 0
        self.source = {"version": 1, "script_path": "/tmp/program.py", "source_digest": "b" * 64,
                       "returncode": 0, "failure_fingerprint": None, "diagnostic": None, "output_truncated": False}

    def receipt(self, *, done):
        return {"version": 1, "operation_id": "f" * 32 if self.replace_operation else self.operation_id,
                "command_digest": self.digest, "server_instance_id": "a" * 32,
                "state": "exited" if done else "running", "returncode": 0 if done else None,
                "process_tree_quiescent": done and self.child_running <= 0}

    async def post(self, url, *, json, **kwargs):
        if url.endswith("/program"):
            self.launches += 1
            self.operation_id = json["operation_id"]
            self.digest = shell_command_digest(json["exec_dir"], program_command(json["script_path"], json["args"]))
            if self.launch_timeout:
                raise httpx.ReadTimeout("lost launch response")
            data = {"status": "running", "execution_receipt": self.receipt(done=False)}
        else:
            assert json["operation_id"] == self.operation_id, "Every read/kill must stay pinned to the launch"
            if url.endswith("/wait"):
                self.waits += 1
                assert json["seconds"] == 1
                self.waiting.set()
                if self.hold:
                    await self.release.wait()
                if self.wait_failures:
                    self.wait_failures -= 1
                    raise httpx.ReadTimeout("transient wait response loss")
                if self.bad_wait is not None:
                    data = self.bad_wait
                else:
                    done = self.waits > self.running_windows
                    data = {"status": "completed" if done else "running", "returncode": 0 if done else None}
            elif url.endswith("/operation-status"):
                self.queries += 1
                if self.status_failures:
                    self.status_failures -= 1
                    raise httpx.ConnectError("transient status outage")
                data = self.receipt(done=self.waits > self.running_windows)
                self.child_running -= 1
            elif url.endswith("/view"):
                if self.view_failures:
                    self.view_failures -= 1
                    raise httpx.ReadTimeout("transient output response loss")
                data = {"output": "synthetic result", "program_execution": self.source}
            elif url.endswith("/kill"):
                self.kills += 1
                data = {"status": "terminated", "returncode": -15}
            else:
                raise AssertionError(url)
        return SimpleNamespace(json=lambda: {"success": True, "data": data})


def configured(transport, *, timeout=0.01):
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "synthetic"
    sandbox.base_url = "http://synthetic"
    sandbox.client = SimpleNamespace(post=AsyncMock(side_effect=transport.post))
    toolkit = ShellToolkit(sandbox)
    repo = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repo)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        AnalysisJobInterceptor(service, user_id="user", session_id="session", identity_provider=lambda: {"task_id": "task"}),
        ToolTimeoutInterceptor(timeout, maximum_timeout_seconds=timeout), ToolConcurrencyInterceptor(),
    ])
    call = {"name": "program_run", "id": "call", "args": {
        "id": "process", "exec_dir": "/tmp", "script_path": "/tmp/program.py", "timeout_seconds": 1}}
    return toolkit.get_tool("program_run"), call, ToolExecutionLedger(), repo


async def run(tool, call, ledger):
    with tool_execution_scope(ledger, call["id"]):
        return await tool.ainvoke(call)


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("app.domain.services.tools.shell._PROGRAM_RETRY_DELAY_SECONDS", 0)


@pytest.mark.asyncio
async def test_130_observation_windows_are_not_a_runtime_budget_and_launch_once():
    transport = ProgramTransport(running_windows=130)
    tool, call, ledger, repo = configured(transport)
    result = await run(tool, call, ledger)
    assert result.artifact.success
    assert transport.waits == 131 and transport.launches == 1 and transport.kills == 0
    assert not ledger.summary()["pending_execution"]
    record = next(iter(repo.records.values()))
    assert record.timeout_seconds is None and record.status == AnalysisJobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_job_remains_running_past_outer_deadline_and_cancel_kills_once():
    transport = ProgramTransport()
    transport.hold = True
    tool, call, ledger, repo = configured(transport)
    task = asyncio.create_task(run(tool, call, ledger))
    await asyncio.wait_for(transport.waiting.wait(), 1)
    await asyncio.sleep(0.04)  # The normal plugin/tool deadline is 0.01 seconds.
    record = next(iter(repo.records.values()))
    assert record.status == AnalysisJobStatus.RUNNING and record.timeout_seconds is None
    assert not task.done()
    service = tool.toolkit.tool_execution_pipeline.interceptors[0]._service
    old_lease = record.lease_expires_at
    await service.maintain()
    assert next(iter(repo.records.values())).lease_expires_at > old_lease
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.kills == 1 and transport.launches == 1
    assert next(iter(repo.records.values())).status == AnalysisJobStatus.CANCELLED


@pytest.mark.asyncio
async def test_long_program_lease_loss_still_cancels_original_operation_once():
    transport = ProgramTransport()
    transport.hold = True
    tool, call, ledger, repo = configured(transport)
    task = asyncio.create_task(run(tool, call, ledger))
    await asyncio.wait_for(transport.waiting.wait(), 1)
    record = next(iter(repo.records.values()))
    repo.records[record.job_id] = record.model_copy(update={"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    service = tool.toolkit.tool_execution_pipeline.interceptors[0]._service
    await service.maintain()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.kills == 1 and transport.launches == 1
    assert repo.records[record.job_id].status == AnalysisJobStatus.INTERRUPTED


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_launch", [False, True])
async def test_transient_wait_and_repeated_receipt_query_outages_only_retry_reads(lost_launch):
    transport = ProgramTransport(running_windows=0)
    transport.launch_timeout = lost_launch
    transport.wait_failures = 2
    transport.status_failures = 4
    transport.view_failures = 2
    tool, call, ledger, repo = configured(transport)
    result = await run(tool, call, ledger)
    assert result.artifact.success and transport.launches == 1 and transport.kills == 0
    assert transport.queries >= 5 and not ledger.summary()["pending_execution"]


@pytest.mark.asyncio
async def test_exited_parent_with_running_children_keeps_polling():
    transport = ProgramTransport(running_windows=0)
    transport.child_running = 4
    tool, call, ledger, _ = configured(transport)
    result = await run(tool, call, ledger)
    assert result.artifact.success and transport.queries >= 5 and transport.waits > 1
    assert transport.launches == 1 and transport.kills == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_wait", [{"status": "unknown"}, {"status": "completed", "returncode": True}, {"status": "completed"}])
async def test_unknown_or_malformed_observation_never_success_or_relaunch(bad_wait):
    transport = ProgramTransport(running_windows=0)
    transport.bad_wait = bad_wait
    tool, call, ledger, repo = configured(transport)
    result = await run(tool, call, ledger)
    assert not result.artifact.success and result.artifact.data["status"] == "unknown"
    assert transport.launches == 1 and transport.kills == 0
    assert next(iter(repo.records.values())).status == AnalysisJobStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_replaced_operation_cannot_attest_old_success():
    transport = ProgramTransport(running_windows=0)
    original = transport.post
    async def replaced(url, **kwargs):
        if url.endswith("/operation-status"):
            transport.replace_operation = True
        return await original(url, **kwargs)
    transport.post = replaced
    tool, call, ledger, _ = configured(transport)
    result = await run(tool, call, ledger)
    assert not result.artifact.success and result.artifact.data["status"] == "unknown"
    assert transport.launches == 1 and transport.kills == 0


@pytest.mark.asyncio
async def test_cancel_before_registering_new_attempt_does_not_kill_existing_process():
    transport = ProgramTransport()
    tool, call, ledger, _ = configured(transport)
    # Deliberately reuse even the call identity: the implementation records
    # the baseline and must not mistake this earlier operation for its launch.
    ledger.register(ShellExecutionAttempt(
        tool_call_id=call["id"], operation_id="a" * 32, sandbox_id="synthetic", shell_id="process",
        command_digest="b" * 64, query=AsyncMock(),
    ))
    entering = asyncio.Event()
    async def before_registration(*args):
        entering.set()
        await asyncio.Event().wait()
    tool.toolkit.sandbox.exec_program = before_registration
    task = asyncio.create_task(run(tool, call, ledger))
    await asyncio.wait_for(entering.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.kills == 0 and transport.launches == 0


@pytest.mark.asyncio
async def test_immediate_completion_must_agree_with_private_exit_receipt():
    transport = ProgramTransport(running_windows=0)
    original = transport.post
    async def inconsistent(url, **kwargs):
        response = await original(url, **kwargs)
        if url.endswith("/program"):
            payload = response.json()
            payload["data"].update(status="completed", returncode=0,
                                   execution_receipt={**transport.receipt(done=True), "returncode": 7})
            return SimpleNamespace(json=lambda: payload)
        return response
    transport.post = inconsistent
    tool, call, ledger, _ = configured(transport)
    result = await run(tool, call, ledger)
    assert not result.artifact.success and result.artifact.data["status"] == "unknown"
    assert transport.launches == 1 and transport.kills == 0 and transport.waits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["name", "contract", "plugin", "adapter", "declaration"])
async def test_only_exact_core_program_implementation_can_remove_outer_deadline(variant):
    transport = ProgramTransport()
    tool, call, _, _ = configured(transport)
    metadata = {}
    if variant == "name":
        tool = SimpleNamespace(name="program_run")
    elif variant == "contract":
        tool = SimpleNamespace(name="program_run", execution_contract={"timeout_seconds": None, "cancellable": True})
    elif variant == "plugin":
        metadata["plugin"] = {"name": "example"}
    elif variant == "adapter":
        tool.toolkit.sandbox = SimpleNamespace()
    else:
        tool._tool = ShellToolkit.shell_run
    context = ToolExecutionContext(tool=tool, tool_call=call, metadata=metadata)
    timeout = ToolTimeoutInterceptor(0.01, maximum_timeout_seconds=0.01)
    with pytest.raises(ToolExecutionTimeoutError):
        await timeout.execute(context, lambda: asyncio.sleep(1))
