"""Continuation admission and dispatch with durable in-memory records only."""
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.application.services.agent_service import AgentService
from app.application.services.dataset_request_resolver import ExecutionDecision, FrontControllerResolution, RequestDecision
from app.domain.models.event import ErrorEvent, MessageEvent
from app.domain.models.safety import SafetyReview
from app.domain.services.agent_domain_service import AgentDomainService, ContinuationRejected, CONTINUATION_MESSAGE
from app.domain.services.analysis_checkpoint import configuration_digest
from app.interfaces.schemas.session import ChatRequest
from test_input_delivery import MemoryInputs, FakeTask


TOKEN = "a" * 32
TARGET = "/home/ubuntu/datasets/registered/data.csv"


class CheckpointRepository(MemoryInputs):
    def __init__(self):
        super().__init__()
        self.session.sandbox_id = "sandbox"
        self.session.dataset_ids = ["registered"]
        self.events = [MessageEvent(seq=1, role="user", message="Create the required chart and table")]
        self.checkpoint = {
            "id": TOKEN, "version": 1, "owner_id": "user", "sandbox_id": "sandbox",
            "configuration_digest": configuration_digest(self.session),
            "expires_at": datetime.now(UTC) + timedelta(hours=1), "claimed_by": None,
            "goal": self.events[0].message, "source_seq": 1,
            "dataset_ids": ["registered"], "target_files": [TARGET], "source_paths": [TARGET],
            "skills": ["original-skill"], "mcp_servers": ["original-mcp"], "mcp_access_all": False,
            "attachment_file_ids": [], "plan": {"private": "do not put in metadata"},
        }
        self.claims = []
        self.clear_count = 0

    async def get_analysis_checkpoint(self, session_id, checkpoint_id):
        if self.checkpoint and self.checkpoint["id"] == checkpoint_id:
            return copy.deepcopy(self.checkpoint)
        return None

    async def claim_analysis_checkpoint(self, session_id, checkpoint_id, user_id, client_message_id,
                                        *, expected_source_seq):
        self.claims.append(client_message_id)
        cp = self.checkpoint
        latest = max((event.seq or 0 for event in self.events if isinstance(event, MessageEvent) and event.role == "user"), default=0)
        if (not cp or cp["id"] != checkpoint_id or cp["owner_id"] != user_id
                or cp["claimed_by"] not in (None, client_message_id)
                or cp["source_seq"] != expected_source_seq or latest != expected_source_seq):
            return None
        cp["claimed_by"] = client_message_id
        return copy.deepcopy(cp)

    async def clear_analysis_checkpoint(self, session_id):
        self.clear_count += 1
        self.checkpoint = None


def resolution(mode="sandbox", targets=None):
    decision = RequestDecision(safety=SafetyReview(decision="reject" if mode == "reject" else "allow"),
        execution=ExecutionDecision(mode="sandbox" if mode == "reject" else mode,
                                    required_evidence="file_content"))
    return FrontControllerResolution(decision=decision, answer="", controller_metadata={}, target_files=targets or [])


def service_for(repository):
    task = FakeTask()
    task.enqueue_input = AsyncMock()
    task.run = AsyncMock()
    service = AgentDomainService(agent_repository=object(), session_repository=repository, sandbox_cls=object(),
        task_cls=SimpleNamespace(get=lambda _: task), file_storage=object(), mcp_repository=object(),
        sandbox_runtime=object(), input_repository=repository)
    service._get_task = AsyncMock(return_value=None)
    service._create_task = AsyncMock(return_value=task)
    service._create_lightweight_task = AsyncMock(return_value=task)
    service._dataset_service = SimpleNamespace(get_dataset=AsyncMock(return_value="dataset-summary"))
    service._dataset_request_resolver = SimpleNamespace(resolve=AsyncMock(return_value=resolution()))
    service._resolve_message_attachments = AsyncMock(return_value=[])
    service._schedule_accepted_input = AsyncMock()
    return service, task


async def submit(service, repository, *, token=TOKEN, client_id="resume-client", **kwargs):
    values = dict(session=repository.session, user_id="user", message="", timestamp=None,
                  attachments=None, skills=None, mcp_servers=None, dataset_ids=None,
                  mcp_access_all=False, client_message_id=client_id, resume_from=token)
    values.update(kwargs)
    return await service._bootstrap_durable_input(**values)


@pytest.mark.parametrize("changes", [
    {"resume_from": "x" * 32}, {"resume_from": "A" * 32}, {"resume_from": "a" * 31},
    {"client_message_id": None}, {"client_message_id": " "}, {"message": "do something else"},
    {"dataset_ids": ["other"]}, {"skills": ["other"]}, {"mcp_servers": ["other"]},
    {"agent_profile_id": "other"}, {"attachments": [{"file_id": "other"}]},
])
def test_continuation_api_refuses_scope_or_identity_overrides(changes):
    values = {"resume_from": TOKEN, "client_message_id": "resume-client", "message": "", **changes}
    with pytest.raises(ValidationError):
        ChatRequest(**values)


def test_empty_continuation_and_ordinary_sse_reconnect_are_distinct():
    assert ChatRequest(resume_from=TOKEN, client_message_id="resume-client").resume_from == TOKEN
    assert ChatRequest(event_seq=10).resume_from is None


@pytest.mark.asyncio
async def test_continuation_restores_scope_before_durable_admission_and_gates_original_goal():
    repository = CheckpointRepository()
    service, task = service_for(repository)
    assert await submit(service, repository) is task
    record = next(iter(repository.records.values()))
    assert record.event.message == CONTINUATION_MESSAGE
    assert record.event.metadata == {"resume_from": TOKEN, "client_message_id": "resume-client",
        "skills": ["original-skill"], "mcp_servers": ["original-mcp"], "mcp_access_all": False,
        "dataset_ids": ["registered"]}
    assert "private" not in record.event.model_dump_json()
    request = service._dataset_request_resolver.resolve.await_args.kwargs
    assert request["question"] == repository.checkpoint["goal"]
    assert request["selected_skills"] == ["original-skill"]
    assert request["selected_mcp_servers"] == ["original-mcp"]
    assert service._create_task.await_args.args[1] == ["registered"]
    assert service._create_task.await_args.kwargs["front_controller_resolution"].target_files == [TARGET]
    assert json.loads(task.enqueue_input.await_args.args[0])["metadata"] == record.event.metadata
    assert repository.clear_count == 0
    task.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_client_reconnect_is_idempotent_even_after_checkpoint_was_cleared():
    repository = CheckpointRepository()
    service, task = service_for(repository)
    await submit(service, repository)
    repository.checkpoint = None
    service._get_task = AsyncMock(side_effect=AssertionError("duplicate must not wait or prepare"))
    assert await submit(service, repository) is task
    assert repository.claims == ["resume-client"]
    assert len(repository.records) == 1
    task.run.assert_awaited_once()
    service._dataset_request_resolver.resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_token_with_new_client_identity_cannot_execute_again():
    repository = CheckpointRepository()
    service, task = service_for(repository)
    await submit(service, repository)
    with pytest.raises(ContinuationRejected):
        await submit(service, repository, client_id="another-client")
    assert len(repository.records) == 1
    task.run.assert_awaited_once()


@pytest.mark.parametrize("change", ["owner", "expiry", "config", "sandbox", "new_user", "attachments", "no_source"])
@pytest.mark.asyncio
async def test_invalid_checkpoint_is_rejected_before_admission_or_model_call(change):
    repository = CheckpointRepository()
    service, task = service_for(repository)
    if change == "owner":
        repository.checkpoint["owner_id"] = "another-user"
    elif change == "expiry":
        repository.checkpoint["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "config":
        repository.session.llm_overrides = {"model_name": "different"}
    elif change == "sandbox":
        repository.session.sandbox_id = "replacement"
    elif change == "new_user":
        repository.events.append(MessageEvent(seq=2, role="user", message="new goal"))
    elif change == "attachments":
        repository.checkpoint["attachment_file_ids"] = ["file-id"]
    else:
        repository.checkpoint["source_seq"] = None
    with pytest.raises(ContinuationRejected):
        await submit(service, repository)
    assert not repository.records
    task.run.assert_not_awaited()
    service._dataset_request_resolver.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_new_input_invalidates_checkpoint():
    repository = CheckpointRepository()
    service, task = service_for(repository)
    await submit(service, repository, token=None, message="new analysis", client_id="ordinary-client")
    assert repository.checkpoint is None and repository.clear_count == 1
    task.run.assert_awaited_once()


@pytest.mark.parametrize("fault", ["expired", "config", "changed_scope", "source_unavailable", "direct_route", "new_input"])
@pytest.mark.asyncio
async def test_changed_continuation_is_terminal_not_preparation_retry(fault):
    repository = CheckpointRepository()
    service, task = service_for(repository)
    real_dispatch = service._dispatch_claimed_input
    service._dispatch_claimed_input = AsyncMock(return_value=None)
    await submit(service, repository)
    record = next(iter(repository.records.values()))
    service._dispatch_claimed_input = real_dispatch
    service._input_delivery.retry_preparation = AsyncMock()
    if fault == "expired":
        repository.checkpoint["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    elif fault == "config":
        repository.session.llm_overrides = {"model_name": "other"}
    elif fault == "changed_scope":
        service._dataset_request_resolver.resolve.return_value = resolution(targets=["/home/ubuntu/datasets/registered/other.csv"])
    elif fault == "source_unavailable":
        service._dataset_service.get_dataset.side_effect = ValueError("private source no longer available")
    elif fault == "direct_route":
        service._dataset_request_resolver.resolve.return_value = resolution(mode="direct")
    else:
        repository.events.append(MessageEvent(seq=3, role="user", message="newer goal"))
    assert await real_dispatch(record) is None
    current = await repository.get(record.session_id, record.key)
    assert current.admission.state == "completed" and current.admission.terminal_kind == "error"
    assert len([event for event in repository.events if isinstance(event, ErrorEvent)]) == 1
    service._input_delivery.retry_preparation.assert_not_awaited()
    service._create_task.assert_not_awaited()
    task.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_safety_rejection_is_not_overridden_to_execute_continuation():
    repository = CheckpointRepository()
    service, task = service_for(repository)
    service._dataset_request_resolver.resolve.return_value = resolution(mode="reject")
    await submit(service, repository)
    service._create_task.assert_not_awaited()
    service._create_lightweight_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_scope_changed_while_gate_was_running_is_rechecked_before_creation():
    repository = CheckpointRepository()
    service, task = service_for(repository)
    async def changed_during_gate(**kwargs):
        repository.session.llm_overrides = {"model_name": "other"}
        return resolution()
    service._dataset_request_resolver.resolve.side_effect = changed_during_gate
    await submit(service, repository)
    service._create_task.assert_not_awaited()
    task.run.assert_not_awaited()
    assert any(isinstance(event, ErrorEvent) for event in repository.events)


@pytest.mark.asyncio
async def test_api_does_not_add_current_auto_skills_or_profile_to_continuation(monkeypatch):
    from app.interfaces.api import session_routes
    user = SimpleNamespace(id="user", role="admin", auto_enabled_skills=["new-auto-skill"])
    capture = {}
    class Agent:
        async def chat(self, **kwargs):
            capture.update(kwargs)
            if False:
                yield None
    profile = AsyncMock(side_effect=AssertionError("resume must not reload a profile override"))
    skills = AsyncMock(side_effect=AssertionError("resume must not merge auto-enabled skills"))
    monkeypatch.setattr(session_routes, "_agent_profile_overrides", profile)
    monkeypatch.setattr(session_routes, "_installed_skill_names", skills)
    response = await session_routes.chat("session", ChatRequest(resume_from=TOKEN, client_message_id="resume-client"),
        Request({"type": "http", "headers": []}), current_user=user, agent_service=Agent(),
        profile_service=object(), user_repository=SimpleNamespace(get_user_by_id=AsyncMock(return_value=user)))
    assert [item async for item in response.body_iterator] == []
    assert capture["resume_from"] == TOKEN and capture["message"] is None
    assert capture["skills"] == [] and capture["mcp_servers"] == []
    assert capture["llm_overrides"] is None and capture["mcp_access_all"] is True


@pytest.mark.asyncio
async def test_changed_current_mcp_permission_cannot_be_restored_from_old_checkpoint():
    repository = CheckpointRepository()
    repository.checkpoint["mcp_access_all"] = True
    service, task = service_for(repository)
    with pytest.raises(ContinuationRejected):
        await submit(service, repository, mcp_access_all=False)
    assert not repository.records
    assert repository.claims == []


@pytest.mark.asyncio
async def test_atomic_input_fence_is_checked_after_acceptance():
    repository = CheckpointRepository()
    service, task = service_for(repository)
    repository.is_analysis_checkpoint_current = AsyncMock(return_value=False)
    await submit(service, repository)
    service._create_task.assert_not_awaited()
    task.run.assert_not_awaited()
    repository.is_analysis_checkpoint_current.assert_awaited_once_with(
        "session", TOKEN, "user", "resume-client", source_seq=1, resume_event_seq=2)
    current = next(iter(repository.records.values()))
    assert current.admission.state == "completed" and current.admission.terminal_kind == "error"


@pytest.mark.asyncio
async def test_application_service_forwards_resume_identity_without_rewriting_goal():
    capture = {}
    class Domain:
        async def chat(self, **kwargs):
            capture.update(kwargs)
            if False:
                yield None
    service = AgentService.__new__(AgentService)
    service._agent_domain_service = Domain()
    assert [event async for event in service.chat("session", "user", resume_from=TOKEN,
                                                  client_message_id="resume-client")] == []
    assert capture["resume_from"] == TOKEN and capture["message"] is None
