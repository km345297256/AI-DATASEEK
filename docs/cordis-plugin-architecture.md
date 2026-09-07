# Cordis plugin architecture

AI-DataSeek uses a compatibility boundary around the existing Agent runtime:
Cordis owns discovery and lifecycle for trusted, bundled analysis-tool plugins,
while FastAPI remains the application host and the existing Agent loop remains
the only component that plans and executes turns.

## Runtime topology

```text
Vue plugin catalog ── REST ──> FastAPI
                                 │
                                 ├─ existing Agent loop
                                 ├─ Redis/Mongo session events ── SSE ──> browser
                                 │
                                 └─ stdio NDJSON RPC
                                           │
                                    Node 22 Cordis host
                                    ├─ Context
                                    ├─ catalog service
                                    └─ one Fiber per tool manifest
                                           │ catalog snapshot
                                           ▼
                                    PluginToolkit adapter
                                           │ invocation
                                           ▼
                                    per-session Docker sandbox
                                    └─ ai-dataseek-tool runner
```

The Cordis host does not run model turns, own sessions, access datasets, or
publish browser events. It registers manifest metadata and produces an
immutable catalog snapshot. Tool implementation code continues to execute in
the per-session Docker sandbox so the read-only dataset mount remains the
security boundary.

## Compatibility contracts

The migration deliberately keeps these contracts stable:

- `BaseAgent.get_tools()` and `BaseAgent.get_tool()` remain the Agent-facing
  registry interface.
- A resolved tool still exposes `name`, `toolkit.name`, and
  `ainvoke(tool_call)`, and returns the existing LangChain `ToolMessage` with a
  `ToolResult` artifact.
- The toolkit order and lookup API do not change. Before a turn begins, the
  complete model-facing namespace must be unique; ambiguous Cordis, core, or
  dynamically discovered MCP names fail closed instead of relying on order.
- `dataset_fast_path` scopes from existing manifests remain available to the
  deterministic dataset path.
- A running Agent receives one fixed catalog revision. Reloaded registrations
  are visible to subsequently created task runtimes, never halfway through a
  model/tool turn.
- Every catalog tool is normalized to Tool Contract v2. Legacy aliases remain
  available, while output schema, execution effects/permissions, concurrency,
  cancellation capability, and declarative presentation metadata are carried
  as one immutable descriptor.
- `POST /api/v1/sessions/{session_id}/chat` and every SSE event name remain
  unchanged. The existing `event_id` is still the Redis resume cursor. The
  payload now adds a session-scoped `seq` and explicit `version`; old history
  without either field is projected as version 1 in its original order.
  Cordis lifecycle events are internal and are not forwarded to SSE.

## Versioned session event stream

Every new session event uses envelope version `1`. Mongo atomically increments
the owning session's event counter and persists a producer-identity reservation
before either an input or output event is serialized into Redis, so live
delivery and durable history carry the same sequence. The private producer ID
is distinct from the later Redis cursor; only a bounded identity hash and
semantic-payload digest are stored. Concurrent retries adopt one sequence, while
reuse of the same producer ID for different content fails closed before Redis.
Sequence gaps are allowed when work fails after reservation: a number is never
reused because reuse would make reconnect deduplication ambiguous.
Sequences are strict positive integers bounded by JavaScript's
`Number.MAX_SAFE_INTEGER`; exhaustion and unknown explicit envelope versions
fail closed rather than losing ordering precision or guessing a schema.

Versioned clients send both `event_id` and `event_seq` when reconnecting. The
backend replays durable events after `event_seq`, then reads the current Redis
stream from its beginning and suppresses the overlap by sequence. Persisted
user events advance the cursor but are not emitted, preserving the existing
optimistic user-message UI behavior. Legacy clients may continue sending only
`event_id`; standard SSE `Last-Event-ID` is also accepted and remains a Redis
stream ID. The frontend rejects duplicate or out-of-order versioned events and
uses a bounded event-ID window for old events that do not have a sequence.

Execution snapshots and the privileged offline recording format are specified
in [`session-events-and-replay.md`](session-events-and-replay.md).

## Tool Contract v2 validation

The Cordis host normalizes every bundled manifest before publishing a catalog
generation. Input and non-null output schemas use JSON Schema draft-07, may
reference only resolvable local JSON pointers, and must use the portable,
bounded regular-expression subset accepted by both Node and Python. The
backend validates arguments before starting a handler and validates normalized
`ToolResult.data` afterward. A declared-output mismatch is returned as a small
`contract_rejected` result; the rejected payload is quarantined from model
context, SSE, and presentation cards. Legacy `output_schema: null` remains
explicitly untyped rather than pretending that arbitrary output was checked.

The sandbox runner retains at most 2 MiB across handler stdout and stderr and
terminates an overflowing process group. The backend independently applies a
2 MiB captured-output ceiling. These limits protect the current in-memory
path; Phase 3's spill artifact store will provide the supported path for large
results rather than weakening them.

## Production invocation boundary

After dynamic MCP discovery and immediately before a turn, `PlanActFlow` checks
the complete tool namespace, pins an immutable registry policy, and installs
one interceptor bundle on every toolkit pipeline. Empty toolkits are included
in this check so a tool discovered later cannot bypass policy. Each invocation
sees one stable interceptor snapshot with this supported order:

1. structured trace and existing audit-store projection;
2. fail-closed policy evaluation for exact tool identity, declared effects,
   and permissions;
3. the AnalysisJob lifecycle, followed by a bounded execution deadline;
4. per-session/plugin/tool concurrency (`exclusive` or `parallel`); and
5. single-use call-grant admission and private credential resolution before the
   tool executes. Results still cross the Spill/presentation boundaries.

The trace emits one start and exactly one terminal state (`succeeded`, `denied`,
`timed_out`, `cancelled`, or `failed`). It records bounded metadata and hashed
call/permission references, not argument values, credentials, host paths, or
raw exception messages. Trace-sink failure is best effort and cannot replace a
tool's original error or swallow cancellation.

The registry policy allows only tools registered for that turn and the known
effect vocabulary. Phase 5 adds per-invocation approval for named permissions,
network/credential/external effects and an owner-scoped encrypted credential
broker; it does not turn declarations into permanent grants. The complete
scope and remaining trust boundaries are specified in
[`tool-credentials-and-approvals.md`](tool-credentials-and-approvals.md).

Every plugin call receives a fresh opaque shell-session identifier.
Timeout or caller cancellation asks the sandbox to kill/release that execution;
the sandbox tracks pre-cancel requests and terminates the complete process
group, including descendants, across process-creation races.

## Declarative cards over SSE

The existing `tool` SSE event gains only an optional `presentation` field. The
allowlisted card kinds are `generic`, `table`, `chart`, `map`, `image`,
`artifact`, and `log`; `auto` keeps the legacy tool display. Vue selects a
checked-in renderer from that enum—plugins cannot name components or inject
HTML, JavaScript, CSS, or arbitrary URLs.

Before browser delivery, tool arguments, content, and card data cross the
public-data sanitizer. It removes credential-like fields and host paths, bounds
depth/item/field/node counts, and caps the complete compact-JSON encoding of
`presentation.data` at 256,000 UTF-8 bytes, including keys and JSON syntax.
Image and artifact links must be relative same-application
`/api/v1/files/<id>` URLs, optionally with the exact signed-file query shape.
The frontend repeats the enum, URL, secret, and size checks as defense in depth.

## Ownership and failure behavior

The FastAPI lifespan owns the Cordis child process. Communication uses
newline-delimited JSON-RPC over stdio; stdout is reserved for protocol frames
and diagnostics use stderr. The child receives a scrubbed environment rather
than the backend model and storage credentials.

Catalog reload builds and validates a new Cordis context before swapping it
into service. Invalid manifests, duplicate plugin names, duplicate tool names,
reserved Agent names, invalid schemas, malformed UTF-8, or missing/escaping
handlers reject the reload and preserve the last valid snapshot. There is no
silent fallback to a second Python manifest scan when the configured Cordis
runtime is unavailable. If the Node child exits unexpectedly, task creation
attempts to restart it before capturing the task's immutable catalog snapshot.

Each catalog generation contains two compatibility guards:

- `manifest_digest` identifies the exact set of manifest bytes and plugin
  versions.
- `execution_bundle_digest` identifies the checked-in tool tree plus the
  sandbox API, runner/operator sources, and dependency contract used to
  execute those tools.

Both guards are attached to every sandbox tool invocation. The sandbox hashes
its own generation before loading a handler and rejects the call if either
guard differs. This prevents a backend-only rebuild, stale session sandbox, or
remote worker from silently executing code different from the schema shown to
the Agent.

The browser receives a presentation-only catalog DTO. Model-facing JSON Schema
parameters stay inside the backend, so schema defaults cannot disclose host
paths or credentials. The reload mutation requires the configured same origin
and a non-simple action header; a rejected candidate is reported alongside the
still-active catalog revision until a later reload succeeds.

The first phase adapts the existing trusted `tools/*/manifest.json` packages.
It does not load arbitrary browser JavaScript or third-party Node modules.
Community plugin execution will require a separately isolated plugin service,
an explicit permission model, and a package-verification policy.

`execution_bundle_digest` is a source-contract guard, not a cryptographic
attestation of the complete OS image. The backend and sandbox images must be
built and deployed together with `./run.sh up -d --build`. A sandbox that was
created before such an upgrade will reject new-generation plugin calls; start a
new session (or retire the old sandbox) to adopt the new image. Dependency
resolution should continue moving toward fully pinned plugin lock files before
multi-host rolling deployment is enabled.

## Extension path

New analysis domains should contribute a manifest with a stable plugin name and
version, a handler contained inside its plugin directory, plus one or more
uniquely named tools. Model-facing tool names must match
`[A-Za-z_][A-Za-z0-9_-]{0,63}`. Each tool must provide a
description, object-shaped JSON input schema, bounded timeout, and optional
scopes. Adding a domain must not require changes to the Agent loop or SSE
adapter.

New tools should also declare Tool Contract v2 `output_schema`, `execution`,
and `presentation` metadata. `output_schema` applies to normalized
`ToolResult.data`; legacy process output is represented honestly as `null`.
Execution declarations are policy inputs, not grants: an interceptor must still
authorize permissions and effects for each call. `cancellable: true` may be
declared only when the execution adapter can actually stop the work. A
presentation descriptor chooses a sanitized card kind and static labels; it
cannot contain runtime payloads, arbitrary HTML, scripts, or URLs.
`exclusive` concurrency serializes calls with the same plugin/tool identity
inside one backend process; it is not a distributed lock. Its timeout budget
includes both lock wait and execution time.

Later phases may expose MCP, Skills, and Renderers through the same catalog
view, but their existing storage, installation, permission, and prompt
semantics remain authoritative until an explicit migration is implemented.

## Verification

The `/plugins?tab=runtime` page and `GET /api/v1/plugins/runtime` expose the
active engine, health, revision, digests, and catalog counts without exposing
model schemas. A healthy Cordis generation has `engine: "cordis"`, non-empty
revision/digests, and the expected plugin/tool counts. Reload from the page is
an atomic candidate swap; an invalid candidate must leave the displayed active
revision unchanged and report an error.

The focused checks for this boundary are:

```bash
cd plugin-host && npm test
cd ../backend && uv run pytest \
  tests/test_node_plugin_runtime.py \
  tests/test_plugin_toolkit.py \
  tests/test_tool_execution_pipeline.py \
  tests/test_tool_execution_interceptors.py \
  tests/test_tool_presentation_schema.py
cd ../frontend && npm test && npm run type-check
cd ../sandbox && uv run pytest \
  tests/test_tool_plugin_runner.py \
  tests/test_shell_service.py
```

These focused checks supplement, rather than replace, the repository-wide
checks in `AGENTS.md`.
