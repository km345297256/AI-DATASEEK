"""Independent regression checks for observed-source revision semantics."""
import pytest
from unittest.mock import AsyncMock

from app.domain.models.tool_result import ToolResult
from test_artifact_write_agent_flow import call, final, memory_files, receipt_runtime, write
from test_tool_response_protocol_recovery import make_agent


@pytest.mark.asyncio
async def test_identical_whole_source_rewrite_does_not_unlock_same_failed_runtime(monkeypatch):
    path = "/home/ubuntu/output/analyze.R"
    source = 'stop("unchanged error")\n'
    run = lambda index: call("shell_run", f"run-{index}", id=f"shell-{index}",
        exec_dir="/home/ubuntu/output", command="Rscript analyze.R")
    agent, _, _, _ = make_agent(monkeypatch, [
        write("initial", source, path), run(1), write("same-source", source, path),
        run(2), final("源码没有改变，需依据错误修正后再执行。"),
    ])
    sandbox, files, _ = memory_files(agent)
    runtime, _ = receipt_runtime(agent, returncode=1)
    _events = [event async for event in agent.execute("恢复非Python失败，但不要重跑未修正的程序")]
    assert files[path] == source
    assert runtime.exec_command.await_count == 1


@pytest.mark.asyncio
async def test_zero_match_patch_does_not_erase_same_source_revision_identity(monkeypatch):
    path = "/home/ubuntu/output/analyze.R"
    source = 'stop("unchanged error")\n'
    run = lambda index: call("shell_run", f"run-{index}", id=f"shell-{index}",
        exec_dir="/home/ubuntu/output", command="Rscript analyze.R")
    agent, _, _, _ = make_agent(monkeypatch, [
        write("initial", source, path), run(1),
        call("file_str_replace", "no-match", file=path, old_str="missing", new_str="corrected"),
        write("same-source", source, path), run(2),
        final("没有实际修改源文件，不应再次执行同一失败程序。"),
    ])
    sandbox, files, _ = memory_files(agent)
    sandbox.file_replace = AsyncMock(return_value=ToolResult(success=True,
        data={"file": path, "replaced_count": 0}))
    runtime, _ = receipt_runtime(agent, returncode=1)
    _events = [event async for event in agent.execute("恢复非Python失败，但不要重跑未修正的程序")]
    assert sandbox.file_replace.await_count == 1
    assert files[path] == source
    assert runtime.exec_command.await_count == 1
