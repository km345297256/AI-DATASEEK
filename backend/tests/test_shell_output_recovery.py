import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.plugin import PluginToolkit
from app.domain.services.tools.shell import ShellToolkit
from app.domain.services.tools.shell_output import recover_shell_output, ShellOutputUnavailable
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def paged_sandbox(text):
    raw = text.encode()
    metadata = {"output_id": "a" * 32, "total_bytes": len(raw), "preview_truncated": True,
                "log_status": "available", "stream_complete": True}
    async def view(session_id, *, output_id, cursor, max_bytes):
        assert output_id == metadata["output_id"]
        part = raw[cursor:cursor + max_bytes].decode("utf-8", errors="ignore")
        next_cursor = cursor + len(part.encode())
        return ToolResult(success=True, data={"output": part, "output_metadata": dict(metadata),
            "output_page": {"cursor": cursor, "start": cursor, "next_cursor": next_cursor,
                            "lossy": False, "source": "spool", "eof": next_cursor == len(raw)}})
    sandbox = SimpleNamespace(view_shell=AsyncMock(side_effect=view),
        exec_command=AsyncMock(), release_shell=AsyncMock())
    return sandbox, {"status": "completed", "returncode": 0, "output": "bounded preview", "output_metadata": metadata}


@pytest.mark.asyncio
async def test_recovers_full_contract_without_reexecuting_or_mixing_observer_cursor():
    text = json.dumps({"data": "科学😀" * 15000}, ensure_ascii=False)
    sandbox, data = paged_sandbox(text)
    assert await recover_shell_output(sandbox, "shell", data) == text
    assert await recover_shell_output(sandbox, "shell", data) == text
    assert sandbox.view_shell.call_args_list[0].kwargs["cursor"] == 0
    sandbox.exec_command.assert_not_called()


@pytest.mark.asyncio
async def test_contract_ceiling_rejects_before_any_request():
    sandbox, data = paged_sandbox("x")
    data["output_metadata"]["total_bytes"] = 2 * 1024 * 1024 + 1
    with pytest.raises(ShellOutputUnavailable) as error:
        await recover_shell_output(sandbox, "shell", data)
    assert error.value.code == "tool_output_size_limit"
    sandbox.view_shell.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"lossy": True}, {"source": "tail"}, {"next_cursor": 0},
    {"start": 1}, {"eof": True}, {"cursor": False},
])
@pytest.mark.asyncio
async def test_recovery_rejects_gaps_invalid_progress_and_false_eof(changes):
    sandbox, data = paged_sandbox("x" * 30000)
    valid = sandbox.view_shell.side_effect
    async def bad(*args, **kwargs):
        result = await valid(*args, **kwargs)
        result.data["output_page"].update(changes)
        return result
    sandbox.view_shell.side_effect = bad
    with pytest.raises(ShellOutputUnavailable):
        await recover_shell_output(sandbox, "shell", data)
    assert sandbox.view_shell.await_count == 1


@pytest.mark.asyncio
async def test_recovery_rejects_replaced_generation():
    sandbox, data = paged_sandbox("x" * 30000)
    valid = sandbox.view_shell.side_effect
    async def bad(*args, **kwargs):
        result = await valid(*args, **kwargs)
        result.data["output_metadata"]["output_id"] = "b" * 32
        return result
    sandbox.view_shell.side_effect = bad
    with pytest.raises(ShellOutputUnavailable):
        await recover_shell_output(sandbox, "shell", data)


@pytest.mark.asyncio
async def test_legacy_output_does_not_request_new_protocol():
    sandbox = SimpleNamespace(view_shell=AsyncMock())
    assert await recover_shell_output(sandbox, "shell", {"output": "legacy"}) == "legacy"
    sandbox.view_shell.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_cancellation_propagates_without_retry():
    sandbox, data = paged_sandbox("x" * 30000)
    sandbox.view_shell.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await recover_shell_output(sandbox, "shell", data)
    assert sandbox.view_shell.await_count == 1


@pytest.mark.asyncio
async def test_plugin_large_json_recovers_before_schema_validation_and_releases(tmp_path):
    text = json.dumps({"data": "x" * 100000})
    sandbox, data = paged_sandbox(text)
    sandbox.exec_command.return_value = ToolResult(success=True, data=data)
    toolkit = PluginToolkit(sandbox, session_id="owner", plugins_dir=tmp_path)
    toolkit._definitions["fixture"] = {"parameters": {"type": "object"},
        "output_schema": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]}}
    result = await toolkit.call_tool("fixture", {})
    assert result.success and result.data["result"] == {"data": "x" * 100000}
    assert sandbox.exec_command.await_count == 1
    assert sandbox.release_shell.await_count == 1


@pytest.mark.asyncio
async def test_plugin_incomplete_output_is_quarantined_and_not_reexecuted(tmp_path):
    sandbox, data = paged_sandbox("x" * 100000)
    data["output_metadata"]["log_status"] = "unavailable"
    sandbox.exec_command.return_value = ToolResult(success=True, data=data)
    toolkit = PluginToolkit(sandbox, session_id="owner", plugins_dir=tmp_path)
    toolkit._definitions["fixture"] = {"parameters": {"type": "object"}}
    result = await toolkit.call_tool("fixture", {})
    assert not result.success
    assert "bounded preview" not in result.model_dump_json()
    assert sandbox.exec_command.await_count == 1
    assert sandbox.release_shell.await_count == 1
    sandbox.view_shell.assert_not_called()


@pytest.mark.asyncio
async def test_shell_run_keeps_preview_while_internal_contract_consumer_recovers():
    sandbox, data = paged_sandbox("x" * 100000)
    sandbox.exec_command.return_value = ToolResult(success=True, data=data)
    toolkit = ShellToolkit(sandbox)
    preview = await toolkit.get_tool("shell_run")._arun(id="shell", exec_dir="/tmp", command="fixture")
    assert preview.data["output"] == "bounded preview"
    sandbox.view_shell.assert_not_called()
    restored = await toolkit._run_bounded_command(id="shell", exec_dir="/tmp", command="fixture", timeout_seconds=30)
    assert restored.data["output"] == "x" * 100000


@pytest.mark.asyncio
async def test_adapter_page_arguments_are_additive_and_preserve_legacy_wire():
    adapter = DockerSandbox.__new__(DockerSandbox)
    adapter.base_url = "http://sandbox"
    adapter.client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(json=lambda: {"success": True, "data": {}})))
    await adapter.view_shell("shell", True)
    assert adapter.client.post.call_args.kwargs["json"] == {"id": "shell", "console": True}
    await adapter.view_shell("shell", output_id="a" * 32, cursor=123, max_bytes=4096)
    assert adapter.client.post.call_args.kwargs["json"] == {"id": "shell", "console": False,
        "output_id": "a" * 32, "cursor": 123, "max_bytes": 4096}
