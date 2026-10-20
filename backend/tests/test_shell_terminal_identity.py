"""Non-Python revision feedback must come from terminal execution receipts."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.services.analysis_protocol_recovery import (
    confirmed_shell_success, confirmed_shell_terminal_identity,
)
from app.domain.services.execution_evidence import (
    ShellExecutionAttempt, ToolExecutionLedger, shell_command_digest,
)
from app.domain.services.tools.base import Tool
from app.domain.services.tools.shell import ShellToolkit


@pytest.fixture(autouse=True)
def synthetic_identity_key(monkeypatch):
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: SimpleNamespace(
        execution_snapshot_identity_key="synthetic-shell-terminal-key"))


def setup(*, returncode=0, state="exited", quiescent=True, operation="1" * 32):
    toolkit = ShellToolkit(SimpleNamespace(id="sandbox"))
    call = {"id": "model-call", "name": "shell_run", "args": {
        "id": "runtime", "exec_dir": "/home/ubuntu/output", "command": "Rscript analysis.R",
    }}
    ledger = ToolExecutionLedger()
    attempt = ShellExecutionAttempt(call["id"], operation, "sandbox", "runtime",
        shell_command_digest(call["args"]["exec_dir"], call["args"]["command"]), AsyncMock())
    ledger.register(attempt)
    assert attempt.observe({"version": 1, "operation_id": operation,
        "command_digest": attempt.command_digest, "server_instance_id": "a" * 32,
        "state": state, "returncode": returncode, "process_tree_quiescent": quiescent})
    return toolkit.get_tool("shell_run"), call, ledger, attempt


@pytest.mark.parametrize("returncode", [0, 1, 2, 127, -9])
def test_actual_success_or_failure_supplies_terminal_identity_not_success_claim(returncode):
    tool, call, ledger, _ = setup(returncode=returncode)
    identity = confirmed_shell_terminal_identity(tool, call, ledger)
    assert isinstance(identity, str) and identity
    assert confirmed_shell_terminal_identity(tool, call, ledger) == identity
    assert confirmed_shell_success(tool, call, ledger) == (returncode == 0)


@pytest.mark.parametrize("state,quiescent,returncode", [
    ("starting", False, None), ("running", False, None), ("unknown", False, None),
    ("not_started", True, None), ("exited", False, 0), ("exited", False, 1),
])
def test_unstarted_unresolved_or_live_process_cannot_open_revision(state, quiescent, returncode):
    tool, call, ledger, _ = setup(returncode=returncode, state=state, quiescent=quiescent)
    assert confirmed_shell_terminal_identity(tool, call, ledger) is None
    assert not confirmed_shell_success(tool, call, ledger)


@pytest.mark.parametrize("field,value", [
    ("command", "Rscript other.R"), ("exec_dir", "/different"), ("id", "other-runtime"),
])
def test_changed_command_directory_or_runtime_cannot_reuse_receipt(field, value):
    tool, call, ledger, _ = setup(returncode=1)
    call["args"][field] = value
    assert confirmed_shell_terminal_identity(tool, call, ledger) is None


def test_identity_is_operation_bound_not_model_call_id_bound():
    tool, call, ledger, attempt = setup(returncode=1)
    original = confirmed_shell_terminal_identity(tool, call, ledger)
    changed = deepcopy(call)
    changed["id"] = "new-model-label"
    assert confirmed_shell_terminal_identity(tool, changed, ledger) is None
    # Relabeling the host association does not manufacture a fresh observation.
    attempt.tool_call_id = changed["id"]
    assert confirmed_shell_terminal_identity(tool, changed, ledger) == original
    other_tool, other_call, other_ledger, _ = setup(returncode=1, operation="2" * 32)
    assert confirmed_shell_terminal_identity(other_tool, other_call, other_ledger) != original


def test_wrong_sandbox_or_latest_unresolved_operation_cannot_fall_back_to_old_receipt():
    tool, call, ledger, attempt = setup(returncode=1)
    attempt.sandbox_id = "wrong-sandbox"
    assert confirmed_shell_terminal_identity(tool, call, ledger) is None
    attempt.sandbox_id = "sandbox"
    ledger.register(ShellExecutionAttempt(call["id"], "2" * 32, "sandbox", "runtime",
        attempt.command_digest, AsyncMock()))
    assert confirmed_shell_terminal_identity(tool, call, ledger) is None


def test_private_receipt_identity_is_rechecked_not_just_exit_state():
    tool, call, ledger, attempt = setup(returncode=1)
    attempt.receipt = attempt.receipt.model_copy(update={"operation_id": "f" * 32})
    assert confirmed_shell_terminal_identity(tool, call, ledger) is None


def test_public_prose_namesake_tools_and_fake_ledgers_are_not_evidence():
    tool, call, ledger, _ = setup(returncode=1)
    empty = ToolExecutionLedger()
    assert confirmed_shell_terminal_identity(tool, call, empty) is None
    assert confirmed_shell_terminal_identity(tool, call, SimpleNamespace(_attempts=ledger._attempts)) is None
    unregistered = Tool(ShellToolkit.shell_run, toolkit=tool.toolkit)
    assert confirmed_shell_terminal_identity(unregistered, call, ledger) is None
    assert confirmed_shell_terminal_identity(tool.toolkit.get_tool("program_run"), call, ledger) is None
    call["name"] = "plugin_run"
    assert confirmed_shell_terminal_identity(tool, call, ledger) is None
