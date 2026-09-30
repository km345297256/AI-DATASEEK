"""Unlimited local plugins keep a real per-job stop control."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_job import AnalysisJobStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.services.tools.analysis_job import (
    AnalysisJobCancelled, AnalysisJobInterceptor, _host_cancellable_sandbox_plugin,
)
from app.domain.services.tools.interceptors import ToolPolicySnapshot, create_production_tool_interceptors
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.plugin import PluginToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.sandbox.runtime import NodeBoundSandbox
from test_analysis_job_service import InMemoryAnalysisJobRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("node_bound", [False, True])
async def test_builtin_non_cancellable_manifest_still_allows_targeted_process_cancel(node_bound):
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "synthetic"
    sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True, data={"status": "running"}))
    observing = asyncio.Event()

    async def observe(*args, **kwargs):
        observing.set()
        await asyncio.Event().wait()

    sandbox.wait_for_process = AsyncMock(side_effect=observe)
    sandbox.kill_process = AsyncMock(return_value=ToolResult(success=True))
    sandbox.release_shell = AsyncMock(return_value=ToolResult(success=True))
    adapter = NodeBoundSandbox(sandbox, SimpleNamespace(node_id="local")) if node_bound else sandbox
    toolkit = PluginToolkit(adapter, session_id="session")
    tool = toolkit.get_tool("table_profile")
    assert tool.execution_contract["cancellable"] is False
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    # Allow the exact declared tool/effect without expanding any remote policy.
    toolkit.tool_execution_pipeline = ToolExecutionPipeline(create_production_tool_interceptors(
        policy_snapshot=ToolPolicySnapshot.for_registered_tools(["table_profile"]),
        analysis_job_interceptor=AnalysisJobInterceptor(
            service, user_id="owner", session_id="session", identity_provider=lambda: {"task_id": "task"},
        ),
    ))
    task = asyncio.create_task(tool.ainvoke({
        "name": "table_profile", "id": "call", "args": {"input_path": "/tmp/synthetic.csv"},
    }))
    await asyncio.wait_for(observing.wait(), 1)
    record = next(iter(repository.records.values()))
    assert record.cancellable and record.timeout_seconds is None
    assert await service.request_cancel("other-owner", "session", record.job_id) is None
    assert not task.done()
    await service.request_cancel("owner", "session", record.job_id)
    with pytest.raises(AnalysisJobCancelled):
        await asyncio.wait_for(task, 1)
    sandbox.exec_command.assert_awaited_once()
    session_id = sandbox.exec_command.await_args.args[0]
    sandbox.kill_process.assert_awaited_once_with(session_id)
    sandbox.release_shell.assert_awaited_once_with(session_id)
    assert repository.records[record.job_id].status == AnalysisJobStatus.CANCELLED


def test_self_declared_or_arbitrary_adapter_does_not_gain_host_cancellation():
    lookalike = SimpleNamespace(name="table_profile", execution_contract={"cancellable": False},
                               toolkit=SimpleNamespace(sandbox=object.__new__(DockerSandbox)))
    assert not _host_cancellable_sandbox_plugin(lookalike)
    arbitrary_adapter = SimpleNamespace(kill_process=AsyncMock(), release_shell=AsyncMock(),
                                        supports_execution_receipts=True)
    toolkit = PluginToolkit(arbitrary_adapter, session_id="session")
    assert not _host_cancellable_sandbox_plugin(toolkit.get_tool("table_profile"))
