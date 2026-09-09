# Analysis execution: metering without task quotas

New dataset/general-analysis requests have no cumulative tool-batch, model-call,
token or wall-clock quota. This is an explicit product policy for the local
installation, not a larger numerical allowance. Execution continues while it
can make progress, until completion, cancellation or an unrecoverable failure.
The service remains at `127.0.0.1:7001`; updates use the existing Compose project
and `./run.sh`.

## Removed task limits

- The former 12 → 14 → 16 tool-batch allowance and automatic grant review.
- The per-input and outer Task limits of 128 model calls / 1,000,000 tokens.
- The original input's 900-second cumulative deadline.
- Profile and domain-agent `max_iterations` ceilings on analysis work.

Retired `ANALYSIS_BUDGET_*` and `MODEL_TASK_*_BUDGET` environment settings do not
reactivate these limits. Production policy represents absent ceilings as
`None`, including in runtime/model-trace snapshots. Explicit bounded policies
may be used by internal tests; they are not production configuration.

## Controls that remain

1. **Execution evidence.** A private sandbox receipt binds the original command,
   operation nonce and process generation. A confirmed nonzero exit is a failed
   attempt, not an unknown execution. An exited leader alone does not prove its
   process group has stopped. Observation queries never resubmit the command.
2. **Progress and recovery.** Repeated identical reads, failed writes and repeated
   whole-program drafts without execution feedback are detected. A pending
   operation is observed through its original identity. When status cannot be
   established or the model repeatedly proposes blocked work, execution safely
   concludes instead of consuming more model calls in an ineffective loop.
3. **Per-operation protection.** Provider/network and individual tool timeouts,
   bounded safe retries, context-capacity limits, parser/file bounds, input
   leases, cancellation and the read-only dataset mount boundary remain. These
   protect individual operations and data; they are not total task quotas.
4. **Delivery verification.** Actual uploaded bytes, hashes, formats and required
   outputs determine completion. A saved script is not a rendered chart.
   Failures and incomplete outputs are reported truthfully; increasing or
   removing a quota never manufactures a successful analysis.

Progress events update one transient UI status. They are not appended as answer
paragraphs. Completion, failure, cancellation and conversation switches clear
the transient status. Real final messages and verified attachments remain.

Provider balances, rate limits and context capacities are external constraints;
this change cannot remove them. Explicit billing errors are not retried as
temporary rate limits. This change does not introduce or claim new CPU/memory
limits for the main analysis sandbox.

## Accounting and safe continuation

Usage and execution identities remain auditable even though they no longer
authorize numerical quota grants. The private original-input lineage retains
owner/session/scope identity. Every admission validates the current input lease;
duplicate or ambiguous reservations must not authorize the same physical call
twice. Model estimates are reconciled with provider usage when available.
Missing usage remains an estimate, and accounting failure never causes a
potentially billed provider request to be replayed.

Request-local execution/progress state is shared across plan steps and helper
agents, and closed when its generator exits or is cancelled. New checkpoints
retain private lineage and progress identities under their content HMAC.
Absence of a total budget or deadline must not disable an otherwise safe
continuation.

Execution confirmation and replay permission are separate. A successful or
failed shell exit does **not** authorize replay of arbitrary partially executed
work. Upload-only recovery can reuse already verified files without repeating
computation. Unknown execution must not be reclassified as safe merely because
the request is now unlimited.

## Scope and verification

This rollout concerns new tasks on the current analysis image. It deliberately
does not implement old-environment protocol compatibility, migrate old
containers, repair historical tasks, rewrite their records or rerun them.

Regression coverage must include:

- Successful progressing tasks exceeding the former tool/model/token limits.
- Work past the former total deadline, with individual timeouts still effective.
- Task/profile configuration unable to reinstate retired cumulative limits.
- Cancellation, closed execution scopes, original-input leases and duplicate
  reservation rejection with unlimited accounting.
- Pending execution recovery and terminal unknown state without repeated
  status paragraphs or an unbounded blocked-operation loop.
- Safe checkpoints and validated output delivery after formerly capped usage.
- Frontend live/history progress projection and cleanup on every terminal path.

Tests use synthetic fixtures and isolated test databases. They do not consume
the configured model key or rerun user analysis. Required gates remain frontend
type-check/build, frontend tests, backend/sandbox pytest and Compose validation.

Verified on 2026-09-08: backend 1,757 passed / 29 environment-specific or
superseded-test skips, frontend 184 passed with type-check and build, sandbox
297 passed. Real Mongo concurrency tests use disposable databases. The actual
new sandbox image also passed `python -m scripts.verify_analysis_recovery`:
confirmed nonzero exit, synthetic Excel analysis, rendered PNG byte validation
and temporary-fixture cleanup. Post-deployment read-only inspection reported
all seven task/agent cumulative limits as null and the local frontend healthy.
