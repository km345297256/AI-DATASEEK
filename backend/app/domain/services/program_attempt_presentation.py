"""Publish an opaque retry identity, never commands, receipts or private source.

This is display-only. It grants no replay permission and makes no statement
about scientific correctness or deliverable completeness.
"""
from app.domain.models.program_attempt import ProgramAttemptView
from app.domain.services.execution_identity import ExecutionIdentityConfigurationError, private_identity_hmac
from app.domain.services.program_execution import trusted_program_execution_feedback


def trusted_program_attempt_view(tool, call, result, ledger) -> ProgramAttemptView | None:
    feedback = trusted_program_execution_feedback(tool, call, result, ledger)
    if feedback is None or type(feedback.get("returncode")) is not int:
        return None
    args = call.get("args") or {}
    # Arguments and cwd distinguish different uses of the same script. Exclude
    # source bytes/digest so a corrected version at that exact target can recover
    # an earlier failure. The UI additionally restricts matching to one step.
    try:
        identity = private_identity_hmac({
            "purpose": "program-attempt-presentation/v1",
            "sandbox_id": str(tool.toolkit.sandbox.id),
            "script_path": feedback["script_path"],
            "exec_dir": args.get("exec_dir"),
            "argv": args.get("argv") or [],
        })
    except (ExecutionIdentityConfigurationError, TypeError, ValueError):
        # Display metadata must not convert a completed execution into a
        # failure. Missing identity leaves its original status visible.
        return None
    return ProgramAttemptView(identity=identity,
        state="succeeded" if feedback["returncode"] == 0 else "failed",
        returncode=feedback["returncode"])
