# AnalysisJob

Phase 4 gives sandbox analysis executions a durable, queryable lifecycle. The
existing PlanActFlow, FastAPI host, SSE transport and read-only dataset mounts
remain in use. Cordis plugin calls and the core `shell_run`, `dataset_unpack`
and `dataset_quicklook` tools are tracked automatically. Interactive
`shell_exec` is excluded because returning “running” is not process completion.

## Lifecycle and execution

An AnalysisJob represents one execution attempt. It moves from `queued` to
`running`, then `succeeded`, `failed`, `cancelled`, `timed_out` or `interrupted`.
A cancellation request is recorded as `cancelling` before the worker is signalled.
Mongo updates compare the current revision and state, so late updates cannot
reopen a terminal job. The queue state covers the existing concurrency lock;
no synthetic completion percentage is shown.

The job interceptor runs after the policy guard and encloses the existing
timeout/concurrency interceptors. The actual operation runs in a separately
cancellable asyncio task; the Agent still awaits its ordinary tool result.
Production timeout limits are unchanged (currently at most 120 seconds).
This release does not introduce an independent distributed job queue or
detached jobs that outlive their Agent task.

Cancelling a job invokes the existing sandbox process cleanup and stops the
dependent conversation turn through its existing cancellation path. Other
sessions are unaffected. A cancellation never enters the compiler's repair
loop or the model's tool retry loop. Previously produced files remain in the
session sandbox; cancellation does not roll back filesystem side effects.

The job is completed after the result transformers, so a third-stage spill
reference can be attached to the job. The record contains only metadata, a
hashed tool-call reference, the execution-snapshot task ID, catalog revision,
and optional typed spill reference. It contains no command, input arguments,
result body, host storage path, or exception text. File products still follow
the existing session attachment workflow.

## Recovery and cleanup

Each runtime renews leases for its live workers. The backend checks every five
seconds, with a default 30-second lease. A crashed runtime's unfinished jobs
become `interrupted` after lease expiry. They are not rerun automatically:
an earlier attempt may already have written files. Live workers that lose
their lease are cancelled, and a new analysis requires a new user request.

Session deletion first stops and joins its Agent task, then removes its job
records and private spill artifacts. Job records otherwise remain with the
session's analysis history. A job-state write failure after execution does not
replace the actual tool result or cause an automatic rerun; an unconfirmed
terminal state is later reported as interrupted.

## API and UI

- `GET /api/v1/sessions/{session_id}/analysis-jobs?limit=100`
- `GET /api/v1/sessions/{session_id}/analysis-jobs/{job_id}`
- `POST /api/v1/sessions/{session_id}/analysis-jobs/{job_id}/cancel`

Cancellation requires `X-Analysis-Job-Action: cancel`. All endpoints check
session access and filter by both owner and session. Cancellation is restricted
to the session owner and rejects a tool declared non-cancellable. Read-only
share pages never expose cancellation controls.

The existing `tool` SSE event gains an optional `analysis_job` object. Its
`schema_version` and `revision` are independent of the session event's existing
`version` and `seq`. Tool detail cards show state, elapsed time and the tool's
time limit. While open, a live card queries the job to reconcile missed updates;
revision checks prevent stale SSE/poll responses from reverting state. Polling
stops at a terminal state or when the card is closed.

`ANALYSIS_JOBS_ENABLED=true` enables new records by default. Disabling creation
does not disable lookup, cancellation, or recovery of existing records.

## Verification

Run an ordinary dataset analysis, open its tool detail panel, and observe the
analysis-job card. For a sufficiently long call, click **取消此作业**: its state
should move through cancelling to cancelled, and the current turn stops without
repeating the operation. Reloading the session restores its recorded job state.
The API can list job history and inspect its execution-snapshot/spill references.

Automated coverage includes status transitions, owner isolation, queued
cancellation, process cleanup, timeout, stale leases, terminal CAS, unchanged
tool results, spill/SSE integration, and frontend revision reconciliation.
