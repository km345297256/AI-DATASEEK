"""Trusted operation evidence, independent of tool payloads and replay policy."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.execution_evidence import (
    ShellExecutionAttempt,
    ToolExecutionLedger,
    consume_shell_receipt,
    current_shell_attempt,
    register_shell_attempt,
    refresh_shell_attempt,
    shell_command_digest,
    tool_execution_scope,
)
from app.domain.services.tools.shell import ShellToolkit
from app.domain.services.tools.tool_contract import resolved_tool_can_observe_pending
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def attempt(call_id="call", *, shell_id="shell", query=None):
    return ShellExecutionAttempt(
        tool_call_id=call_id, operation_id=uuid4().hex,
        sandbox_id="sandbox", shell_id=shell_id,
        command_digest=shell_command_digest("/tmp", "some operation"),
        query=query or AsyncMock(return_value=ToolResult(success=False)),
    )


def receipt(operation, **changes):
    return dict({
        "version": 1, "operation_id": operation.operation_id,
        "command_digest": operation.command_digest,
        "server_instance_id": "a" * 32,
        "state": "exited", "returncode": 0, "process_tree_quiescent": True,
    }, **changes)


@pytest.mark.parametrize("returncode", [0, 2, -15])
def test_confirmed_exit_is_not_replay_permission(returncode):
    ledger = ToolExecutionLedger()
    operation = attempt()
    ledger.register(operation)
    assert operation.observe(receipt(operation, returncode=returncode))
    assert ledger.failure_state("call") == "confirmed_terminal"
    assert ledger.summary()["pending_execution"] is False
    assert ledger.summary()["replay_safe"] is False
    assert ledger.summary()["confirmed_failed_operation_count"] == int(returncode != 0)


def test_only_proven_not_started_is_replay_safe():
    ledger = ToolExecutionLedger()
    operation = attempt()
    ledger.register(operation)
    assert operation.observe(receipt(operation, state="not_started", returncode=None))
    assert ledger.failure_state("call") == "not_started"
    assert ledger.summary()["replay_safe"] is True


@pytest.mark.parametrize("changes", [
    {"operation_id": "b" * 32}, {"command_digest": "b" * 64},
    {"version": 2}, {"version": True}, {"returncode": False},
    {"returncode": "0"}, {"returncode": None}, {"process_tree_quiescent": "true"},
    {"state": "running"}, {"state": "not_started"}, {"state": "unknown"}, {"replay_safe": True},
])
def test_invalid_or_wrong_operation_evidence_cannot_clear_pending(changes):
    ledger = ToolExecutionLedger()
    operation = attempt()
    ledger.register(operation)
    assert operation.observe(receipt(operation, **changes)) is False
    assert ledger.failure_state("call") == "unknown"


def test_leader_exit_does_not_prove_children_quiescent():
    operation = attempt()
    assert operation.observe(receipt(operation, process_tree_quiescent=False))
    assert operation.confirmed is False
    assert operation.observe(receipt(operation))
    assert operation.confirmed is True
    assert operation.observe(receipt(operation, returncode=2)) is False
    assert operation.observe(receipt(operation, state="running", returncode=None,
                                     process_tree_quiescent=False)) is False
    assert operation.receipt.returncode == 0


def test_server_instance_change_cannot_resolve_old_execution():
    operation = attempt()
    assert operation.observe(receipt(operation, state="running", returncode=None,
                                     process_tree_quiescent=False))
    assert operation.observe(receipt(operation, server_instance_id="b" * 32)) is False
    assert operation.confirmed is False


def test_opaque_unknown_and_all_attempts_are_independent():
    ledger = ToolExecutionLedger()
    first, second = attempt(), attempt()
    ledger.register(first)
    ledger.register(second)
    ledger.record_unknown("opaque-plugin")
    assert first.observe(receipt(first))
    assert ledger.failure_state("call") == "unknown"
    assert second.observe(receipt(second))
    assert ledger.failure_state("call") == "confirmed_terminal"
    assert ledger.failure_state("opaque-plugin", fallback="idempotent") == "unknown"
    assert ledger.summary()["unresolved_call_count"] == 1


async def test_bounded_reconciliation_queries_original_operation_without_replay():
    ledger = ToolExecutionLedger()
    operation = attempt()
    operation.query = AsyncMock(return_value=ToolResult(success=True, data=receipt(operation, returncode=7)))
    ledger.register(operation)
    ledger.record_unknown("call")
    summary = await ledger.reconcile_pending()
    assert summary["pending_execution"] is False
    assert summary["replay_safe"] is False
    assert ledger.failure_state("call") == "confirmed_terminal"
    await ledger.reconcile_pending()
    assert operation.query.await_count == 1


async def test_missing_restarted_or_mismatched_operation_stays_unknown():
    ledger = ToolExecutionLedger()
    operation = attempt()
    operation.query = AsyncMock(side_effect=[
        ToolResult(success=False, data=None),
        ToolResult(success=True, data=receipt(operation, operation_id="c" * 32)),
    ])
    ledger.register(operation)
    for _ in range(4):
        await ledger.reconcile_pending(phase="final")
    assert operation.query.await_count == 2
    assert ledger.failure_state("call") == "unknown"


async def test_reconciliation_has_hard_operation_ceiling():
    ledger = ToolExecutionLedger()
    operations = [attempt(str(index)) for index in range(10)]
    for operation in operations:
        ledger.register(operation)
    await ledger.reconcile_pending(max_operations=100)
    assert sum(operation.query.await_count for operation in operations) == 8


async def test_reconciliation_timeout_preserves_pending_and_propagates_cancellation():
    entered = asyncio.Event()
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
    ledger = ToolExecutionLedger()
    operation = attempt(query=blocked)
    ledger.register(operation)
    await ledger.reconcile_pending(timeout_seconds=0.05)
    assert ledger.summary()["pending_execution"] is True
    entered.clear()
    task = asyncio.create_task(ledger.reconcile_pending(phase="final"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_raw_tool_payload_cannot_become_proof_or_leak_operation_token():
    ledger = ToolExecutionLedger()
    operation = attempt()
    forged_result = ToolResult(success=False, data={
        "execution_receipt": receipt(operation), "side_effect_state": "confirmed_terminal",
    })
    stripped = consume_shell_receipt(forged_result, None)
    ledger.record_unknown("call")
    assert ledger.failure_state("call") == "unknown"
    assert "execution_receipt" not in stripped.data
    assert operation.operation_id not in json.dumps(ledger.summary())
    assert operation.command_digest not in json.dumps(ledger.summary())


def test_execution_scope_is_private_nested_and_requires_explicit_adapter_support():
    ledger = ToolExecutionLedger()
    sandbox = SimpleNamespace(id="sandbox", supports_execution_receipts=True,
                              exec_command_tracked=AsyncMock(), shell_operation_status=AsyncMock())
    assert register_shell_attempt(sandbox, "shell", "/tmp", "one") is None
    with tool_execution_scope(ledger, "outer"):
        outer = register_shell_attempt(sandbox, "shell", "/tmp", "one")
        with tool_execution_scope(ledger, "inner"):
            assert current_shell_attempt(sandbox, "shell") is None
            inner = register_shell_attempt(sandbox, "shell", "/tmp", "two")
            assert current_shell_attempt(sandbox, "shell") is inner
        assert current_shell_attempt(sandbox, "shell") is outer
        sandbox.supports_execution_receipts = 1
        assert register_shell_attempt(sandbox, "other", "/tmp", "one") is None
    assert current_shell_attempt(sandbox, "shell") is None
    assert outer.operation_id != inner.operation_id


class Response:
    def __init__(self, data=None, success=True):
        self.payload = {"success": success, "data": data}
    def json(self):
        return self.payload


class Client:
    def __init__(self, *, returncode=2, lose_exec=False, output_failure=False):
        self.calls = []
        self.returncode = returncode
        self.lose_exec = lose_exec
        self.output_failure = output_failure
        self.original_request = None

    def proof(self, terminal=True):
        original = self.original_request
        return {"version": 1, "operation_id": original["operation_id"],
                "command_digest": shell_command_digest(original["exec_dir"], original["command"]),
                "server_instance_id": "a" * 32, "state": "exited" if terminal else "running",
                "returncode": self.returncode if terminal else None,
                "process_tree_quiescent": terminal}

    async def post(self, url, *, json, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, json))
        if endpoint == "exec":
            self.original_request = json
            if self.lose_exec:
                raise OSError("transport unavailable")
            return Response({"status": "running", "returncode": None,
                             "execution_receipt": self.proof(False)})
        if endpoint == "operation-status":
            assert json["operation_id"] == self.original_request["operation_id"]
            assert json["id"] == self.original_request["id"]
            return Response(self.proof())
        if endpoint == "wait":
            assert json["operation_id"] == self.original_request["operation_id"]
            return Response({"status": "completed", "returncode": self.returncode})
        if endpoint == "view":
            assert json["operation_id"] == self.original_request["operation_id"]
            if self.output_failure:
                raise OSError("output unavailable")
            return Response({"output": "some output"})
        if endpoint in {"kill", "release"}:
            assert json["operation_id"] == self.original_request["operation_id"]
            return Response({"status": "terminated", "returncode": self.returncode})
        raise AssertionError(endpoint)


def sandbox(client):
    value = DockerSandbox.__new__(DockerSandbox)
    value._container_name = "sandbox"
    value.base_url = "http://sandbox.invalid"
    value.client = client
    return value


async def test_adapter_registers_before_lost_exec_response_and_queries_without_reexec():
    ledger, client = ToolExecutionLedger(), Client(lose_exec=True)
    with tool_execution_scope(ledger, "call"):
        with pytest.raises(OSError):
            await sandbox(client).exec_command("same-shell", "/tmp", "some operation")
    assert ledger.summary()["pending_execution"] is True
    await ledger.reconcile_pending()
    assert ledger.failure_state("call") == "confirmed_terminal"
    assert [entry[0] for entry in client.calls] == ["exec", "operation-status"]


@pytest.mark.parametrize("credentialed", [False, True])
async def test_adapter_tracks_real_plugin_or_shell_wait_and_strips_receipts(credentialed):
    ledger, client = ToolExecutionLedger(), Client()
    adapter = sandbox(client)
    with tool_execution_scope(ledger, "call"):
        if credentialed:
            result = await adapter.exec_command_with_credentials("shell", "/tmp", "some operation",
                                                                  {"token": "secret-not-retained"})
        else:
            result = await adapter.exec_command("shell", "/tmp", "some operation")
        assert "execution_receipt" not in result.data
        await adapter.wait_for_process("shell", 3)
        await adapter.view_shell("shell")
    assert ledger.failure_state("call") == "confirmed_terminal"
    assert ledger.summary()["replay_safe"] is False
    assert "secret-not-retained" not in repr(ledger._attempts)
    assert [entry[0] for entry in client.calls] == ["exec", "wait", "operation-status", "view"]


async def test_zero_exit_output_transport_failure_is_not_command_execution_failure():
    ledger, client = ToolExecutionLedger(), Client(returncode=0, output_failure=True)
    toolkit = ShellToolkit(sandbox(client))
    with tool_execution_scope(ledger, "call"):
        result = await toolkit._run_bounded_command(id="shell", exec_dir="/tmp",
                                                    command="some operation", timeout_seconds=5)
    assert result.success is False
    assert result.data["returncode"] == 0
    assert result.data["error_code"] == "shell_output_unavailable"
    assert "failed with return code: 0" not in result.message
    assert ledger.failure_state("call") == "confirmed_terminal"
    assert ledger.summary()["replay_safe"] is False
    assert sum(endpoint == "exec" for endpoint, _ in client.calls) == 1


async def test_later_wait_call_is_bound_to_original_attempt_without_opaque_unknown():
    ledger, client = ToolExecutionLedger(), Client(returncode=9)
    adapter = sandbox(client)
    with tool_execution_scope(ledger, "exec-call"):
        await adapter.exec_command("shell", "/tmp", "some operation")
    with tool_execution_scope(ledger, "wait-call"):
        result = await adapter.wait_for_process("shell", 1)
        assert result.success is False
        assert ledger.failure_state("wait-call") == "confirmed_terminal"
        ledger.record_unknown("wait-call")
    assert ledger.summary()["pending_execution"] is False
    assert ledger.summary()["tracked_operation_count"] == 1
    assert ledger.summary()["replay_safe"] is False
    assert ledger.failure_state("exec-call") == "confirmed_terminal"
    assert sum(endpoint == "exec" for endpoint, _ in client.calls) == 1


@pytest.mark.parametrize("endpoint", ["kill", "release"])
async def test_cleanup_is_guarded_by_original_generation_and_not_itself_terminal_proof(endpoint):
    ledger, client = ToolExecutionLedger(), Client(returncode=-15)
    adapter = sandbox(client)
    with tool_execution_scope(ledger, "exec-call"):
        await adapter.exec_command("shell", "/tmp", "some operation")
    with tool_execution_scope(ledger, "cleanup-call"):
        method = adapter.kill_process if endpoint == "kill" else adapter.release_shell
        await method("shell")
    assert ledger.failure_state("cleanup-call") == "unknown"
    assert ledger.summary()["pending_execution"] is True
    await ledger.reconcile_pending(phase="final")
    assert ledger.failure_state("cleanup-call") == "confirmed_terminal"
    assert ledger.summary()["replay_safe"] is False


async def test_successful_evidence_after_one_query_failure_recovers_without_a_final_allowance():
    ledger = ToolExecutionLedger()
    operation = attempt()
    operation.query = AsyncMock(side_effect=[ToolResult(success=False),
        ToolResult(success=True, data=receipt(operation, returncode=7))])
    ledger.register(operation)
    for _ in range(5):
        await ledger.reconcile_pending()
    assert operation.query.await_count == 2
    assert ledger.summary()["pending_execution"] is False
    await ledger.reconcile_pending(phase="final")
    assert operation.query.await_count == 2
    assert ledger.failure_state("call") == "confirmed_terminal"


async def test_fresh_running_receipt_does_not_consume_early_query_allowance():
    ledger = ToolExecutionLedger()
    operation = attempt()
    ledger.register(operation)
    operation.observe(receipt(operation, state="running", returncode=None,
                              process_tree_quiescent=False))
    await ledger.reconcile_pending()
    assert operation.query.await_count == 0
    await ledger.reconcile_pending(phase="final")
    assert operation.query.await_count == 1


@pytest.mark.parametrize("name", ["shell_wait", "shell_view"])
def test_pending_observer_requires_original_core_declaration_adapter_and_registered_attempt(name):
    ledger = ToolExecutionLedger()
    adapter = sandbox(Client())
    toolkit = ShellToolkit(adapter)
    tool = toolkit.get_tool(name)
    call = {"name": name, "id": "observer", "args": {"id": "shell"}}
    assert resolved_tool_can_observe_pending(tool, call, ledger) is False
    with tool_execution_scope(ledger, "exec"):
        register_shell_attempt(adapter, "shell", "/tmp", "some operation")
    assert resolved_tool_can_observe_pending(tool, call, ledger) is True
    assert resolved_tool_can_observe_pending(tool, {**call, "args": {"id": "other"}}, ledger) is False
    forged = SimpleNamespace(name=name, toolkit=toolkit, _tool=tool._tool,
                             execution_contract={"effects": ["sandbox_read"]})
    assert resolved_tool_can_observe_pending(forged, call, ledger) is False
    original = tool._tool
    tool._tool = toolkit.get_tool("shell_run")._tool
    assert resolved_tool_can_observe_pending(tool, call, ledger) is False
    tool._tool = original
    adapter.supports_execution_receipts = False
    assert resolved_tool_can_observe_pending(tool, call, ledger) is False


def test_pending_observer_never_authorizes_new_exec_or_process_input():
    ledger, adapter = ToolExecutionLedger(), sandbox(Client())
    toolkit = ShellToolkit(adapter)
    with tool_execution_scope(ledger, "exec"):
        register_shell_attempt(adapter, "shell", "/tmp", "some operation")
    for name in ["shell_run", "shell_exec", "shell_write_to_process"]:
        assert resolved_tool_can_observe_pending(toolkit.get_tool(name),
            {"name": name, "id": "next", "args": {"id": "shell"}}, ledger) is False


async def test_successful_untracked_write_blocks_replay_without_becoming_pending():
    ledger = ToolExecutionLedger()
    private_call_id = "private-external-operation-token"
    ledger.record_nonreplayable(private_call_id)
    ledger.record_nonreplayable(private_call_id)
    expected = {
        "pending_execution": False, "execution_confirmed": True, "replay_safe": False,
        "tracked_operation_count": 0, "confirmed_failed_operation_count": 0,
        "unresolved_call_count": 0, "nonreplayable_call_count": 1,
        "has_observable_pending": False, "has_unresolvable_pending": False, "reason": "none",
    }
    assert ledger.summary() == expected
    assert ledger.call_summary(private_call_id) == expected
    assert ledger.call_summary("unrelated-read")["replay_safe"] is True
    assert private_call_id not in json.dumps(expected)
    assert await ledger.reconcile_pending(phase="final") == {
        **expected, "attempted_count": 0, "changed_count": 0,
    }


def test_not_started_receipt_and_later_reads_do_not_erase_completed_external_write():
    ledger = ToolExecutionLedger()
    operation = attempt("safe-not-started")
    ledger.register(operation)
    operation.observe(receipt(operation, state="not_started", returncode=None))
    assert ledger.summary()["replay_safe"] is True
    ledger.record_nonreplayable("completed-external-write")
    assert ledger.failure_state("safe-not-started") == "not_started"
    assert ledger.call_summary("safe-not-started")["replay_safe"] is True
    assert ledger.call_summary("later-safe-read")["replay_safe"] is True
    assert ledger.summary()["replay_safe"] is False
    assert ledger.summary()["pending_execution"] is False


def test_nonreplayable_success_does_not_clear_an_independent_unknown_call():
    ledger = ToolExecutionLedger()
    ledger.record_unknown("unresolved-external-operation")
    ledger.record_nonreplayable("completed-external-write")
    assert ledger.summary()["replay_safe"] is False
    assert ledger.summary()["pending_execution"] is True
    assert ledger.summary()["unresolved_call_count"] == 1
    assert ledger.call_summary("completed-external-write")["pending_execution"] is False


async def test_healthy_running_observation_has_no_cumulative_query_limit():
    ledger, operation = ToolExecutionLedger(), attempt()
    running = receipt(operation, state="running", returncode=None, process_tree_quiescent=False)
    operation.query = AsyncMock(return_value=ToolResult(success=True, data=running))
    ledger.register(operation)
    for index in range(12):
        report = await ledger.reconcile_pending(phase="final")
        assert report["attempted_count"] == 1
        assert report["changed_count"] == int(index == 0)
        assert report["has_observable_pending"] is True
        assert report["has_unresolvable_pending"] is False
        assert operation.consecutive_query_failures == 0
    assert operation.query.await_count == 12
    operation.query.return_value = ToolResult(success=True, data=receipt(operation))
    report = await ledger.reconcile_pending(phase="final")
    assert report["changed_count"] == 1
    assert report["pending_execution"] is False
    assert report["replay_safe"] is False


@pytest.mark.parametrize("state,returncode", [("starting", None), ("exited", 0)])
async def test_original_starting_and_live_descendant_states_remain_observable(state, returncode):
    ledger, operation = ToolExecutionLedger(), attempt()
    operation.query = AsyncMock(return_value=ToolResult(success=True, data=receipt(
        operation, state=state, returncode=returncode, process_tree_quiescent=False)))
    ledger.register(operation)
    for _ in range(5):
        report = await ledger.reconcile_pending(phase="final")
        assert report["has_observable_pending"] is True
        assert report["has_unresolvable_pending"] is False
        assert report["replay_safe"] is False
    assert operation.query.await_count == 5


async def test_continuous_query_failures_stop_retries_but_healthy_receipt_resets_streak():
    ledger, operation = ToolExecutionLedger(), attempt()
    ledger.register(operation)
    running = receipt(operation, state="running", returncode=None, process_tree_quiescent=False)
    operation.query = AsyncMock(side_effect=[
        OSError("temporary failure"), ToolResult(success=True, data=running),
        ToolResult(success=False), ToolResult(success=False),
    ])
    for expected_failures in [1, 0, 1, 2]:
        report = await ledger.reconcile_pending(phase="final")
        assert report["attempted_count"] == 1
        assert operation.consecutive_query_failures == expected_failures
    assert report["has_observable_pending"] is False
    assert report["has_unresolvable_pending"] is True
    assert report["reason"] == "execution_status_query_failed"
    for _ in range(5):
        report = await ledger.reconcile_pending(phase="final")
        assert report["attempted_count"] == 0
    assert operation.query.await_count == 4


@pytest.mark.parametrize("kind,reason", [
    ("opaque", "opaque_execution_unknown"),
    ("unknown", "execution_state_unknown"),
    ("identity", "execution_identity_lost"),
    ("malformed", "execution_receipt_invalid"),
])
async def test_unresolvable_evidence_is_explicit_without_endless_observation(kind, reason):
    ledger, operation = ToolExecutionLedger(), attempt()
    if kind == "opaque":
        ledger.record_unknown("private-untracked-token")
    else:
        ledger.register(operation)
        if kind == "unknown":
            operation.observe(receipt(operation, state="unknown", returncode=None, process_tree_quiescent=False))
        elif kind == "identity":
            operation.observe(receipt(operation, operation_id="c" * 32))
        else:
            operation.observe({"untrusted": "malformed"})
    summary = ledger.summary()
    assert summary["has_unresolvable_pending"] is True
    assert summary["has_observable_pending"] is False
    assert summary["reason"] == reason
    report = await ledger.reconcile_pending(phase="final")
    assert report["attempted_count"] == 0
    assert report["changed_count"] == 0
    operation.query.assert_not_awaited()
    assert "private-untracked-token" not in json.dumps(report)


async def test_mixed_pending_reports_unresolvable_and_observable_independently():
    ledger, operation = ToolExecutionLedger(), attempt()
    ledger.register(operation)
    ledger.record_unknown("opaque")
    summary = ledger.summary()
    assert summary["has_observable_pending"] is True
    assert summary["has_unresolvable_pending"] is True
    assert summary["reason"] == "opaque_execution_unknown"


async def test_fresh_receipt_report_does_not_claim_a_query_was_performed():
    ledger, operation = ToolExecutionLedger(), attempt()
    ledger.register(operation)
    operation.observe(receipt(operation, state="running", returncode=None, process_tree_quiescent=False))
    report = await ledger.reconcile_pending()
    assert report["attempted_count"] == 0 and report["changed_count"] == 0
    assert report["has_observable_pending"] is True
    operation.query.assert_not_awaited()


async def test_direct_wait_refresh_shares_failure_streak_and_never_consumes_healthy_lifetime_allowance():
    ledger, operation = ToolExecutionLedger(), attempt()
    ledger.register(operation)
    running = receipt(operation, state="running", returncode=None, process_tree_quiescent=False)
    operation.query = AsyncMock(return_value=ToolResult(success=True, data=running))
    for _ in range(6):
        await refresh_shell_attempt(operation)
    assert operation.query.await_count == 6
    operation.query.return_value = ToolResult(success=False)
    await refresh_shell_attempt(operation)
    assert operation.consecutive_query_failures == 1
    await ledger.reconcile_pending(phase="final")
    assert operation.consecutive_query_failures == 2
    await refresh_shell_attempt(operation)
    assert operation.query.await_count == 8
    assert ledger.summary()["has_unresolvable_pending"] is True


async def test_observation_selection_is_fair_across_healthy_long_running_operations():
    ledger = ToolExecutionLedger()
    operations = [attempt(str(index)) for index in range(10)]
    for operation in operations:
        operation.query = AsyncMock(return_value=ToolResult(success=True, data=receipt(
            operation, state="running", returncode=None, process_tree_quiescent=False)))
        ledger.register(operation)
    first = await ledger.reconcile_pending(max_operations=8, phase="final")
    second = await ledger.reconcile_pending(max_operations=2, phase="final")
    assert first["attempted_count"] == 8 and second["attempted_count"] == 2
    assert all(operation.query.await_count == 1 for operation in operations)


async def test_query_ignoring_cancellation_cannot_exceed_deadline_or_apply_late_evidence():
    ledger, operation = ToolExecutionLedger(), attempt()
    release, finished = asyncio.Event(), asyncio.Event()
    async def cancellation_resistant_query():
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return ToolResult(success=True, data=receipt(operation))
        finally:
            finished.set()
    operation.query = cancellation_resistant_query
    ledger.register(operation)
    try:
        report = await asyncio.wait_for(ledger.reconcile_pending(timeout_seconds=0.05), timeout=0.3)
        assert report["attempted_count"] == 1 and report["changed_count"] == 0
        assert report["has_unresolvable_pending"] is True
        assert report["reason"] == "execution_status_query_unresponsive"
    finally:
        release.set()
        await finished.wait()
        await asyncio.sleep(0)
    assert operation.receipt is None
    assert ledger.summary()["pending_execution"] is True


def test_observation_cannot_rewrite_a_known_leader_exit_before_descendants_finish():
    ledger, operation = ToolExecutionLedger(), attempt()
    ledger.register(operation)
    operation.observe(receipt(operation, returncode=7, process_tree_quiescent=False))
    assert not operation.observe(receipt(operation, returncode=0))
    assert operation.receipt.returncode == 7
    assert ledger.summary()["has_unresolvable_pending"] is True
    assert ledger.summary()["replay_safe"] is False


def test_confirmed_history_does_not_become_a_cumulative_execution_quota():
    ledger = ToolExecutionLedger()
    operations = [attempt(str(index)) for index in range(200)]
    for operation in operations:
        ledger.register(operation)
        assert operation.observe(receipt(operation))
    assert ledger.summary()["tracked_operation_count"] == 200
    assert ledger.summary()["pending_execution"] is False
    assert ledger.summary()["replay_safe"] is False
    assert ledger._attempts[operations[0].operation_id] is operations[0]
    assert ledger.call_summary("0")["replay_safe"] is False


def test_pending_operation_capacity_remains_bounded_without_dropping_old_identity():
    from app.domain.services.tools.tool_contract import ToolContractError
    ledger = ToolExecutionLedger()
    for index in range(ledger.MAX_PENDING_OPERATIONS):
        ledger.register(attempt(str(index)))
    with pytest.raises(ToolContractError):
        ledger.register(attempt("over-capacity"))
    assert len(ledger._attempts) == ledger.MAX_PENDING_OPERATIONS
    assert ledger.summary()["pending_execution"] is True
