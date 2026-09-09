from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.plan import Step, Plan, ExecutionStatus
from app.domain.models.session import SessionStatus
from app.domain.services.agents.planner import PlannerAgent


@pytest.mark.asyncio
async def test_planner_cannot_self_attest_new_work_completed():
    agent = PlannerAgent.__new__(PlannerAgent)
    completed = Step(id="trusted", status=ExecutionStatus.COMPLETED, success=True)
    invented = Step(id="new", status=ExecutionStatus.COMPLETED, success=True,
                    result="already done", attachments=["/home/ubuntu/output/nonexistent.png"])
    proposed = agent._sanitize_updated_steps([completed, invented], Plan(steps=[completed]))
    assert proposed == [invented]
    assert invented.status == ExecutionStatus.PENDING and not invented.success
    assert invented.attachments == [] and invented.result is None
    assert completed.success is True


@pytest.mark.asyncio
async def test_shared_history_does_not_expose_continuation_commands_or_mutate_private_events():
    from app.interfaces.api.session_routes import get_shared_session
    token = "c" * 32
    user = MessageEvent(role="user", message="continue", metadata={"resume_from": token})
    assistant = MessageEvent(message="partial", metadata={"analysis_outcome": {
        "status": "partial", "reason_code": "artifacts_missing", "missing": [],
        "can_resume": True, "resume_from": token,
    }})
    service = SimpleNamespace(
        get_shared_session=AsyncMock(return_value=SimpleNamespace(
            id="shared-session", title="shared", status=SessionStatus.COMPLETED, is_shared=True)),
        get_session_events=AsyncMock(return_value=[user, assistant]),
    )
    response = await get_shared_session("shared-session", service)
    assert token not in response.model_dump_json()
    assert response.data.events[-1].data.metadata["analysis_outcome"]["can_resume"] is False
    assert user.metadata["resume_from"] == token
    assert assistant.metadata["analysis_outcome"]["can_resume"] is True


@pytest.mark.parametrize("change", [
    {"artifact_name": "/Users/private/file.csv"}, {"artifact_name": "C:\\private\\file.csv"},
    {"artifact_name": "../file.csv"}, {"artifact_name": "."}, {"artifact_name": ".."},
    {"artifact_name": "bad\nname.csv"}, {"artifact_name": "bad\u202ename.csv"},
    {"artifact_name": "x" * 161}, {"kind": "private/path"}, {"reason_code": "parser_secret"},
    {"blocking": "false"}, {"diagnostics": {"path": "/Users/private"}},
])
def test_public_artifact_issue_is_a_strict_bounded_allowlist(change):
    from pydantic import ValidationError
    from app.domain.models.analysis_outcome import ArtifactIssue
    with pytest.raises(ValidationError):
        ArtifactIssue.model_validate({"artifact_name": "result.csv", "kind": "table",
            "reason_code": "inconsistent_table_width", "blocking": False, **change})


def test_public_issue_serialization_cannot_leak_mutated_private_fields():
    from app.domain.models.analysis_outcome import AnalysisOutcome, ArtifactIssue
    issue = ArtifactIssue(artifact_name="result.csv", kind="table", reason_code="invalid_content", blocking=False)
    copied = issue.model_copy(update={"artifact_name": "/Users/private/file.csv", "kind": "/private/kind",
                                     "reason_code": "/private/reason"})
    payload = copied.model_dump_json()
    assert "private" not in payload and "/Users" not in payload
    outcome = AnalysisOutcome(status="succeeded", reason_code="completed", issues=[issue])
    assert outcome.issues[0].blocking is False
    assert set(outcome.model_dump()["issues"][0]) == {"artifact_name", "kind", "reason_code", "blocking"}
    unsafe = outcome.model_copy(update={"issues": [{"artifact_name": "/Users/private/result.csv",
        "kind": "/private/kind", "reason_code": "/private/reason", "blocking": "yes",
        "path": "/Users/private/data", "diagnostics": {"secret": "value"}}]})
    public = unsafe.model_dump_json()
    assert "private" not in public and "/Users" not in public and "secret" not in public
    assert unsafe.model_dump()["issues"] == [{"artifact_name": "result.csv", "kind": "any",
        "reason_code": "invalid_content", "blocking": False}]
