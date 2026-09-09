"""Launch-bound receipts: no replay, no shell-generation or terminal-state guesses."""

import asyncio
import hashlib
import importlib
import json
import os
import uuid
from collections import OrderedDict
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.exceptions import AppException, BadRequestException
from app.models.shell import ShellExecutionReceipt
from app.schemas.shell import ShellExecRequest, ShellOperationStatusRequest
from app.services.shell import ShellService
from conftest import BASE_URL


@pytest.fixture
def service(monkeypatch):
    value = ShellService()
    value.active_shells = {}
    value._pending_execs = {}
    value._pre_cancelled_execs = OrderedDict()
    value._execution_operations = {}
    value.EXEC_COMPLETION_GRACE_SECONDS = 0
    value.OUTPUT_READER_DRAIN_GRACE_SECONDS = 0.05
    monkeypatch.setattr(importlib.import_module("app.api.v1.shell"), "shell_service", value)
    return value


def _digest(command, directory):
    return hashlib.sha256(json.dumps(
        {"command": command, "exec_dir": os.path.abspath(os.path.normpath(directory))},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


class Process:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.stdout = None
        self._dataseek_process_group = {
            "process_group_id": 900001, "session_id": 900001,
            "leader_pid": 900001, "leader_start_time": 100, "retired": False,
        }

    async def wait(self):
        if self.returncode is None:
            await asyncio.Future()
        return self.returncode


def _fake_launch(service, monkeypatch, returncode=0, quiescent=True):
    process = Process(returncode)
    launcher = AsyncMock(return_value=process)
    monkeypatch.setattr(service, "_create_process", launcher)
    monkeypatch.setattr(service, "_receipt_group_is_quiescent", lambda _group: quiescent)
    return process, launcher


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 7, -15])
async def test_receipt_has_canonical_digest_and_strict_exit_identity(service, monkeypatch, returncode):
    _fake_launch(service, monkeypatch, returncode)
    operation_id = uuid.uuid4().hex
    result = await service.exec_command("shell", "/tmp/unused/../", "printf '数据'", operation_id=operation_id)
    receipt = result.execution_receipt
    assert receipt.model_dump() == {
        "version": 1, "operation_id": operation_id,
        "command_digest": _digest("printf '数据'", "/tmp"),
        "server_instance_id": service.SERVER_INSTANCE_ID,
        "state": "exited", "returncode": returncode, "process_tree_quiescent": True,
    }
    assert await service.operation_status("other-shell", operation_id) is None
    assert await service.operation_status("shell", uuid.uuid4().hex) is None


@pytest.mark.asyncio
async def test_duplicate_operation_never_launches_even_in_another_shell(service, monkeypatch):
    _, launcher = _fake_launch(service, monkeypatch)
    operation_id = uuid.uuid4().hex
    await service.exec_command("first", "/tmp", "one", operation_id=operation_id)
    for session_id, command in [("first", "one"), ("first", "two"), ("other", "one")]:
        with pytest.raises(BadRequestException, match="already used"):
            await service.exec_command(session_id, "/tmp", command, operation_id=operation_id)
    assert launcher.await_count == 1


@pytest.mark.asyncio
async def test_starting_is_registered_before_spawn_and_duplicate_race_rejected(service, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    process = Process(0)
    async def spawn(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return process
    monkeypatch.setattr(service, "_create_process", spawn)
    monkeypatch.setattr(service, "_receipt_group_is_quiescent", lambda _group: True)
    operation_id = uuid.uuid4().hex
    task = asyncio.create_task(service.exec_command("shell", "/tmp", "one", operation_id=operation_id))
    await entered.wait()
    receipt = await service.operation_status("shell", operation_id)
    assert receipt.state == "starting"
    assert receipt.process_tree_quiescent is False
    with pytest.raises(BadRequestException, match="already used"):
        await service.exec_command("shell", "/tmp", "one", operation_id=operation_id)
    release.set()
    await task


@pytest.mark.asyncio
async def test_pre_spawn_failure_is_not_started_but_spawn_failure_is_unknown(service, monkeypatch, tmp_path):
    not_started_id = uuid.uuid4().hex
    with pytest.raises(BadRequestException):
        await service.exec_command("missing", str(tmp_path / "missing"), "one", operation_id=not_started_id)
    not_started = await service.operation_status("missing", not_started_id)
    assert not_started.state == "not_started"
    assert not_started.returncode is None and not_started.process_tree_quiescent
    monkeypatch.setattr(service, "_create_process", AsyncMock(side_effect=OSError("spawn failed")))
    unknown_id = uuid.uuid4().hex
    with pytest.raises(AppException):
        await service.exec_command("spawn", "/tmp", "one", operation_id=unknown_id)
    unknown = await service.operation_status("spawn", unknown_id)
    assert unknown.state == "unknown"
    assert unknown.returncode is None and not unknown.process_tree_quiescent


@pytest.mark.asyncio
async def test_completed_operations_do_not_consume_pending_capacity_or_lose_replay_identity(service, monkeypatch):
    _fake_launch(service, monkeypatch)
    service.MAX_PENDING_EXECUTION_OPERATIONS = 1
    first = uuid.uuid4().hex
    await service.exec_command("first", "/tmp", "one", operation_id=first)
    await service.exec_command("second", "/tmp", "two", operation_id=uuid.uuid4().hex)
    assert len(service._execution_operations) == 2
    assert (await service.operation_status("first", first)).state == "exited"
    # Terminal records retain only their small receipt, not output buffers.
    assert service._execution_operations[first].process is None
    with pytest.raises(BadRequestException, match="already used"):
        await service.exec_command("first", "/tmp", "one", operation_id=first)


@pytest.mark.asyncio
async def test_unresolved_operations_still_have_a_concurrent_resource_bound(service, monkeypatch):
    _fake_launch(service, monkeypatch, 0, False)
    service.MAX_PENDING_EXECUTION_OPERATIONS = 1
    first = uuid.uuid4().hex
    await service.exec_command("first", "/tmp", "one", operation_id=first)
    with pytest.raises(BadRequestException, match="Pending execution operation capacity"):
        await service.exec_command("second", "/tmp", "two", operation_id=uuid.uuid4().hex)
    assert len(service._execution_operations) == 1
    receipt = await service.operation_status("first", first)
    assert receipt.state == "exited" and not receipt.process_tree_quiescent


@pytest.mark.asyncio
async def test_query_uses_original_process_after_shell_reuse(service, monkeypatch):
    original, _ = _fake_launch(service, monkeypatch, 7, False)
    first = uuid.uuid4().hex
    await service.exec_command("shell", "/tmp", "one", operation_id=first)
    replacement = Process(0)
    service.active_shells["shell"] = {"process": replacement, "operation_id": uuid.uuid4().hex}
    receipt = await service.operation_status("shell", first)
    assert receipt.returncode == 7 and not receipt.process_tree_quiescent
    assert service._execution_operations[first].process is original
    for call in (service.view_shell("shell", operation_id=first), service.wait_for_process("shell", 0, operation_id=first)):
        with pytest.raises(BadRequestException, match="no longer owns"):
            await call


@pytest.mark.asyncio
async def test_tracked_wait_rechecks_generation_after_await(service, monkeypatch):
    process, _ = _fake_launch(service, monkeypatch, None, False)
    operation_id = uuid.uuid4().hex
    await service.exec_command("shell", "/tmp", "one", operation_id=operation_id)
    async def wait_and_replace():
        service.active_shells["shell"] = {"process": Process(0), "operation_id": uuid.uuid4().hex}
        process.returncode = 0
        return 0
    monkeypatch.setattr(process, "wait", wait_and_replace)
    with pytest.raises(AppException, match="Failed to wait"):
        await service.wait_for_process("shell", 1, operation_id=operation_id)


@pytest.mark.asyncio
async def test_stale_kill_and_release_cannot_cancel_new_generation(service, monkeypatch):
    _fake_launch(service, monkeypatch, 0, False)
    old_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex
    await service.exec_command("shell", "/tmp", "one", operation_id=old_id)
    pending, _ = service._begin_exec("shell", new_id)
    terminator = AsyncMock()
    monkeypatch.setattr(service, "_terminate_process_tree", terminator)
    for method in (service.kill_process, service.release_shell):
        with pytest.raises(BadRequestException, match="no longer owns"):
            await method("shell", operation_id=old_id)
    assert not pending.cancelled
    terminator.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracked_cancel_during_spawn_only_cancels_matching_pending(service, monkeypatch):
    old_process, _ = _fake_launch(service, monkeypatch, 0, False)
    old_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex
    await service.exec_command("shell", "/tmp", "one", operation_id=old_id)
    service._register_execution_operation("shell", new_id, "two", "/tmp")
    pending, _ = service._begin_exec("shell", new_id)
    terminator = AsyncMock()
    monkeypatch.setattr(service, "_terminate_process_tree", terminator)
    await service.release_shell("shell", operation_id=new_id)
    assert pending.cancelled
    assert service.active_shells["shell"]["process"] is old_process
    terminator.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_read_failure_does_not_erase_exit_evidence(service, monkeypatch):
    _fake_launch(service, monkeypatch, 0)
    monkeypatch.setattr(service, "view_shell", AsyncMock(side_effect=OSError("output unavailable")))
    operation_id = uuid.uuid4().hex
    result = await service.exec_command("shell", "/tmp", "one", operation_id=operation_id)
    assert result.execution_receipt.state == "exited"
    assert result.execution_receipt.returncode == 0


@pytest.mark.asyncio
async def test_boolean_returncode_is_not_exit_evidence(service, monkeypatch):
    _fake_launch(service, monkeypatch, True)
    result = await service.exec_command("shell", "/tmp", "one", operation_id=uuid.uuid4().hex)
    assert result.execution_receipt.state == "unknown"
    assert result.execution_receipt.returncode is None


@pytest.mark.asyncio
async def test_restarted_or_empty_registry_cannot_claim_not_started(service, monkeypatch):
    _fake_launch(service, monkeypatch)
    operation_id = uuid.uuid4().hex
    await service.exec_command("shell", "/tmp", "one", operation_id=operation_id)
    service._execution_operations = {}
    assert await service.operation_status("shell", operation_id) is None


@pytest.mark.parametrize("field,value", [("returncode", True), ("returncode", "0"), ("process_tree_quiescent", 1), ("version", True)])
def test_receipt_rejects_coerced_evidence(field, value):
    receipt = {
        "version": 1, "operation_id": uuid.uuid4().hex, "command_digest": "a" * 64,
        "server_instance_id": uuid.uuid4().hex, "state": "exited", "returncode": 0,
        "process_tree_quiescent": True,
    }
    receipt[field] = value
    with pytest.raises(ValidationError):
        ShellExecutionReceipt(**receipt)


@pytest.mark.parametrize("operation_id", ["", "wrong", "A" * 32, 1])
def test_operation_ids_are_validated(operation_id):
    with pytest.raises(ValidationError):
        ShellExecRequest(command="true", operation_id=operation_id)
    with pytest.raises(ValidationError):
        ShellOperationStatusRequest(id="shell", operation_id=operation_id)


def test_retired_missing_or_unverifiable_group_is_not_quiescent(service, monkeypatch):
    assert not service._receipt_group_is_quiescent(None)
    monkeypatch.setattr(os, "killpg", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    group = Process()._dataseek_process_group
    group["retired"] = True
    assert not service._receipt_group_is_quiescent(group)


def test_group_scan_has_a_hard_bound_and_does_not_infer_missing_evidence(service, monkeypatch):
    monkeypatch.setattr(os, "killpg", lambda *_args: None)
    service.RECEIPT_PROCESS_SCAN_SECONDS = 0
    assert not service._receipt_group_is_quiescent(Process()._dataseek_process_group)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="Real process-group evidence requires POSIX")
async def test_live_child_blocks_quiescence_after_leader_exits_and_query_never_kills(service, monkeypatch):
    operation_id = uuid.uuid4().hex
    service.EXEC_COMPLETION_GRACE_SECONDS = 1
    try:
        await service.exec_command("shell", "/tmp", "sleep 30 >/dev/null 2>&1 & exit 0", operation_id=operation_id)
        actual_killpg = os.killpg
        seen_signals = []
        def read_only_killpg(pgid, sig):
            seen_signals.append(sig)
            assert sig == 0
            return actual_killpg(pgid, sig)
        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", read_only_killpg)
            receipt = await service.operation_status("shell", operation_id)
        assert receipt.state == "exited" and receipt.returncode == 0
        assert receipt.process_tree_quiescent is False
        assert seen_signals == [0]
    finally:
        await service.release_shell("shell")
    assert (await service.operation_status("shell", operation_id)).process_tree_quiescent


def test_operation_status_api_has_bounded_receipt_and_no_command_output(service, monkeypatch, client):
    _fake_launch(service, monkeypatch)
    operation_id = uuid.uuid4().hex
    launched = client.post(f"{BASE_URL}/api/v1/shell/exec", json={
        "id": "api-shell", "command": "private command text", "exec_dir": "/tmp", "operation_id": operation_id,
    })
    assert launched.status_code == 200
    response = client.post(f"{BASE_URL}/api/v1/shell/operation-status", json={"id": "api-shell", "operation_id": operation_id})
    assert response.status_code == 200 and response.json()["success"] is True
    assert set(response.json()["data"]) == {
        "version", "operation_id", "command_digest", "server_instance_id", "state", "returncode", "process_tree_quiescent",
    }
    assert "private command text" not in response.text
    missing = client.post(f"{BASE_URL}/api/v1/shell/operation-status", json={"id": "other", "operation_id": operation_id})
    assert missing.json()["success"] is False and missing.json()["data"] is None


def test_tracked_api_requests_reject_replaced_shell_for_read_and_mutation(service, monkeypatch, client):
    _fake_launch(service, monkeypatch)
    operation_id = uuid.uuid4().hex
    response = client.post(f"{BASE_URL}/api/v1/shell/exec", json={
        "id": "api-shell", "command": "one", "exec_dir": "/tmp", "operation_id": operation_id,
    })
    assert response.json()["success"] is True
    service.active_shells["api-shell"] = {"process": Process(0), "operation_id": uuid.uuid4().hex}
    for endpoint in ("view", "wait", "kill", "release"):
        response = client.post(f"{BASE_URL}/api/v1/shell/{endpoint}", json={
            "id": "api-shell", "operation_id": operation_id,
        })
        assert response.status_code == 400
        assert response.json()["success"] is False
