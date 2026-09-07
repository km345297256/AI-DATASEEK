# Execution environment snapshots

Each analysis task freezes a versioned, immutable execution-environment
snapshot. The snapshot is stored in the `execution_environment_snapshots`
sidecar collection rather than in SSE events, so existing event history and
older sessions remain compatible. A session created before this feature simply
has no snapshots.

## Capture boundary

- Agent tasks capture after MCP discovery and production interceptor
  installation, and before `PlanActFlow.run` can yield its first event.
- Rejected or empty agent requests capture before their first response event;
  these snapshots may intentionally have no effective toolset because no tool
  execution boundary was installed.
- Lightweight tasks capture after front-controller resolution and before their
  first response event. They never allocate or claim a sandbox identity.

The task id is the immutability key. Retrying the same persistence write with
the same fingerprint is idempotent. Attempting to write a different fingerprint
for the same task fails closed. The in-memory runners enforce the same rule: if
a later message is delivered to an already-running task after its catalog,
model, sandbox, or tool environment changes, the task raises an error instead
of silently changing its recorded environment. A new task gets a new snapshot.

## Safe identity boundary

The persisted model is a strict whitelist. It includes:

- Cordis version, catalog revision, manifest digest, execution-bundle digest,
  and plugin/tool counts;
- sandbox runtime kind, safe image reference, immutable image digest when
  available, and a sanitized dataset-mount identity;
- safe provider/model labels plus digests of repository-owned prompts and
  whitelisted numeric configuration, plus a server-keyed HMAC when a custom
  system prompt is active;
- the effective post-discovery tool registry and policy digests and counts.

Skill selection is resolved before the agent snapshot is written. The snapshot
records both requested and actually active counts. MCP server/tool contracts,
the selected MCP server set, and the effective Skill definitions are identified
with HMAC-SHA256 under a server-only key. This distinguishes two environments
with equal counts while keeping private names, descriptions, schemas,
instructions, and custom prompts out of the serialized record. These values are
never fed to an ordinary unkeyed hash.

Set `EXECUTION_SNAPSHOT_IDENTITY_KEY` to an independent random secret of at
least 32 bytes. Keep it stable across restarts, image upgrades, replicas, and
restore operations; rotation intentionally changes new environment
fingerprints. For upgrade compatibility, an unset dedicated key falls back to
the already-required model `API_KEY`, using a purpose-derived HMAC key. The
fallback is stable across restarts as long as that API key is unchanged, but a
dedicated secret avoids coupling provenance to model credential rotation.

The stable fingerprint covers those environment fields but deliberately omits
task id, session id, and capture time. It therefore compares reproducibility
across runs without making the execution context part of the environment.

`trigger_event_seq` records the sequence of the user `MessageEvent` that
started the task. It is excluded from the fingerprint and lets an offline
recording associate multiple task snapshots with their corresponding event
segments.

API keys, authorization headers, complete environment/configuration maps, MCP
commands, and host connection details are excluded entirely. Custom prompts and
effective Skill/MCP contracts enter only the keyed HMAC boundary; their raw
text (including any accidentally embedded host path) is never serialized or fed
to an ordinary hash. Snapshot records are backend provenance and are not
included in browser-facing session or SSE payloads.

Deleting a session deletes its execution snapshot sidecars before deleting the
session record, preventing task provenance from being orphaned by normal user
deletion.
