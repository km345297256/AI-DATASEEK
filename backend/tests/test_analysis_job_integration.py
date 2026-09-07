import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.messages import ToolMessage

from app.domain.models.analysis_job import AnalysisJobStatus
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.analysis_job_service import AnalysisJobService
from app.domain.services.event_recording import create_event_recording, load_event_recording_jsonl
from app.domain.services.tools.analysis_job import AnalysisJobCancelled, AnalysisJobInterceptor
from app.domain.services.tools.interceptors import ToolConcurrencyInterceptor, ToolExecutionTimeoutError, ToolTimeoutInterceptor
from app.domain.services.tools.pipeline import ToolExecutionPipeline
from app.domain.services.tools.spill import SpillArtifactInterceptor
from app.domain.services.tools.spill_projection import projected_tool_artifact
from app.interfaces.api.analysis_job_routes import router
from app.interfaces.dependencies import get_agent_service, get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.infrastructure.external.analysis_job_factory import get_analysis_job_service
from app.interfaces.schemas.event import ToolSSEEvent
from test_analysis_job_service import InMemoryAnalysisJobRepository, _create
from test_spill_artifact_store import CapturingStore, OWNER


def pipeline(service, *, sink=None, timeout=1, spill=None):
    interceptors = [AnalysisJobInterceptor(
        service, user_id="user-1", session_id="session-1",
        identity_provider=lambda: {"task_id": "task-1", "execution_snapshot_id": "task-1", "catalog_revision": "a" * 64},
        event_sink=sink,
    ), ToolTimeoutInterceptor(timeout, maximum_timeout_seconds=timeout), ToolConcurrencyInterceptor()]
    if spill:
        interceptors.append(SpillArtifactInterceptor(spill, owner=OWNER, max_inline_bytes=1024, preview_bytes=200))
    return ToolExecutionPipeline(interceptors)


async def invoke(chain, execute, call_id="call-1", *, name="shell_run"):
    return await chain.invoke(
        tool=SimpleNamespace(name=name),
        tool_call={"name": name, "id": call_id, "args": {"command": "secret-source-program"}},
        execute=execute,
    )


def result(content="analysis result", success=True):
    artifact = ToolResult(success=success, data={"output": content})
    return ToolMessage(tool_call_id="call-1", name="shell_run", content=artifact.model_dump_json(), artifact=artifact)


@pytest.mark.asyncio
async def test_job_lifecycle_sse_retains_display_identity_and_spill_reference():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    serialized = []
    persisted = []

    async def put(payload):
        serialized.append(payload)
        return f"redis-{len(serialized)}"

    async def add(session_id, event):
        persisted.append(event)

    async def reserve(session_id, event):
        event.seq = len(serialized) + 1

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent-1"
    runner._session_id = "session-1"
    runner._analysis_job_tools = {}
    runner._analysis_job_views = {}
    runner._session_repository = SimpleNamespace(add_event=add, reserve_event_sequence=reserve)
    task = SimpleNamespace(output_stream=SimpleNamespace(put=put))
    template = ToolEvent(tool_call_id="call-1", tool_name="shell", function_name="dataset_analysis_run",
                         function_args={"command": "分析数据集并生成成果"}, status=ToolStatus.CALLING)
    await runner._put_and_add_event(task, template)
    chain = pipeline(service, sink=lambda view, ctx: runner._publish_analysis_job(task, view, ctx), spill=CapturingStore())
    raw = result("x" * 5000)
    transformed = await invoke(chain, AsyncMock(return_value=raw))
    await runner._put_and_add_event(task, template.model_copy(update={
        "status": ToolStatus.CALLED, "function_result": projected_tool_artifact(transformed),
    }))
    record = next(iter(repository.records.values()))
    assert record.status == AnalysisJobStatus.SUCCEEDED
    assert record.result_spill is not None
    assert record.execution_snapshot_id == "task-1"
    assert transformed.artifact is raw.artifact
    assert "secret-source-program" not in "".join(serialized)
    assert [event.seq for event in persisted] == list(range(1, len(persisted) + 1))
    for event in persisted:
        assert event.function_name == "dataset_analysis_run"
    mapped = await ToolSSEEvent.from_event_async(persisted[-1])
    assert mapped.event == "tool"
    assert mapped.data.analysis_job.job_id == record.job_id
    assert mapped.data.spill.reference == record.result_spill
    assert "runtime_id" not in mapped.model_dump_json()
    recording = create_event_recording(session_id="session-1", events=persisted)
    restored = load_event_recording_jsonl(recording.to_jsonl())
    assert restored.to_jsonl() == recording.to_jsonl()
    assert restored.events[-1].analysis_job.job_id == record.job_id


@pytest.mark.asyncio
async def test_targeted_cancel_kills_execution_and_bypasses_retry_without_touching_other_job():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    started = asyncio.Event()
    killed = asyncio.Event()

    async def execute(context):
        context.register_cancellation_callback(lambda reason: killed.set())
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(invoke(pipeline(service), execute))
    await asyncio.wait_for(started.wait(), 1)
    job_id = next(iter(repository.records))
    assert await service.request_cancel("other-user", "session-1", job_id) is None
    assert not task.done()
    await service.request_cancel("user-1", "session-1", job_id)
    with pytest.raises(AnalysisJobCancelled):
        await asyncio.wait_for(task, 1)
    assert killed.is_set()
    assert repository.records[job_id].status == AnalysisJobStatus.CANCELLED
    second = await invoke(pipeline(service), AsyncMock(return_value=result()), "call-2")
    assert second.artifact.success is True


@pytest.mark.asyncio
async def test_deadline_is_recorded_as_timeout_and_runs_sandbox_cleanup():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    killed = asyncio.Event()

    async def execute(context):
        context.register_cancellation_callback(lambda reason: killed.set())
        await asyncio.Event().wait()

    with pytest.raises(ToolExecutionTimeoutError):
        await invoke(pipeline(service, timeout=0.02), execute)
    assert killed.is_set()
    assert next(iter(repository.records.values())).status == AnalysisJobStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_exclusive_job_waits_in_queued_state_and_can_be_cancelled_before_execution():
    repository = InMemoryAnalysisJobRepository()
    service = AnalysisJobService(repository)
    chain = pipeline(service)
    started = asyncio.Event()
    release = asyncio.Event()
    second_created = asyncio.Event()

    async def first(context):
        started.set()
        await release.wait()
        return result()

    first_task = asyncio.create_task(invoke(chain, first, "first"))
    await started.wait()
    original_create = service.create

    async def create(**kwargs):
        record = await original_create(**kwargs)
        second_created.set()
        return record

    service.create = create
    second_execute = AsyncMock(return_value=result())
    second_task = asyncio.create_task(invoke(chain, second_execute, "second"))
    await second_created.wait()
    second_job = list(repository.records.values())[-1]
    assert second_job.status == AnalysisJobStatus.QUEUED
    await service.request_cancel("user-1", "session-1", second_job.job_id)
    with pytest.raises(AnalysisJobCancelled):
        await second_task
    second_execute.assert_not_awaited()
    release.set()
    await first_task


@pytest.mark.asyncio
async def test_non_analysis_tools_are_unchanged_and_terminal_database_failure_does_not_retry_output():
    service = AnalysisJobService(InMemoryAnalysisJobRepository())
    execute = AsyncMock(return_value=result())
    await invoke(pipeline(service), execute, name="file_read")
    assert await service.list_for_owner("user-1", "session-1") == []
    service.finish = AsyncMock(side_effect=RuntimeError("private database detail"))
    output = await invoke(pipeline(service), execute)
    assert output.artifact.success is True


@pytest.mark.asyncio
async def test_api_owner_isolation_cancellation_header_and_public_projection():
    service = AnalysisJobService(InMemoryAnalysisJobRepository())
    record = await _create(service)
    agents = SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(user_id="user-1")))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_analysis_job_service] = lambda: service
    app.dependency_overrides[get_agent_service] = lambda: agents
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-1")
    with TestClient(app) as client:
        url = f"/sessions/session-1/analysis-jobs/{record.job_id}"
        response = client.get(url)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["job_id"] == record.job_id
        assert "runtime_id" not in data and "user_id" not in data
        assert client.get(url.replace("session-1", "session-2")).status_code == 404
        assert client.post(url + "/cancel").status_code == 403
        agents.get_session.return_value = SimpleNamespace(user_id="other-owner")
        assert client.post(url + "/cancel", headers={"X-Analysis-Job-Action": "cancel"}).status_code == 404
        agents.get_session.return_value = SimpleNamespace(user_id="user-1")
        response = client.post(url + "/cancel", headers={"X-Analysis-Job-Action": "cancel"})
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "cancelling"
        assert client.get("/sessions/session-1/analysis-jobs").json()["data"]["jobs"]
