import asyncio
import uuid

import pytest

from app.models.shell import ConsoleRecord
from app.services.shell import ShellService
from conftest import BASE_URL


class _RunningProcess:
    returncode = None

    async def wait(self):
        await asyncio.sleep(3600)


class _CompletedProcess:
    returncode = 0

    async def wait(self):
        return self.returncode


class _FailedProcess(_CompletedProcess):
    returncode = 7


class _ChunkedOutput:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _OutputProcess:
    def __init__(self, chunks):
        self.stdout = _ChunkedOutput(chunks)


def _register_process(session_id, process):
    ShellService.active_shells[session_id] = {
        "process": process,
        "exec_dir": "/tmp",
        "output": "",
        "console": [ConsoleRecord(ps1="$", command="test", output="")],
    }


@pytest.mark.asyncio
async def test_wait_timeout_returns_running_state():
    session_id = f"running-{uuid.uuid4().hex}"
    _register_process(session_id, _RunningProcess())
    try:
        result = await ShellService().wait_for_process(session_id, seconds=0)
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert result.status == "running"
    assert result.returncode is None


@pytest.mark.asyncio
async def test_wait_returns_completed_state_and_return_code():
    session_id = f"completed-{uuid.uuid4().hex}"
    _register_process(session_id, _CompletedProcess())
    try:
        result = await ShellService().wait_for_process(session_id, seconds=1)
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert result.status == "completed"
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_zero_second_wait_observes_already_completed_process():
    session_id = f"completed-zero-{uuid.uuid4().hex}"
    _register_process(session_id, _CompletedProcess())
    try:
        result = await ShellService().wait_for_process(session_id, seconds=0)
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert result.status == "completed"
    assert result.returncode == 0


@pytest.mark.shell_api
def test_wait_api_keeps_running_process_as_success(client):
    session_id = f"api-running-{uuid.uuid4().hex}"
    _register_process(session_id, _RunningProcess())
    try:
        response = client.post(
            f"{BASE_URL}/api/v1/shell/wait",
            json={"id": session_id, "seconds": 0},
        )
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["message"] == "Process is still running"
    assert payload["data"] == {"status": "running", "returncode": None}


@pytest.mark.shell_api
def test_wait_api_marks_nonzero_return_code_as_failure(client):
    session_id = f"api-failed-{uuid.uuid4().hex}"
    _register_process(session_id, _FailedProcess())
    try:
        response = client.post(
            f"{BASE_URL}/api/v1/shell/wait",
            json={"id": session_id, "seconds": 1},
        )
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["message"] == "Process failed with return code: 7"
    assert payload["data"] == {"status": "completed", "returncode": 7}


@pytest.mark.shell_api
def test_exec_api_marks_nonzero_return_code_as_failure(client):
    session_id = f"api-exec-failed-{uuid.uuid4().hex}"
    try:
        response = client.post(
            f"{BASE_URL}/api/v1/shell/exec",
            json={"id": session_id, "exec_dir": "/tmp", "command": "exit 9"},
        )
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["message"] == "Command failed with return code: 9"
    assert payload["data"]["status"] == "completed"
    assert payload["data"]["returncode"] == 9


@pytest.mark.shell_api
def test_wait_api_preserves_missing_session_as_error(client):
    response = client.post(
        f"{BASE_URL}/api/v1/shell/wait",
        json={"id": f"missing-{uuid.uuid4().hex}", "seconds": 0},
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["success"] is False
    assert "does not exist" in payload["message"]


@pytest.mark.asyncio
async def test_output_reader_preserves_split_utf8_characters():
    session_id = f"utf8-{uuid.uuid4().hex}"
    encoded = "数据可视化完成".encode("utf-8")
    chunks = [encoded[:2], encoded[2:5], encoded[5:9], encoded[9:]]
    _register_process(session_id, _OutputProcess(chunks))
    try:
        await ShellService()._start_output_reader(
            session_id,
            ShellService.active_shells[session_id]["process"],
        )
        shell = ShellService.active_shells[session_id]
        assert shell["output"] == "数据可视化完成"
        assert shell["console"][-1].output == "数据可视化完成"
    finally:
        ShellService.active_shells.pop(session_id, None)


@pytest.mark.asyncio
async def test_exec_drains_combined_stdout_and_stderr_before_returning():
    session_id = f"combined-output-{uuid.uuid4().hex}"
    service = ShellService()
    try:
        result = await service.exec_command(
            session_id,
            "/tmp",
            "printf '标准输出'; printf '标准错误' >&2",
        )

        assert result.status == "completed"
        assert result.returncode == 0
        assert result.output == "标准输出标准错误"
        assert ShellService.active_shells[session_id]["reader_task"].done()
    finally:
        shell = ShellService.active_shells.pop(session_id, None)
        if shell:
            reader_task = shell.get("reader_task")
            if reader_task and not reader_task.done():
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
