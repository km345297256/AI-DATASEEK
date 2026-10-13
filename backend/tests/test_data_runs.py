"""Run evidence remains scoped, durable, bounded and owner-authorized."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.domain.models.event import RunEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, DoneEvent, ErrorEvent, WaitEvent, MessageEvent
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.services.data_runs import data_run_id, project_data_run
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.application.services.agent_service import AgentService
from app.application.errors.exceptions import NotFoundError
from app.interfaces.api.session_routes import get_data_runs
from app.interfaces.schemas.event import EventMapper


def start(session="s", source=1, seq=2):
    return RunEvent(phase="started", run_id=data_run_id(session, source), source_event_seq=source, seq=seq)


def success(id="inspect"):
    return Step(id=id, status=ExecutionStatus.COMPLETED, success=True,
                outcome=AnalysisOutcome(status="succeeded", reason_code="completed"))


def plan_event(run, steps, seq=5, status=PlanStatus.COMPLETED):
    return PlanEvent(run_id=run.run_id, seq=seq, status=status, plan=Plan(steps=steps, status=ExecutionStatus.COMPLETED if status == PlanStatus.COMPLETED else ExecutionStatus.RUNNING))


def test_same_session_runs_and_reused_step_ids_are_independent():
    one, two = start(), start(source=8, seq=9)
    assert one.run_id != two.run_id
    assert data_run_id("s", 1) == one.run_id
    events = [plan_event(one, [success()]), plan_event(two, [Step(id="inspect", status=ExecutionStatus.RUNNING)], seq=10, status=PlanStatus.UPDATED)]
    a, b = project_data_run(one, events), project_data_run(two, events)
    assert a.status == "succeeded" and b.status == "running"
    assert a.steps[0].step_id != b.steps[0].step_id
    assert a.revision == 5 and b.revision == 10


def test_successful_stage_never_finishes_run_and_done_does_not_invent_success():
    run = start()
    events = [StepEvent(run_id=run.run_id, seq=3, status=StepStatus.COMPLETED, step=success())]
    assert project_data_run(run, events).status == "running"
    events.append(DoneEvent(run_id=run.run_id, seq=4))
    assert project_data_run(run, events).status == "unknown"


@pytest.mark.parametrize("event,status", [(WaitEvent, "waiting"), (ErrorEvent, "failed"), (DoneEvent, "unknown")])
def test_terminal_evidence_cannot_be_reversed_by_late_plan_or_duplicate_events(event, status):
    run = start()
    terminal = event(run_id=run.run_id, seq=4, **({"error": "private"} if event is ErrorEvent else {}))
    late = plan_event(run, [success()])
    view = project_data_run(run, [late, terminal, late, terminal])
    assert view.status == status
    assert "private" not in view.model_dump_json()


@pytest.mark.parametrize("steps,expected", [([], "unknown"), ([Step(status=ExecutionStatus.RUNNING)], "unknown"),
    ([Step(status=ExecutionStatus.FAILED)], "failed"), ([success(), Step(status=ExecutionStatus.FAILED)], "partial"),
    ([Step(success=True, status=ExecutionStatus.COMPLETED)], "partial")])
def test_plan_completion_requires_all_step_evidence(steps, expected):
    run = start()
    assert project_data_run(run, [plan_event(run, steps)]).status == expected


def test_truncated_or_orphaned_history_never_claims_complete_or_live():
    run = start()
    assert project_data_run(run, [plan_event(run, [success()])], history_complete=False).status == "unknown"
    assert project_data_run(run, [], session_active=False).status == "unknown"
    assert project_data_run(run, []).recovery == "inspect_only"


@pytest.mark.asyncio
async def test_run_ids_survive_durable_event_and_sse_mapping():
    run = start()
    mapped = await EventMapper.event_to_sse_event(run)
    assert mapped.event == "run" and mapped.data.run_id == run.run_id
    step = StepEvent(run_id=run.run_id, seq=3, status=StepStatus.COMPLETED, step=success())
    assert (await EventMapper.event_to_sse_event(step)).data.run_id == run.run_id
    assert "run_id" not in MessageEvent(message="legacy").model_dump()
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._active_run_id = run.run_id
    runner._session_id, runner._agent_id = "s", "a"
    repo = SimpleNamespace(add_event=AsyncMock())
    runner._session_repository = repo
    task = SimpleNamespace(output_stream=SimpleNamespace(put=AsyncMock(return_value="cursor")))
    await runner._put_and_add_event(task, DoneEvent())
    saved = repo.add_event.call_args.args[1]
    assert saved.run_id == run.run_id
    assert '"run_id":"' + run.run_id in task.output_stream.put.call_args.args[0]


@pytest.mark.asyncio
async def test_run_routes_and_service_check_session_access_before_reading_evidence():
    repo = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=None), get_run_starts=AsyncMock())
    service = AgentService.__new__(AgentService)
    service._session_repository = repo
    with pytest.raises(NotFoundError):
        await get_data_runs("private", before_seq=None, current_user=SimpleNamespace(id="other"), agent_service=service)
    repo.get_run_starts.assert_not_awaited()
    with pytest.raises(RuntimeError):
        await service.get_data_runs("private", "other")
    repo.get_run_starts.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_page_is_bounded_and_old_sessions_are_not_fabricated():
    from app.domain.models.session import SessionStatus
    repo = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=SimpleNamespace(status=SessionStatus.COMPLETED)),
                           get_run_starts=AsyncMock(return_value=[]), get_run_events=AsyncMock(return_value=[]))
    service = AgentService.__new__(AgentService)
    service._session_repository = repo
    assert (await service.get_data_runs("s", "owner")).runs == []
    starts = [start(source=i * 10 + 1, seq=i * 10 + 2) for i in range(11, 0, -1)]
    repo.get_run_starts.return_value = starts
    page = await service.get_data_runs("s", "owner")
    assert len(page.runs) == 10 and page.next_before_seq == starts[9].seq
    assert all(run.status == "unknown" for run in page.runs)
    assert repo.get_run_events.await_count == 10


def test_environment_receipt_is_immutable_and_contains_only_public_release_identity():
    from app.domain.models.data_run import RunSkillRelease
    run = start()
    release = RunSkillRelease(package_id="lab/netcdf-profile", version="1.0.0", sha256="a" * 64)
    first = RunEvent(run_id=run.run_id, seq=3, phase="environment", source_event_seq=1,
                     skills=[release], legacy_skill_count=1, environment_fingerprint="b" * 64, catalog_revision="c" * 64)
    late = first.model_copy(update={"seq": 4, "skills": [], "environment_fingerprint": "d" * 64})
    view = project_data_run(run, [late, first])
    assert view.skills == [release] and view.environment_fingerprint == "b" * 64
    assert view.catalog_revision == "c" * 64 and view.legacy_skill_count == 1
    assert set(view.skills[0].model_dump()) == {"package_id", "version", "sha256"}


def test_active_jobs_block_success_and_stale_job_revisions_are_ignored():
    from app.domain.models.analysis_job import AnalysisJobView, AnalysisJobStatus
    from app.domain.models.event import ToolEvent, ToolStatus
    run = start()
    job = AnalysisJobView(tool_name="analyze", status=AnalysisJobStatus.RUNNING)
    tool = ToolEvent(run_id=run.run_id, seq=3, tool_call_id="call", tool_name="analysis", function_name="analyze",
                     function_args={}, status=ToolStatus.CALLING, analysis_job=job)
    assert project_data_run(run, [tool, plan_event(run, [success()], seq=6)]).status == "unknown"
    done = tool.model_copy(update={"seq": 4, "analysis_job": job.model_copy(update={"revision": 2, "status": AnalysisJobStatus.SUCCEEDED})})
    late_old = tool.model_copy(update={"seq": 5})
    assert project_data_run(run, [tool, done, late_old, plan_event(run, [success()], seq=6)]).status == "succeeded"


def test_recording_roundtrip_retains_run_ids_and_public_version_receipts():
    from app.domain.services.event_recording import create_event_recording, load_event_recording_jsonl
    run = start()
    recorded = create_event_recording(session_id="s", events=[run, plan_event(run, [success()])])
    replay = load_event_recording_jsonl(recorded.to_jsonl())
    assert replay.to_jsonl() == recorded.to_jsonl()


@pytest.mark.asyncio
async def test_only_latest_run_can_remain_active_after_a_new_request():
    from app.domain.models.session import SessionStatus
    older, latest = start(), start(source=10, seq=11)
    repo = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=SimpleNamespace(status=SessionStatus.RUNNING)),
                           get_run_starts=AsyncMock(return_value=[latest, older]), get_run_events=AsyncMock(return_value=[]))
    service = AgentService.__new__(AgentService)
    service._session_repository = repo
    page = await service.get_data_runs("s", "owner")
    assert [run.status for run in page.runs] == ["running", "unknown"]


@pytest.mark.asyncio
async def test_real_runner_registers_each_input_before_flow_and_keeps_run_identity():
    persisted = []
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._session_id, runner._agent_id, runner._user_id = "s", "agent", "owner"
    runner._sandbox = SimpleNamespace(ensure_api_ready=AsyncMock())
    runner._dataset_service = SimpleNamespace(mounted_datasets=AsyncMock(return_value=[]))
    runner._flow = SimpleNamespace()
    runner._sync_message_attachments_to_sandbox = AsyncMock()
    runner._capture_artifact_baseline = AsyncMock()
    runner._remember_mounted_dataset_paths = lambda datasets: None
    runner._front_controller_resolution = None
    runner._session_events_snapshot = None
    counter = 30

    async def reserve(session, event):
        nonlocal counter
        counter += 1
        event.seq = counter

    async def persist(session, event):
        persisted.append(event.model_copy(deep=True))

    runner._session_repository = SimpleNamespace(reserve_event_sequence=reserve, add_event=persist,
        update_status=AsyncMock(), update_latest_message=AsyncMock(), increment_unread_message_count=AsyncMock())

    async def flow(message, **kwargs):
        assert isinstance(persisted[-1], RunEvent) and persisted[-1].phase == "started"
        yield StepEvent(status=StepStatus.COMPLETED, step=success())
        yield DoneEvent()

    runner._run_flow = flow
    inputs = iter([("first", MessageEvent(role="user", message="first", seq=1).model_dump_json()),
                   ("second", MessageEvent(role="user", message="second", seq=20).model_dump_json()), (None, None)])

    async def pop():
        return next(inputs)

    task = SimpleNamespace(id="task", pop_input_or_close=pop, output_stream=SimpleNamespace(put=AsyncMock(return_value="output")))
    await runner._run_with_model_budget(task)
    assert [event.type for event in persisted] == ["run", "step", "done", "run", "step", "done"]
    assert {event.run_id for event in persisted[:3]} == {data_run_id("s", 1)}
    assert {event.run_id for event in persisted[3:]} == {data_run_id("s", 20)}
