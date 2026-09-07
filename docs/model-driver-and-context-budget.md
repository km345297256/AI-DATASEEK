# Model driver and context budget

Phase 6 puts every model created through `create_chat_model` behind one
`LangChainModelDriver`. This includes normal Agent requests, tool-bound calls,
parser repair calls, safety review, completion advice, browser-owned model calls,
dataset routing and suggested-question generation. The driver retains the
existing LangChain interface while making each physical provider request cross a
single preparation, accounting and trace boundary.

This does not add a second Agent loop or a native token-stream protocol. The
driver receives and returns completed messages, and the existing Agent loop and
SSE events remain responsible for user-visible progress. Sandbox isolation,
read-only dataset mounts and the existing DeepSeek adapter behavior, including
disabled provider thinking, are unchanged.

## Scope of enforcement

Every driver request has a per-request context check. `AgentTaskRunner.run`
additionally opens a task-local `model_execution_scope`; its `ContextVar` is
inherited by child asyncio tasks, so repair and tool-owned model requests inside
that task share one token/call ledger.

The dataset bootstrap resolver and suggested-question service currently run
outside `AgentTaskRunner`. They still pass through the driver and receive the
per-request context limit, but they do not consume an Agent task's cumulative
token or call budget and do not acquire that task's Mongo trace store. This
boundary is intentional and should not be described as task-wide coverage of
bootstrap work.

The production runtime's default engineering limits are:

- context capacity: 131,072 estimated tokens;
- per-request safety reserve: 2,048 estimated tokens;
- cumulative Agent task budget: 1,000,000 tokens;
- cumulative Agent task call limit: 128 physical model calls.

The pure `prepare_context` function retains a 65,536-token default so it can be
used independently and explicitly in tests. Production code passes the Settings
value instead. A complete PlanActFlow request with core included currently has
319 tool definitions and estimates 62,237 input tokens. A 65,536-token capacity
with a 4,096-token output reservation and the 2,048-token safety reserve leaves
only 59,392 input tokens, so that request cannot fit unchanged. The 131,072-token
production setting leaves 124,928 input tokens under the same reservations.

These numbers are engineering configuration, not a provider-advertised context
window, account quota, billing meter or price calculation. The currently
configured local `deepseek-v4-flash` model is documented with a 1M-token context
window in the [official DeepSeek documentation](https://api-docs.deepseek.com/quick_start/pricing),
so 131,072 is a conservative application budget for that deployment. When the
deployment switches to a model with a smaller supported window, the configured
capacity must be lowered accordingly.

## Offline context estimate

Before a request is admitted, `prepare_context` calculates:

```text
input_limit = context_capacity - requested_max_output - safety_reserve
```

The versioned `utf8_bytes_div3_v1` estimator is local and performs no network or
tokenizer call. It counts messages, tool schemas and `response_format`; text is
estimated conservatively from `ceil(UTF-8 bytes / 3)` plus structural overhead.
Images receive a separate fixed reservation instead of treating an encoded image
payload as text. A large unrecognized content block fails closed.

Both `input_tokens_before` and `input_tokens_after` are estimates of the full
provider input, including tool schemas and response format. `tool_tokens` is the
tool-schema subset. Provider-reported usage, when available later, is retained as
the actual value rather than relabeling the offline estimate as exact usage.

## Pair-safe context preparation

Preparation operates on deep copies and never mutates persisted Agent messages.
If a request fits, it is passed through unchanged. When it does not fit, only
these bounded transformations are available:

1. Text in a completed assistant/tool exchange may be replaced with a bounded
   head/tail projection and an explicit omitted marker. Exact typed
   `spill://artifact/<id>` references are preserved.
2. Older completed assistant/tool exchanges may be omitted only as whole units.
   An assistant tool call is never separated from any of its tool replies. An AI
   bookkeeping marker records the omission without gaining system authority.

The latest user request, every system instruction, pending or partially paired
tool calls, and the newest complete exchange are not deleted. Completed exchanges
earlier in the latest user turn are eligible, because a long single analysis turn
can append many tool results before the next model request. The newest exchange
remains as evidence even in that case.

If the fixed context still cannot fit, `ContextBudgetExceeded` is raised with
`retryable=False`. Inside an Agent task scope, the runtime converts that boundary
to `ModelBudgetStopped`, an `asyncio.CancelledError` control-flow signal that ends
the dependent turn instead of entering parser or Agent retry loops. The current
request is never silently truncated or discarded.

Each transformation produces a typed `CompactionRecord` containing only its
kind, the original half-open message range, message/unit counts, before/after
estimates and a keyed HMAC. It contains no prompt or tool-result text. Tool output
is never converted into a system message.

## Reservation and settlement

For a scoped request, the ledger atomically reserves the prepared input estimate
plus the requested maximum output before the provider call. The call count is a
count of physical model requests, so repairs and retries are visible rather than
hidden behind an SDK retry loop.

On a successful response with valid provider usage, the ledger settles the
reservation to the reported total and records reported input/output/total token
counts. If usage is absent, malformed, or the provider call fails, the full
reservation remains charged: a failed or unreported request may still have been
billed by the provider. This is conservative runtime accounting, not a financial
usage statement.

## Mongo model traces

`ModelTraceDocument` is registered in the production Beanie model list. A model
request trace records bounded metadata such as task/call identity, public-safe
provider and model identifiers, configured limits, estimates, reservation,
provider usage when present, status/error category, compaction records and keyed
request HMACs. It does not persist prompts, raw compressed content, credentials,
provider endpoints or exception text.

The HMACs support correlation and proof that a request changed across
preparation. They are not reversible and do not provide full-text replay. Older
trace records that lack newly added estimate, usage or compaction fields load via
the model's defaults; ownership and session identity remain required.

Existing memory controls remain separate from request preparation. On the
governed BaseAgent path, active memory is no longer BSON byte-bounded before the
model request. A current large image or user prompt therefore remains intact
until it reaches the driver. `_persist_memory` instead makes a deep durable copy
and applies the byte bound only to that copy before storage. Step reset, bounded
tool results, compacted successful tool arguments, history repair and durable
Spill projection continue to run.

The durable copy's byte-bound truncation is irreversible. Within a task scope,
each actual change is represented by a metadata-only `MemoryChange` containing a
reason, before/after HMACs and message/byte counts. Neither that record nor a
model trace contains the omitted content or can reconstruct it.

Session-scoped trace lookup is available at:

```text
GET /api/v1/sessions/{session_id}/model-traces?task_id={task_id}&limit=100
```

`task_id` is optional and `limit` is capped at 200. The endpoint applies existing
session access checks and the repository filters by both owner and session. Its
response is `data.traces`, containing public metadata views only. A full page
also returns `data.next_cursor`; pass it as `before={next_cursor}` to read older
records while keeping the same session and optional task filter. Pages sort by
creation time descending with trace ID as a stable tie-breaker. A cursor from
another owner/session returns an empty page. There is no Phase 6 frontend trace
card or other new trace UI.

Deleting a session also deletes its `model_traces` records before the parent
session document. The cleanup remains session-scoped and follows the existing
owner-checked session deletion path.

## Configuration

The corresponding Settings environment fields are:

| Environment field | Default | Meaning |
|---|---:|---|
| `MODEL_CONTEXT_CAPACITY_TOKENS` | `131072` | Production engineering capacity passed to the offline request estimate |
| `MODEL_CONTEXT_SAFETY_TOKENS` | `2048` | Input safety reserve in addition to requested output |
| `MODEL_TASK_TOKEN_BUDGET` | `1000000` | Cumulative scoped task token budget |
| `MODEL_TASK_CALL_BUDGET` | `128` | Cumulative scoped physical-call limit |
| `MODEL_TRACE_STORE_TIMEOUT_SECONDS` | `3` | Bound for each trace-store operation |

The model's existing `MAX_TOKENS`/per-call override supplies the requested output
reservation. Within a task scope, a trace-store failure before a changed request
is sent fails closed; the driver never sends a compacted prompt whose audit
record could not be saved.

## Verification

Run the focused backend coverage:

```bash
cd backend
uv run pytest tests/test_context_budget.py tests/test_model_driver.py \
  tests/test_model_runtime.py tests/test_model_budget_cancellation.py \
  tests/test_model_trace_pagination.py
```

The repository-wide required checks remain:

```bash
cd frontend && npm run type-check && npm run build
cd ../backend && uv run pytest
cd ../sandbox && uv run pytest
cd .. && docker compose config --quiet
```

Container updates still use the single supported Compose project through
`./run.sh`; Phase 6 does not create a second stack or frontend port. On the
current single-machine configuration, the frontend/API entry is
`http://127.0.0.1:7001`. With an existing session ID, metadata can be inspected
from the CLI without exposing model content:

```bash
SESSION_ID=replace-with-session-id
curl -fsS "http://127.0.0.1:7001/api/v1/sessions/${SESSION_ID}/model-traces?limit=100"
```
