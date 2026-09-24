"""Reject empty core write targets before jobs, policy, or sandbox dispatch."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.pipeline import ToolExecutionInterceptor, ToolExecutionPipeline
from app.domain.services.tools.tool_contract import ToolContractError, validate_tool_arguments


def make_call(name, target):
    args = {"file": target}
    args.update({"content": "replacement"} if name == "file_write" else
                {"old_str": "old", "new_str": "new"})
    return {"name": name, "id": "file-target-test", "args": args}


def make_toolkit():
    sandbox = SimpleNamespace(
        file_write=AsyncMock(return_value=ToolResult(success=True)),
        file_replace=AsyncMock(return_value=ToolResult(success=True)),
    )
    return FileToolkit(sandbox), sandbox


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["file_write", "file_str_replace"])
@pytest.mark.parametrize("target", ["", " ", "\t\r\n", "\u2003", "\u001c", None])
async def test_blank_targets_are_rejected_before_admission_and_dispatch(name, target):
    toolkit, sandbox = make_toolkit()
    admitted = []

    class Admission(ToolExecutionInterceptor):
        async def pre_execute(self, context):
            admitted.append(context)

    toolkit.tool_execution_pipeline = ToolExecutionPipeline([Admission()])
    with pytest.raises(ToolContractError) as caught:
        await toolkit.get_tool(name).ainvoke(make_call(name, target))
    assert any(item["field"] == "file" for item in caught.value.fields)
    assert caught.value.side_effect_state == "not_started"
    assert not admitted
    sandbox.file_write.assert_not_awaited()
    sandbox.file_replace.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["file_write", "file_str_replace"])
async def test_agent_records_rejected_target_as_not_started_without_retry(name):
    toolkit, sandbox = make_toolkit()
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    result = await agent.invoke_tool(toolkit.get_tool(name), make_call(name, ""))
    assert result.status == "error"
    assert result.artifact.data["side_effect_state"] == "not_started"
    assert result.artifact.data["retryable"] is False
    assert agent.last_execution_outcome["side_effect_state"] == "not_started"
    assert not agent._tool_execution_ledger.summary()["pending_execution"]
    sandbox.file_write.assert_not_awaited()
    sandbox.file_replace.assert_not_awaited()
    corrected = make_call(name, "/home/ubuntu/output/corrected.txt")
    corrected["id"] = "corrected-file-target-test"
    result = await agent.invoke_tool(toolkit.get_tool(name), corrected)
    assert result.status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["file_write", "file_str_replace"])
@pytest.mark.parametrize("target", ["/home/ubuntu/output/report.md", "relative.txt", " report .txt ", "结果/报告.txt"])
async def test_nonblank_targets_are_dispatched_unchanged(name, target):
    toolkit, sandbox = make_toolkit()
    call = make_call(name, target)
    assert validate_tool_arguments(toolkit.get_tool(name), call)["args"]["file"] == target
    result = await toolkit.get_tool(name).ainvoke(call)
    assert result.status == "success"
    method = sandbox.file_write if name == "file_write" else sandbox.file_replace
    assert method.await_args.kwargs["file"] == target
    assert method.await_count == 1


@pytest.mark.asyncio
async def test_empty_file_contents_are_still_a_valid_write():
    toolkit, sandbox = make_toolkit()
    call = make_call("file_write", "/home/ubuntu/output/empty.txt")
    call["args"]["content"] = ""
    await toolkit.get_tool("file_write").ainvoke(call)
    assert sandbox.file_write.await_args.kwargs["content"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["file_write", "file_str_replace"])
async def test_dispatched_io_failure_still_has_unknown_effects(name):
    toolkit, sandbox = make_toolkit()
    method = sandbox.file_write if name == "file_write" else sandbox.file_replace
    method.side_effect = OSError("synthetic write failed after dispatch")
    agent = object.__new__(BaseAgent)
    agent.max_retries = 3
    result = await agent.invoke_tool(
        toolkit.get_tool(name), make_call(name, "/home/ubuntu/output/report.txt")
    )
    assert method.await_count == 1
    assert result.status == "error"
    assert result.artifact.data["side_effect_state"] == "unknown"
