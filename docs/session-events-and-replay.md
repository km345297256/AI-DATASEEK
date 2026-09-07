# Versioned session events and deterministic replay

Phase 2 adds replayability around the existing PlanActFlow. It does not replace
the Agent loop, the FastAPI host, the current SSE event vocabulary, or the
per-session Docker isolation boundary.

## Event envelope

Every newly persisted `AgentEvent` carries:

- `version: 1`, the event-envelope schema version;
- `seq`, a positive session-scoped integer no greater than JavaScript's
  `Number.MAX_SAFE_INTEGER`;
- the existing `id`, which remains the Redis stream cursor; and
- the existing timestamp and typed event payload.

The Mongo session document owns an atomic `event_seq` allocator. Before Redis
publication, a producer seals a private logical event ID and atomically upserts
a durable record in `session_event_reservations`. That record maps a SHA-256 of
the producer ID to one sequence and a canonical semantic-payload digest. Two
interleaved producers with the same logical ID therefore adopt the same winning
sequence; reusing that ID for different content fails before publication. The
raw producer ID is a Pydantic private attribute and never enters event JSON,
SSE, recordings, or their schemas. Redis may then replace the public event
`id` with its stream cursor without changing durable idempotency.

The event document stores `seq` and `version` beside the full event, links to
the reservation by its bounded hash, and enforces uniqueness for
`(session_id, seq)`. Existing `event_key` documents remain readable and are
lazily adopted into reservations when their stable ID is encountered. A failed
or losing concurrent reservation may leave a gap; allocated numbers are not
reused because reuse would make reconnect deduplication ambiguous. Deleting a
session cascades to its internal reservation records.

Documents written before Phase 2 remain valid. Missing `version` means version
1, and history loading projects legacy events onto stable sequence values in
their original creation order. Explicit unknown versions, invalid numbers, and
sequence exhaustion fail closed.

## SSE resume behavior

SSE event names and payload-specific fields are unchanged. `seq` and `version`
are additive fields, and the wire `id` is still the Redis event ID. A client may
resume with the existing body `event_id` (or standard `Last-Event-ID`) and the
new body `event_seq`.

For a versioned reconnect, FastAPI first reads persisted events after the
client watermark, then attaches to the current Redis stream from its beginning.
Overlap is removed by `seq`. Persisted user messages advance the watermark but
are not emitted because the current Web UI already renders a submitted user
message optimistically. The Vue cursor rejects duplicate, out-of-order, unsafe,
or unknown-version envelopes; legacy events use a bounded event-ID window.
Fully versioned history uses the `(session_id, seq)` Mongo index directly;
legacy or mixed history retains the complete ordering/synthesis fallback.

The live ordering invariant is one sequential publisher per running session
task. The current single-process task bootstrap lock and the two sequential
task runners preserve that invariant. The Mongo allocator guarantees unique,
monotonic sequence values, but it does not by itself serialize concurrent
Redis `XADD` calls; a future multi-worker or parallel-publisher design must add
distributed per-session publication serialization. Redis delivery remains
at-least-once, so a concurrent retry can produce two stream cursors carrying
the same `seq`; versioned clients intentionally collapse that duplicate.

## Execution environment snapshot

Each real task writes one immutable `ExecutionEnvironmentSnapshot` before its
first assistant/tool output. Agent tasks capture after Cordis catalog pinning,
MCP discovery, namespace validation, and production interceptor installation.
Lightweight tasks capture their front-controller environment without inventing
a sandbox. A task cannot silently change environment after this point.

Snapshots live in the separate `execution_environment_snapshots` collection,
keyed uniquely by task ID. Retrying the same task and fingerprint is a no-op;
attempting to overwrite the task with a different fingerprint fails closed.
Old sessions simply have no snapshots.

The snapshot records only replay identities:

- Cordis engine/version, revision, manifest digest, and execution-bundle digest;
- sandbox runtime, safe image identity, dataset-mount count and digest;
- model role, safe provider/model labels, built-in prompt digest, and a digest
  of allowlisted numeric/configuration facts; and
- the effective post-discovery tool-name/policy digests and counts.

It never serializes API keys, credential values, environment variables, MCP
connection details, host absolute paths, user prompts, custom system-prompt
text, dataset contents, or raw tool arguments. Private low-entropy identities
that must affect reproducibility—custom system prompts, selected MCP servers,
discovered MCP tool contracts, and effective Skill definitions—cross a single
server-keyed HMAC-SHA256 boundary; only the resulting 64-character opaque value
is stored and included in the fingerprint. API keys and credentials are not
HMAC message inputs (the configured server secret, or compatibility fallback
`API_KEY`, acts only as the HMAC key). Task/session IDs, `trigger_event_seq`,
and capture time are excluded from the stable environment fingerprint, so
equivalent environments compare equal across runs.

## Recorded-session test format

`event_recording.py` provides a bounded, offline JSONL format. The first line is
a versioned header with the session ID, capture time, event count and sequence
watermarks, zero or more execution snapshots, a `privileged-raw` content label,
and a SHA-256 digest. Each later line contains one complete, unprojected
`AgentEvent`. Canonical JSON uses sorted keys and the recording is a
normalization fixed point: loading and dumping an untouched fixture produces
identical bytes.

The digest covers header content (including execution snapshots) and all event
payloads. The loader rejects duplicate JSON keys, non-finite values, malformed
UTF-8, oversized recordings, duplicate event IDs, missing/non-increasing/unsafe
sequences, excessive line counts before allocating per-line strings, unknown
format or event versions, snapshot/session mismatch, snapshot triggers that do
not point to a recorded user message, and content tampering. Replay only
reconstructs validated domain events; tests pass those events through the
shipping `EventMapper`, without contacting a model or executing a tool.

Lossless replay means event bodies may contain the same private user/tool data
as Mongo session history. The recorder is intentionally not connected to an
HTTP route or browser control, performs no filesystem writes, and is not a
sanitizer. Any future export must add explicit authorization and either retain
the privileged forensic classification or introduce a separate public
projection; committed fixtures must contain synthetic data only.

The committed baseline is
`backend/tests/fixtures/replays/versioned_session.jsonl`. Refreshing a baseline
is an explicit developer action and its JSONL diff must be reviewed. Normal
test runs are read-only.

## Focused verification

```bash
cd backend && uv run pytest \
  tests/test_event_sequence.py \
  tests/test_execution_environment_snapshot.py \
  tests/test_event_recording_replay.py

cd ../frontend && npm test && npm run type-check
```

These checks supplement the repository-wide checks in `AGENTS.md`.
