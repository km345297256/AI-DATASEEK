import asyncio
import logging
import os
import shlex
import uuid

import pytest

from app.core.exceptions import AppException, ResourceNotFoundException
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


class _TerminableProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False

    def terminate(self):
        self.terminate_called = True
        self.returncode = -15

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


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


@pytest.mark.asyncio
async def test_release_shell_is_idempotent_and_discards_completed_history():
    session_id = f"release-{uuid.uuid4().hex}"
    _register_process(session_id, _CompletedProcess())
    service = ShellService()

    first = await service.release_shell(session_id)
    second = await service.release_shell(session_id)

    assert first.status == "released"
    assert first.returncode == 0
    assert second.status == "released"
    assert session_id not in ShellService.active_shells


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


@pytest.mark.asyncio
async def test_exec_failure_metadata_and_logs_do_not_echo_private_values(
    monkeypatch,
    caplog,
):
    service = ShellService()
    session_id = f"exec-failure-{uuid.uuid4().hex}"
    secret_command = "printf super-secret-command-value"

    async def fail_to_create_process(_command, _exec_dir):
        raise RuntimeError("spawn-secret /Users/alice/private/data.nc")

    monkeypatch.setattr(service, "_create_process", fail_to_create_process)

    with caplog.at_level(logging.DEBUG, logger="app.services.shell"):
        with pytest.raises(AppException) as raised:
            await service.exec_command(session_id, "/tmp", secret_command)

    assert raised.value.data == {
        "command_bytes": len(secret_command.encode("utf-8")),
    }
    assert raised.value.message == "Command execution failed"
    assert secret_command not in repr(raised.value.data)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "session=session:sha256:" in logs
    assert session_id not in logs
    assert secret_command not in logs
    assert "spawn-secret" not in logs
    assert "/Users/alice" not in logs
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("termination_action", ["kill", "release"])
async def test_pre_registration_termination_cancels_blocked_exec(
    monkeypatch,
    termination_action,
):
    service = ShellService()
    session_id = f"pre-registration-{termination_action}-{uuid.uuid4().hex}"
    create_started = asyncio.Event()
    allow_create_to_return = asyncio.Event()
    created_process = _TerminableProcess(pid=987654)
    terminated_processes = []

    async def blocked_create_process(_command, _exec_dir):
        create_started.set()
        await allow_create_to_return.wait()
        return created_process

    async def record_termination(process, timeout_seconds, process_group=None):
        assert timeout_seconds == service.KILL_PROCESS_TERMINATION_GRACE_SECONDS
        terminated_processes.append((process, process_group))
        process.returncode = -15
        return True

    monkeypatch.setattr(service, "_create_process", blocked_create_process)
    monkeypatch.setattr(service, "_terminate_process_tree", record_termination)

    exec_task = asyncio.create_task(
        service.exec_command(session_id, "/tmp", "printf race")
    )
    await asyncio.wait_for(create_started.wait(), timeout=1)

    if termination_action == "kill":
        termination_result = await asyncio.wait_for(
            service.kill_process(session_id),
            timeout=1,
        )
        assert termination_result.status == "terminated"
    else:
        termination_result = await asyncio.wait_for(
            service.release_shell(session_id),
            timeout=1,
        )
        assert termination_result.status == "released"

    # The cancellation endpoint has completed while process creation remains
    # behind the barrier. The late process must be discarded, never published.
    assert exec_task.done() is False
    assert session_id not in ShellService.active_shells
    allow_create_to_return.set()

    with pytest.raises(AppException, match="cancelled during process creation"):
        await asyncio.wait_for(exec_task, timeout=1)

    assert terminated_processes == [(created_process, None)]
    assert created_process.returncode == -15
    assert session_id not in ShellService.active_shells
    assert session_id not in ShellService._pending_execs


@pytest.mark.asyncio
@pytest.mark.parametrize("termination_action", ["kill", "release"])
async def test_termination_overtaking_exec_request_cancels_next_generation(
    monkeypatch,
    termination_action,
):
    service = ShellService()
    session_id = f"overtaking-{termination_action}-{uuid.uuid4().hex}"
    create_calls = 0

    async def record_create(_command, _exec_dir):
        nonlocal create_calls
        create_calls += 1
        return _TerminableProcess(pid=987655)

    monkeypatch.setattr(service, "_create_process", record_create)

    if termination_action == "kill":
        with pytest.raises(ResourceNotFoundException):
            await service.kill_process(session_id)
    else:
        result = await service.release_shell(session_id)
        assert result.status == "released"

    assert session_id in ShellService._pre_cancelled_execs

    with pytest.raises(AppException, match="cancelled before process creation"):
        await service.exec_command(session_id, "/tmp", "printf too-late")

    assert create_calls == 0
    assert session_id not in ShellService.active_shells
    assert session_id not in ShellService._pending_execs
    assert session_id not in ShellService._pre_cancelled_execs


@pytest.mark.asyncio
async def test_pre_cancel_tombstone_expires_before_unrelated_late_exec(monkeypatch):
    service = ShellService()
    service.PRE_CANCELLED_EXEC_TTL_SECONDS = 5.0
    session_id = f"expired-pre-cancel-{uuid.uuid4().hex}"
    now = 100.0
    process = _CompletedProcess()
    process.stdout = _ChunkedOutput([])
    create_calls = 0

    monkeypatch.setattr(service, "_monotonic", lambda: now)

    async def record_create(_command, _exec_dir):
        nonlocal create_calls
        create_calls += 1
        return process

    monkeypatch.setattr(service, "_create_process", record_create)

    result = await service.release_shell(session_id)
    assert result.status == "released"
    assert session_id in ShellService._pre_cancelled_execs

    now += service.PRE_CANCELLED_EXEC_TTL_SECONDS + 0.001
    try:
        exec_result = await service.exec_command(
            session_id,
            "/tmp",
            "printf after-expiry",
        )
    finally:
        ShellService.active_shells.pop(session_id, None)
        ShellService._pending_execs.pop(session_id, None)
        ShellService._pre_cancelled_execs.pop(session_id, None)

    assert create_calls == 1
    assert exec_result.status == "completed"
    assert exec_result.returncode == 0


@pytest.mark.asyncio
async def test_pre_cancel_tombstone_store_is_capacity_bounded(monkeypatch):
    service = ShellService()
    service.MAX_PRE_CANCELLED_EXEC_SESSIONS = 2
    monkeypatch.setattr(service, "_monotonic", lambda: 100.0)
    session_ids = [f"bounded-pre-cancel-{uuid.uuid4().hex}" for _ in range(3)]

    with ShellService._session_state_lock:
        original_tombstones = list(ShellService._pre_cancelled_execs.items())
        ShellService._pre_cancelled_execs.clear()

    try:
        for session_id in session_ids:
            result = await service.release_shell(session_id)
            assert result.status == "released"

        assert list(ShellService._pre_cancelled_execs) == session_ids[-2:]
    finally:
        with ShellService._session_state_lock:
            ShellService._pre_cancelled_execs.clear()
            ShellService._pre_cancelled_execs.update(original_tombstones)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
async def test_kill_never_signals_the_sandbox_current_process_group(monkeypatch):
    session_id = f"safe-group-{uuid.uuid4().hex}"
    current_process_group_id = os.getpgrp()
    process = _TerminableProcess(current_process_group_id)
    _register_process(session_id, process)

    def fail_if_group_is_signalled(_process_group_id, _signal):
        pytest.fail("the sandbox server process group must never be signalled")

    monkeypatch.setattr(os, "getpgid", lambda _pid: current_process_group_id)
    monkeypatch.setattr(os, "killpg", fail_if_group_is_signalled)

    try:
        result = await ShellService().kill_process(session_id)
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert result.status == "terminated"
    assert process.terminate_called is True
    assert process.kill_called is False


async def _wait_until(predicate, timeout_seconds=2):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
@pytest.mark.parametrize("termination_action", ["kill", "replace"])
async def test_command_grandchild_is_terminated_with_its_process_group(
    tmp_path,
    termination_action,
):
    session_id = f"process-tree-{termination_action}-{uuid.uuid4().hex}"
    pid_path = tmp_path / "grandchild.pid"
    survived_path = tmp_path / "grandchild-survived"
    service = ShellService()
    service.EXEC_COMPLETION_GRACE_SECONDS = 0
    service.KILL_PROCESS_TERMINATION_GRACE_SECONDS = 0.1
    service.REPLACED_PROCESS_TERMINATION_GRACE_SECONDS = 0.1
    service.FORCE_KILL_WAIT_SECONDS = 1

    # The background bash is a real grandchild.  It ignores SIGTERM and would
    # create the marker unless the grace-period escalation reaches the whole
    # process group with SIGKILL.
    child_command = (
        "(trap '' TERM; "
        f"printf '%s' \"$BASHPID\" > {shlex.quote(str(pid_path))}; "
        "sleep 0.6; "
        f"printf survived > {shlex.quote(str(survived_path))}) & wait"
    )
    command = f"/bin/bash -c {shlex.quote(child_command)}"
    original_process = None

    try:
        result = await service.exec_command(session_id, str(tmp_path), command)
        assert result.status == "running"
        assert await _wait_until(pid_path.exists)

        original_process = ShellService.active_shells[session_id]["process"]
        process_group_id = os.getpgid(original_process.pid)
        assert process_group_id == original_process.pid
        assert process_group_id != os.getpgrp()

        if termination_action == "kill":
            kill_result = await service.kill_process(session_id)
            assert kill_result.status == "terminated"
        else:
            service.EXEC_COMPLETION_GRACE_SECONDS = 1
            replacement_result = await service.exec_command(
                session_id,
                str(tmp_path),
                "printf replacement",
            )
            assert replacement_result.status == "completed"
            assert replacement_result.output == "replacement"

        assert await _wait_until(
            lambda: not service._process_group_exists(process_group_id)
        )
        await asyncio.sleep(0.7)
        assert not survived_path.exists()
    finally:
        if original_process is not None and original_process.returncode is None:
            await service._terminate_process_tree(original_process, 0.1)

        shell = ShellService.active_shells.pop(session_id, None)
        if shell:
            process = shell.get("process")
            if process and process.returncode is None:
                await service._terminate_process_tree(process, 0.1)
            reader_task = shell.get("reader_task")
            if reader_task and not reader_task.done():
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
@pytest.mark.parametrize("termination_action", ["kill", "replace"])
async def test_completed_leader_does_not_orphan_its_live_grandchild_group(
    tmp_path,
    termination_action,
):
    session_id = f"orphan-group-{termination_action}-{uuid.uuid4().hex}"
    pid_path = tmp_path / "orphan-grandchild.pid"
    survived_path = tmp_path / "orphan-grandchild-survived"
    service = ShellService()
    service.EXEC_COMPLETION_GRACE_SECONDS = 1
    service.OUTPUT_READER_DRAIN_GRACE_SECONDS = 0.05
    service.KILL_PROCESS_TERMINATION_GRACE_SECONDS = 0.1
    service.REPLACED_PROCESS_TERMINATION_GRACE_SECONDS = 0.1

    # The leader exits successfully while a background grandchild remains in
    # its session.  This is the daemonization edge case for cached PGID cleanup.
    child_command = (
        "(trap '' TERM; "
        f"printf '%s' \"$BASHPID\" > {shlex.quote(str(pid_path))}; "
        "sleep 0.6; "
        f"printf survived > {shlex.quote(str(survived_path))}) & exit 0"
    )
    command = f"/bin/bash -c {shlex.quote(child_command)}"
    original_process = None
    process_group = None

    try:
        result = await service.exec_command(session_id, str(tmp_path), command)
        assert result.status == "completed"
        assert result.returncode == 0
        assert await _wait_until(pid_path.exists)

        shell = ShellService.active_shells[session_id]
        original_process = shell["process"]
        process_group = shell["process_group"]
        assert original_process.returncode == 0
        assert process_group["process_group_id"] == original_process.pid
        assert service._process_group_identity_is_current(process_group)

        if termination_action == "kill":
            kill_result = await service.kill_process(session_id)
            assert kill_result.status == "terminated"
        else:
            replacement_result = await service.exec_command(
                session_id,
                str(tmp_path),
                "printf replacement-after-orphan",
            )
            assert replacement_result.status == "completed"
            assert replacement_result.output == "replacement-after-orphan"

        assert await _wait_until(
            lambda: not service._process_group_identity_is_current(process_group)
        )
        await asyncio.sleep(0.7)
        assert not survived_path.exists()
    finally:
        if (
            original_process is not None
            and process_group is not None
            and service._process_group_identity_is_current(process_group)
        ):
            await service._terminate_process_tree(
                original_process,
                0.1,
                process_group=process_group,
            )

        shell = ShellService.active_shells.pop(session_id, None)
        if shell:
            process = shell.get("process")
            if process and process.returncode is None:
                await service._terminate_process_tree(
                    process,
                    0.1,
                    process_group=shell.get("process_group"),
                )
            reader_task = shell.get("reader_task")
            if reader_task and not reader_task.done():
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
async def test_cached_group_rejects_a_recycled_leader_identity(monkeypatch):
    session_id = f"recycled-group-{uuid.uuid4().hex}"
    service = ShellService()
    process = _CompletedProcess()
    process_group = {
        "process_group_id": 43210,
        "session_id": 43210,
        "leader_pid": 43210,
        "leader_start_time": 100,
        "retired": False,
    }
    _register_process(session_id, process)
    ShellService.active_shells[session_id]["process_group"] = process_group

    monkeypatch.setattr(service, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(
        service,
        "_linux_process_group_members",
        lambda _pgid: [
            {
                "pid": 43210,
                "state": "S",
                "process_group_id": 43210,
                "session_id": 43210,
                "start_time": 200,
            }
        ],
    )

    def fail_if_recycled_group_is_signalled(_process_group_id, _signal):
        pytest.fail("a recycled process group must never be signalled")

    monkeypatch.setattr(os, "killpg", fail_if_recycled_group_is_signalled)

    try:
        result = await service.kill_process(session_id)
    finally:
        ShellService.active_shells.pop(session_id, None)

    assert result.status == "already_terminated"
    assert process_group["retired"] is True
