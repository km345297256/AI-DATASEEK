import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.domain.models.tool_result import ToolResult
from app.domain.services.credential_service import CredentialService
from app.domain.services.tool_approval_service import ToolApprovalService
from app.domain.services.tools.authorization import ToolAuthorizationStopped, ToolCallAuthorizationInterceptor
from app.domain.services.tools.interceptors import ToolPolicySnapshot, create_production_tool_interceptors
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.plugin import PluginToolkit
from app.infrastructure.external.tool_approval_factory import get_tool_approval_service
from app.interfaces.api.tool_approval_routes import router
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.domain.external.plugin_runtime import ToolExecutionDescriptor
from pydantic import ValidationError
from test_credential_service import InMemoryCredentialRepository
from test_tool_approval_service import InMemoryToolApprovalRepository
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.event_recording import create_event_recording, load_event_recording_jsonl
from app.interfaces.schemas.event import ToolSSEEvent


def services():
    repository = InMemoryToolApprovalRepository()
    approvals = ToolApprovalService(repository)
    credentials = CredentialService(InMemoryCredentialRepository(), encryption_key=Fernet.generate_key())
    return approvals, credentials, repository


def test_credential_contract_cannot_choose_environment_or_omit_effect():
    allowed = {"effects": ["credential_use"], "credentials": [{"slot": "api_key", "provider": "example"}]}
    assert ToolExecutionDescriptor.model_validate(allowed).credentials[0].slot == "api_key"
    for bad in [
        {"credentials": allowed["credentials"]},
        {**allowed, "credentials": [{"slot": "PATH", "provider": "example"}]},
        {**allowed, "credentials": [{"slot": "api_key", "provider": "example", "env": "PATH"}]},
        {**allowed, "credentials": allowed["credentials"] * 2},
        {"permissions": ["https://private.example/secret"]},
    ]:
        with pytest.raises(ValidationError):
            ToolExecutionDescriptor.model_validate(bad)


def chain(approvals, credentials, *, sink=None, wait=1, names=("remote_read", "local_read"), policy=None):
    policy = policy or ToolPolicySnapshot.for_registered_tools(names)
    authorizer = ToolCallAuthorizationInterceptor(
        policy, approvals=approvals, credentials=credentials,
        user_id="user-1", session_id="session-1", event_sink=sink,
        identity_provider=lambda: {"task_id": "task-1", "execution_snapshot_id": "task-1", "catalog_revision": "a" * 64},
        poll_seconds=0.002, wait_seconds=wait,
    )
    return ToolExecutionPipeline(create_production_tool_interceptors(
        policy_snapshot=policy, call_authorization_interceptor=authorizer,
    ))


async def invoke(pipeline, execute, *, name="remote_read", args=None, effects=None, permissions=None):
    return await pipeline.invoke(
        tool=SimpleNamespace(name=name), tool_call={"name": name, "id": "call-1", "args": args or {}},
        metadata={"plugin": "fixture", "execution_contract": {
            "effects": effects if effects is not None else ["network"], "permissions": permissions or [],
        }}, execute=execute,
    )


@pytest.mark.asyncio
async def test_local_tools_remain_unchanged_and_permissions_are_not_permanent():
    approvals, credentials, repository = services()
    seen = []
    async def approve(view, context):
        seen.append(view)
        if view.status == "pending":
            await approvals.decide("user-1", "session-1", view.approval_id, "approved", view.revision)
    pipeline = chain(approvals, credentials, sink=approve)
    handler = AsyncMock(return_value="real result")
    assert await invoke(pipeline, handler, name="local_read", effects=["sandbox_read"]) == "real result"
    assert not repository.records
    for _ in range(2):
        assert await invoke(pipeline, handler, permissions=["dataset:export"]) == "real result"
    assert len(repository.records) == 2
    assert all(record.status == "consumed" for record in repository.records.values())
    assert [view.status for view in seen].count("pending") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["rejected", None])
async def test_rejection_and_expiry_stop_before_execution_without_retry(decision):
    approvals, credentials, repository = services()
    async def decide(view, context):
        if view.status == "pending" and decision:
            await approvals.decide("user-1", "session-1", view.approval_id, decision, view.revision)
    handler = AsyncMock()
    with pytest.raises(ToolAuthorizationStopped):
        await invoke(chain(approvals, credentials, sink=decide, wait=0.03), handler)
    handler.assert_not_awaited()
    assert len(repository.records) == 1
    assert next(iter(repository.records.values())).status in {"cancelled", "rejected", "expired"}


@pytest.mark.asyncio
async def test_pending_approval_does_not_hold_execution_lock_and_call_arguments_are_frozen():
    approvals, credentials, repository = services()
    pending = asyncio.Event()
    async def sink(view, context):
        if view.status == "pending":
            pending.set()
    pipeline = chain(approvals, credentials, sink=sink)
    original = {"dataset": "reviewed-data"}
    executed = []
    async def run(context):
        executed.append(context.arguments)
        return "done"
    task = asyncio.create_task(invoke(pipeline, run, args=original))
    try:
        await asyncio.wait_for(pending.wait(), 1)
        original["dataset"] = "different-data"
        assert await invoke(pipeline, AsyncMock(return_value="local"), name="local_read", effects=["sandbox_read"]) == "local"
        record = next(iter(repository.records.values()))
        await approvals.decide("user-1", "session-1", record.approval_id, "approved", record.revision)
        assert await asyncio.wait_for(task, 1) == "done"
        assert executed == [{"dataset": "reviewed-data"}]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_approval_cannot_override_registry_denial():
    approvals, credentials, repository = services()
    handler = AsyncMock()
    with pytest.raises(ToolAuthorizationStopped):
        await invoke(chain(approvals, credentials, policy=ToolPolicySnapshot.deny_all()), handler)
    assert not repository.records
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_credential_plugin_uses_private_transport_after_consent_and_scrubs_output(tmp_path, revoke):
    approvals, credentials, repository = services()
    secret = "phase-five-test-secret"
    binding = await credentials.create(user_id="user-1", provider="example", tool_name="remote_read", slot="api_key", secret=SecretStr(secret))
    plugin = tmp_path / "fixture"
    plugin.mkdir()
    (plugin / "manifest.json").write_text(json.dumps({
        "plugin": "fixture", "version": "1.0.0", "handler": "handler.py",
        "tools": [{"name": "remote_read", "description": "Read protected data", "parameters": {"type": "object"},
                   "execution": {"effects": ["network", "credential_use"], "credentials": [{"slot": "api_key", "provider": "example"}]}}],
    }))
    sandbox = SimpleNamespace(
        exec_command=AsyncMock(), exec_command_with_credentials=AsyncMock(return_value=ToolResult(
            success=True, data={"status": "completed", "returncode": 0, "output": json.dumps({"value": secret})},
        )), release_shell=AsyncMock(), kill_process=AsyncMock(),
    )
    toolkit = PluginToolkit(sandbox, session_id="session-1", plugins_dir=tmp_path)
    published = []
    async def approve(view, context):
        published.append(view.model_dump_json())
        if view.status == "pending":
            if revoke:
                await credentials.revoke("user-1", binding.reference)
            await approvals.decide("user-1", "session-1", view.approval_id, "approved", view.revision)
    toolkit.tool_execution_pipeline = chain(approvals, credentials, sink=approve)
    operation = toolkit.get_tool("remote_read").ainvoke({"name": "remote_read", "id": "call-1", "args": {"dataset": "public-data"}})
    if revoke:
        with pytest.raises(ToolAuthorizationStopped):
            await operation
        sandbox.exec_command_with_credentials.assert_not_awaited()
    else:
        result = await operation
        assert result.artifact.success
        assert secret not in result.content and secret not in result.artifact.model_dump_json()
        args = sandbox.exec_command_with_credentials.call_args.args
        assert secret not in args[2]
        assert args[3] == {"api_key": secret}
        sandbox.release_shell.assert_awaited_once()
    sandbox.exec_command.assert_not_awaited()
    assert secret not in "".join(published)
    assert secret not in "".join(record.model_dump_json() for record in repository.records.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_at", ["consume", "publish"])
async def test_revocation_during_admission_never_executes_with_a_previously_resolved_secret(monkeypatch, revoke_at):
    approvals, credentials, repository = services()
    binding = await credentials.create(
        user_id="user-1", provider="example", tool_name="remote_read", slot="api_key",
        secret=SecretStr("admission-race-fixture-secret"),
    )
    original_consume = approvals.consume

    async def consume(approval_id, digest):
        if revoke_at == "consume":
            await credentials.revoke("user-1", binding.reference)
        return await original_consume(approval_id, digest)

    async def sink(view, context):
        if view.status == "pending":
            await approvals.decide("user-1", "session-1", view.approval_id, "approved", view.revision)
        elif view.status == "consumed" and revoke_at == "publish":
            await credentials.revoke("user-1", binding.reference)

    monkeypatch.setattr(approvals, "consume", consume)
    handler = AsyncMock(return_value="must not execute")
    pipeline = chain(approvals, credentials, sink=sink)
    with pytest.raises(ToolAuthorizationStopped):
        await pipeline.invoke(
            tool=SimpleNamespace(name="remote_read"),
            tool_call={"name": "remote_read", "id": "call-1", "args": {}},
            metadata={"plugin": "fixture", "execution_contract": {
                "effects": ["network", "credential_use"],
                "credentials": [{"slot": "api_key", "provider": "example"}],
            }},
            execute=handler,
        )
    handler.assert_not_awaited()
    assert next(iter(repository.records.values())).status == "consumed"


@pytest.mark.asyncio
async def test_approval_api_scope_header_revision_and_public_projection():
    approvals, _, _ = services()
    record = await approvals.create(user_id="user-1", session_id="session-1", task_id="task-1", tool_name="remote_read",
        call_digest="a" * 64, effects=["network"], permissions=[], arguments_preview={"url": "https://example.test/data"}, credential_refs=[])
    agents = SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(user_id="user-1")))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_tool_approval_service] = lambda: approvals
    app.dependency_overrides[get_agent_service] = lambda: agents
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-1")
    with TestClient(app) as client:
        url = f"/sessions/session-1/tool-approvals/{record.approval_id}"
        body = client.get(url).json()["data"]
        assert "call_digest" not in body and "user_id" not in body
        assert client.get(url.replace("session-1", "session-2")).status_code == 404
        decision = {"decision": "approved", "expected_revision": 1}
        assert client.post(url + "/decision", json=decision).status_code == 403
        headers = {"X-Tool-Approval-Action": "decide"}
        agents.get_session.return_value = SimpleNamespace(user_id="other-owner")
        assert client.post(url + "/decision", json=decision, headers=headers).status_code == 404
        agents.get_session.return_value = SimpleNamespace(user_id="user-1")
        assert client.post(url + "/decision", json=decision, headers=headers).status_code == 200
        assert client.post(url + "/decision", json=decision, headers=headers).status_code == 409


@pytest.mark.asyncio
async def test_approval_sse_recording_keeps_original_display_identity_and_bound_sequence():
    approvals, credentials, _ = services()
    persisted, serialized = [], []
    async def put(payload):
        serialized.append(payload)
        return f"redis-{len(serialized)}"
    async def reserve(session_id, event):
        event.seq = len(serialized) + 1
    async def add(session_id, event):
        persisted.append(event)
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._session_id = "session-1"
    runner._analysis_job_tools = {}
    runner._analysis_job_views = {}
    runner._tool_approval_views = {}
    runner._session_repository = SimpleNamespace(add_event=add, reserve_event_sequence=reserve)
    task = SimpleNamespace(output_stream=SimpleNamespace(put=put))
    template = ToolEvent(tool_call_id="call-1", tool_name="plugin", function_name="remote_read", function_args={"label": "原始调用"}, status=ToolStatus.CALLING)
    await runner._put_and_add_event(task, template)
    async def sink(view, context):
        await runner._publish_tool_approval(task, view, context)
        if view.status == "pending":
            await approvals.decide("user-1", "session-1", view.approval_id, "approved", view.revision)
    await invoke(chain(approvals, credentials, sink=sink), AsyncMock(return_value="done"), args={"path": "/Users/private/data", "api_key": "private-argument"})
    await runner._put_and_add_event(task, template.model_copy(update={"status": ToolStatus.CALLED}))
    assert all(event.function_args == {"label": "原始调用"} for event in persisted)
    assert "private-argument" not in "".join(serialized) and "/Users/private" not in "".join(serialized)
    assert [event.seq for event in persisted] == list(range(1, len(persisted) + 1))
    sse = await ToolSSEEvent.from_event_async(persisted[-1])
    assert sse.event == "tool" and sse.data.tool_approval.status == "consumed"
    recording = create_event_recording(session_id="session-1", events=persisted)
    assert load_event_recording_jsonl(recording.to_jsonl()).to_jsonl() == recording.to_jsonl()
