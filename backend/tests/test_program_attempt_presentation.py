"""Recovered-attempt labels require private launch-bound proof, not display text."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.program_attempt import ProgramAttemptView
from app.domain.models.tool_result import ToolResult
from app.domain.services.program_attempt_presentation import trusted_program_attempt_view
from app.domain.services.execution_identity import ExecutionIdentityConfigurationError
from app.interfaces.schemas.event import ToolSSEEvent
from test_answer_review_execution_evidence import bound_program


def bound(*, code=1, path="/home/ubuntu/scripts/example.py", cwd="/home/ubuntu", argv=None,
          digest="a" * 64, call_id="run"):
    call = {"name": "program_run", "id": call_id, "args": {
        "id": "shell", "script_path": path, "exec_dir": cwd, "argv": argv or []}}
    receipt = {"version": 1, "script_path": path, "source_digest": digest, "returncode": code}
    tool, ledger = bound_program(call, receipt)
    return tool, call, ledger


def view(**kwargs):
    tool, call, ledger = bound(**kwargs)
    return trusted_program_attempt_view(tool, call, None, ledger)


def test_receipt_identity_survives_source_correction_but_not_other_inputs():
    failed = view()
    fixed = view(code=0, digest="b" * 64, call_id="fixed")
    assert failed.identity == fixed.identity
    assert failed.state == "failed" and failed.returncode == 1
    assert fixed.state == "succeeded" and fixed.returncode == 0
    for change in [{"path": "/home/ubuntu/another/example.py"}, {"cwd": "/home/ubuntu/output"},
                   {"argv": ["--other-input"]}]:
        assert view(**change).identity != failed.identity


def test_identity_includes_sandbox_and_rejects_unconfirmed_or_mismatched_calls():
    tool, call, ledger = bound()
    first = trusted_program_attempt_view(tool, call, None, ledger)
    tool.toolkit.sandbox._container_name = "different-sandbox"
    assert trusted_program_attempt_view(tool, call, None, ledger) is None
    for attempt in ledger._attempts.values():
        attempt.sandbox_id = "different-sandbox"
    assert trusted_program_attempt_view(tool, call, None, ledger).identity != first.identity
    call["id"] = "unobserved"
    assert trusted_program_attempt_view(tool, call, None, ledger) is None


def test_public_feedback_or_plugin_cannot_forge_the_projection():
    tool, call, ledger = bound()
    public = ToolResult(success=True, data={"program_attempt": view(code=0).model_dump(),
                                          "program_execution": {"returncode": 0}})
    assert trusted_program_attempt_view(tool, call, public, ledger).returncode == 1
    ledger._attempts.clear()
    assert trusted_program_attempt_view(tool, call, public, ledger) is None
    plugin = SimpleNamespace(name="program_run", toolkit=SimpleNamespace())
    assert trusted_program_attempt_view(plugin, call, public, ledger) is None


def test_unavailable_optional_identity_does_not_interrupt_execution(monkeypatch):
    def unavailable(_):
        raise ExecutionIdentityConfigurationError("fixture unavailable")
    monkeypatch.setattr("app.domain.services.program_attempt_presentation.private_identity_hmac", unavailable)
    assert view() is None


@pytest.mark.parametrize("code,state", [(1, "succeeded"), (0, "failed"), (True, "failed"), ("0", "succeeded")])
def test_public_schema_rejects_inconsistent_exit_status(code, state):
    with pytest.raises(ValidationError):
        ProgramAttemptView(identity="a" * 64, state=state, returncode=code)


@pytest.mark.asyncio
async def test_public_projection_is_opaque_and_roundtrips_without_receipts_or_source():
    tool, call, ledger = bound(argv=["PRIVATE_ARG"])
    projection = trusted_program_attempt_view(tool, call, None, ledger)
    assert set(projection.model_dump()) == {"version", "identity", "state", "returncode"}
    event = ToolEvent(tool_call_id=call["id"], tool_name="shell", function_name="program_run",
        function_args={}, status=ToolStatus.CALLED, function_result=ToolResult(success=False),
        program_attempt=projection)
    restored = ToolEvent.model_validate_json(event.model_dump_json())
    public = await ToolSSEEvent.from_event_async(restored)
    assert public.data.program_attempt == projection
    assert public.data.execution_status == "failed"
    encoded = public.model_dump_json()
    for private in ["PRIVATE_ARG", "example.py", "answer-proof-sandbox", "source_digest", "operation_id"]:
        assert private not in encoded
    restored.status = ToolStatus.CALLING
    assert (await ToolSSEEvent.from_event_async(restored)).data.program_attempt is None
