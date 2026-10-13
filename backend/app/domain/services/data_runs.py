"""Run identities and deterministic read models; no execution or automatic retry."""
import hashlib
from app.domain.models.data_run import DataRun, DataRunStep
from app.domain.models.event import RunEvent, PlanEvent, PlanStatus, StepEvent, StepStatus, DoneEvent, WaitEvent, ErrorEvent, ToolEvent


def data_run_id(session_id: str, source_event_seq: int) -> str:
    return hashlib.sha256(f"data-run-v1:{session_id}:{source_event_seq}".encode()).hexdigest()[:32]


def project_data_run(start: RunEvent, events, *, history_complete: bool = True, session_active: bool = True) -> DataRun:
    view = DataRun(run_id=start.run_id, source_event_seq=start.source_event_seq,
                   revision=start.seq or 0, started_at=int(start.timestamp.timestamp()), history_complete=history_complete)
    steps = {}
    jobs = {}
    closed = False
    environment_seen = False
    seen = set()
    for event in sorted(events, key=lambda item: item.seq or 0):
        if event.run_id != start.run_id or not event.seq or event.seq < (start.seq or 0) or event.seq in seen:
            continue
        seen.add(event.seq)
        view.revision = max(view.revision, event.seq)
        if closed:
            continue
        if isinstance(event, RunEvent) and event.phase == "environment":
            # The first admitted environment is immutable for this run.
            if not environment_seen and event.source_event_seq == start.source_event_seq:
                environment_seen = True
                view.environment_fingerprint = event.environment_fingerprint
                view.catalog_revision = event.catalog_revision
                view.skills = event.skills
                view.legacy_skill_count = event.legacy_skill_count
        elif isinstance(event, ToolEvent) and event.analysis_job:
            job = event.analysis_job
            previous = jobs.get(job.job_id)
            if previous is None or job.revision > previous.revision:
                jobs[job.job_id] = job
        elif isinstance(event, PlanEvent):
            view.plan_revision = event.seq
            steps = {step.id: step for step in event.plan.steps}
            if event.status == PlanStatus.COMPLETED:
                succeeded = event.plan.status.value == "completed" and bool(steps) and all(
                    step.status.value == "completed" and step.success
                    and step.outcome is not None and step.outcome.status == "succeeded"
                    and not step.outcome.missing and not any(issue.blocking for issue in step.outcome.issues)
                    for step in steps.values()
                )
                active_jobs = any(job.status not in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
                                  for job in jobs.values())
                if succeeded and not active_jobs:
                    view.status = "succeeded"
                elif active_jobs or not steps or any(step.status.value in {"pending", "running"} for step in steps.values()):
                    view.status = "unknown"
                else:
                    view.status = "partial" if any(step.success for step in steps.values()) else "failed"
                view.ended_at = int(event.timestamp.timestamp())
                closed = True
        elif isinstance(event, StepEvent):
            steps[event.step.id] = event.step
        elif isinstance(event, ErrorEvent):
            view.status = "failed"
            view.ended_at = int(event.timestamp.timestamp())
            closed = True
        elif isinstance(event, WaitEvent):
            view.status = "waiting"
            closed = True
        elif isinstance(event, DoneEvent):
            # Message stream closure alone never certifies a successful run.
            view.status = "unknown"
            view.ended_at = int(event.timestamp.timestamp())
            closed = True
    if not history_complete or (not closed and not session_active):
        view.status = "unknown"
    for ordinal, step in enumerate(steps.values(), 1):
        status = {"completed": "succeeded", "failed": "failed", "running": "running", "pending": "pending"}[step.status.value]
        if status == "succeeded" and (not step.success or (step.outcome and step.outcome.status != "succeeded")):
            status = "failed"
        if view.status in {"unknown", "failed", "partial"} and status in {"pending", "running"}:
            status = "unknown"
        view.steps.append(DataRunStep(
            step_id=hashlib.sha256(f"{start.run_id}:{step.id}".encode()).hexdigest()[:32],
            ordinal=ordinal, status=status, outcome_status=step.outcome.status if step.outcome else None,
        ))
    return view
