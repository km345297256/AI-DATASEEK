import asyncio

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.interceptors import (
    ToolExecutionTimeoutError,
    ToolTimeoutInterceptor,
)
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _response(data, *, success=True, message="legacy response"):
    return _Response({"success": success, "message": message, "data": data})


def test_adapter_marks_legacy_nonzero_shell_exec_as_failure():
    result = DockerSandbox._tool_result_from_response(
        _response({"status": "completed", "returncode": 3, "output": "failed"}),
        "shell_exec",
    )

    assert result.success is False
    assert result.message == "Command failed with return code: 3"
    assert result.data["output"] == "failed"


def test_adapter_keeps_running_shell_wait_successful():
    result = DockerSandbox._tool_result_from_response(
        _response({"status": "running", "returncode": None}),
        "shell_wait",
    )

    assert result.success is True
    assert result.data["status"] == "running"


def test_adapter_marks_legacy_noop_replace_as_failure():
    result = DockerSandbox._tool_result_from_response(
        _response({"file": "/tmp/example.txt", "replaced_count": 0}),
        "file_replace",
    )

    assert result.success is False
    assert result.message == "Replacement made no changes: target text was not found"


class _Sandbox:
    def __init__(self, returncode=0, still_running=False):
        self.returncode = returncode
        self.still_running = still_running
        self.wait_seconds = None
        self.exec_dir = None
        self.command = None
        self.kill_calls = []

    async def exec_command(self, session_id, exec_dir, command):
        self.exec_dir = exec_dir
        self.command = command
        return ToolResult(
            success=True,
            message="started",
            data={
                "session_id": session_id,
                "command": command,
                "status": "running",
                "returncode": None,
            },
        )

    async def wait_for_process(self, session_id, seconds):
        self.wait_seconds = seconds
        if self.still_running:
            return ToolResult(
                success=True,
                message="running",
                data={"status": "running", "returncode": None},
            )
        return ToolResult(
            success=self.returncode == 0,
            message="completed",
            data={"status": "completed", "returncode": self.returncode},
        )

    async def view_shell(self, session_id):
        return ToolResult(
            success=True,
            message="viewed",
            data={"session_id": session_id, "output": "analysis complete\n"},
        )

    async def kill_process(self, session_id):
        self.kill_calls.append(session_id)
        return ToolResult(
            success=True,
            message="terminated",
            data={"session_id": session_id, "status": "terminated"},
        )


async def _invoke_shell_run(sandbox, *, timeout_seconds=30):
    toolkit = ShellToolkit(sandbox)
    shell_run = toolkit.get_tool("shell_run")
    assert shell_run is not None
    return await shell_run._arun(
        id="shell-1",
        exec_dir="/home/ubuntu",
        command="python analyze.py",
        timeout_seconds=timeout_seconds,
    )


async def test_shell_run_returns_completed_output_in_one_tool_call():
    sandbox = _Sandbox(returncode=0)

    result = await _invoke_shell_run(sandbox, timeout_seconds=45)

    assert sandbox.wait_seconds == 45
    assert sandbox.kill_calls == []
    assert result.success is True
    assert result.data == {
        "session_id": "shell-1",
        "command": "python analyze.py",
        "status": "completed",
        "returncode": 0,
        "output": "analysis complete\n",
    }


async def test_shell_run_preserves_nonzero_return_code_and_output():
    result = await _invoke_shell_run(_Sandbox(returncode=11))

    assert result.success is False
    assert result.message == "Command failed with return code: 11"
    assert result.data["returncode"] == 11
    assert result.data["output"] == "analysis complete\n"


async def test_shell_run_terminates_process_when_bounded_wait_expires_without_interceptor():
    sandbox = _Sandbox(still_running=True)

    result = await _invoke_shell_run(sandbox, timeout_seconds=500)

    # Leave host-side headroom below the production interceptor's 120 second
    # outer deadline so this fallback can clean up and return deterministically.
    assert sandbox.wait_seconds == 119
    assert sandbox.kill_calls == ["shell-1"]
    assert result.success is False
    assert result.data["status"] == "timed_out"
    assert result.data["returncode"] is None


class _BlockingShellSandbox(_Sandbox):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def exec_command(self, session_id, exec_dir, command):
        self.started.set()
        await asyncio.Event().wait()

    async def wait_for_process(self, session_id, seconds):
        self.started.set()
        await asyncio.Event().wait()

    async def view_shell(self, session_id):
        self.started.set()
        await asyncio.Event().wait()


async def test_shell_exec_pipeline_timeout_cooperatively_kills_process_once():
    sandbox = _BlockingShellSandbox()
    toolkit = ShellToolkit(sandbox)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        ToolTimeoutInterceptor(0.01, maximum_timeout_seconds=1),
    ])

    with pytest.raises(ToolExecutionTimeoutError):
        await toolkit.get_tool("shell_exec").ainvoke({
            "id": "call-shell-exec",
            "name": "shell_exec",
            "args": {
                "id": "process-1",
                "exec_dir": "/home/ubuntu",
                "command": "sleep 600",
            },
        })

    assert sandbox.kill_calls == ["process-1"]


async def test_shell_wait_external_cancellation_kills_process_once():
    sandbox = _BlockingShellSandbox()
    toolkit = ShellToolkit(sandbox)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        ToolTimeoutInterceptor(5, maximum_timeout_seconds=5),
    ])

    task = asyncio.create_task(toolkit.get_tool("shell_wait").ainvoke({
        "id": "call-shell-wait",
        "name": "shell_wait",
        "args": {"id": "process-2", "seconds": 60},
    }))
    await sandbox.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sandbox.kill_calls == ["process-2"]


async def test_shell_view_timeout_does_not_kill_unrelated_process():
    sandbox = _BlockingShellSandbox()
    toolkit = ShellToolkit(sandbox)
    toolkit.tool_execution_pipeline = ToolExecutionPipeline([
        ToolTimeoutInterceptor(0.01, maximum_timeout_seconds=1),
    ])

    with pytest.raises(ToolExecutionTimeoutError):
        await toolkit.get_tool("shell_view").ainvoke({
            "id": "call-shell-view",
            "name": "shell_view",
            "args": {"id": "process-3"},
        })

    assert sandbox.kill_calls == []


async def test_dataset_unpack_uses_one_quoted_bounded_command():
    sandbox = _Sandbox(returncode=0)
    toolkit = ShellToolkit(sandbox)
    unpack = toolkit.get_tool("dataset_unpack")
    assert unpack is not None

    result = await unpack._arun(
        id="unpack-1",
        archive_path="/home/ubuntu/datasets/demo/a file; touch nope.zip",
        output_dir="/home/ubuntu/output/unpacked demo",
        timeout_seconds=500,
    )

    assert result.success is True
    assert sandbox.exec_dir == "/home/ubuntu"
    assert sandbox.wait_seconds == 119
    assert sandbox.command == (
        "ai-dataseek-unpack '/home/ubuntu/datasets/demo/a file; touch nope.zip' "
        "--output '/home/ubuntu/output/unpacked demo' --timeout-seconds 120"
    )


async def test_dataset_quicklook_uses_one_quoted_bounded_command():
    sandbox = _Sandbox(returncode=0)
    toolkit = ShellToolkit(sandbox)
    quicklook = toolkit.get_tool("dataset_quicklook")
    assert quicklook is not None

    result = await quicklook._arun(
        id="quicklook-1",
        input_path="/home/ubuntu/datasets/demo/data; touch nope.zip",
        output_dir="/home/ubuntu/output/quick look",
        max_plots=99,
        timeout_seconds=500,
    )

    assert result.success is True
    assert sandbox.exec_dir == "/home/ubuntu"
    assert sandbox.wait_seconds == 119
    assert sandbox.command == (
        "ai-dataseek-quicklook '/home/ubuntu/datasets/demo/data; touch nope.zip' "
        "--output '/home/ubuntu/output/quick look' --max-plots 4 --timeout-seconds 120"
    )


async def test_scientific_tools_build_one_quoted_bounded_command():
    sandbox = _Sandbox(returncode=0)
    toolkit = ShellToolkit(sandbox)
    subset = toolkit.get_tool("scientific_subset")
    assert subset is not None

    result = await subset._arun(
        id="science-1",
        input_path="/home/ubuntu/datasets/demo/a file; touch nope.nc",
        output_path="/home/ubuntu/output/subset file.nc",
        variable="rain rate",
        bbox=[100, 20, 110, 30],
        dimension_indices={"time": 0},
        timeout_seconds=500,
    )

    assert result.success is True
    assert sandbox.wait_seconds == 119
    assert sandbox.command == (
        "ai-dataseek-scientific subset '/home/ubuntu/datasets/demo/a file; touch nope.nc' "
        "--variable 'rain rate' --dimension-indices '{\"time\": 0}' "
        "--output '/home/ubuntu/output/subset file.nc' --bbox '[100, 20, 110, 30]'"
    )


async def test_netcdf_visualization_bundle_builds_one_bounded_command():
    sandbox = _Sandbox(returncode=0)
    toolkit = ShellToolkit(sandbox)
    visualize_bundle = toolkit.get_tool("scientific_netcdf_visualize")
    assert visualize_bundle is not None

    result = await visualize_bundle._arun(
        id="visualize-1",
        input_path="/home/ubuntu/datasets/demo/a file; touch nope.nc",
        output_dir="/home/ubuntu/output/netcdf charts",
        variable="rain rate",
        max_plots=99,
        dimension_indices={"level": 2},
        timeout_seconds=500,
    )

    assert result.success is True
    assert sandbox.wait_seconds == 119
    assert sandbox.command == (
        "ai-dataseek-scientific visualize-bundle "
        "'/home/ubuntu/datasets/demo/a file; touch nope.nc' "
        "--variable 'rain rate' --dimension-indices '{\"level\": 2}' "
        "--output '/home/ubuntu/output/netcdf charts' --max-plots 4"
    )


async def test_scientific_recipe_tools_build_one_quoted_bounded_command():
    sandbox = _Sandbox(returncode=0)
    toolkit = ShellToolkit(sandbox)
    region_timeseries = toolkit.get_tool("scientific_region_timeseries")
    assert region_timeseries is not None

    result = await region_timeseries._arun(
        id="recipe-1",
        input_path="/home/ubuntu/datasets/demo/a file; touch nope.nc",
        variable="rain rate",
        method="median",
        polygon=[[100.5, 20.25], [110.5, 20.25], [105.5, 30.25]],
        time_coordinate="valid time",
        dimension_indices={"level": 2},
        max_points=9999,
        timeout_seconds=500,
    )

    assert result.success is True
    assert sandbox.exec_dir == "/home/ubuntu"
    assert sandbox.wait_seconds == 119
    assert sandbox.command == (
        "ai-dataseek-scientific-recipe region-timeseries "
        "'/home/ubuntu/datasets/demo/a file; touch nope.nc' "
        "--variable 'rain rate' --dimension-indices '{\"level\": 2}' "
        "--max-points 2000 --method median "
        "--polygon '[[100.5, 20.25], [110.5, 20.25], [105.5, 30.25]]' "
        "--time-coordinate 'valid time'"
    )
