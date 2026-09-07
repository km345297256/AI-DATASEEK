# Spill Artifact Store

AI-DataSeek stores oversized text tool results outside the Agent transcript.
The existing PlanActFlow, tool call protocol, SSE event names, and sandbox
isolation boundary remain unchanged.

## Data path

1. A tool completes normally and returns its ordinary `ToolMessage`.
2. The production result interceptor measures its final UTF-8 text.
3. Results strictly larger than `SPILL_MAX_INLINE_BYTES` are saved through the
   `SpillArtifactStore` capability.
4. The current model turn receives a bounded head/tail preview, byte counts,
   SHA-256, and opaque `spill://artifact/<id>` locator. Before the corresponding
   event enters Redis/Mongo or SSE, credentials and host paths in that preview
   are redacted on a cloned durable projection; the typed locator is preserved.
5. The in-process raw `ToolMessage.artifact` remains available only to the
   current deterministic completion hook; it is not placed in Agent memory,
   Redis, Mongo events, or SSE.
6. The Agent can page the full text with `spill_artifact_read`, using
   `next_byte` until `eof=true`. Each page is measured again after JSON
   escaping so control characters cannot expand past the inline ceiling.
   GridFS and MinIO both use native byte-range reads, so later pages never
   download or skip the complete prefix again.
   Read-page events and persisted Agent memory retain only page metadata; raw
   page text is delivered to the current model invocation. Spill previews in
   persisted Agent memory use the same credential/path-redacted projection.

The final 2 MiB event bound remains a defense-in-depth limit. It is no longer
the normal truncation mechanism for oversized tool text.

## Storage and access

`FileStorageSpillArtifactStore` reuses the configured GridFS, MinIO, or hybrid
file storage for bytes. A separate `spill_artifacts` Mongo collection maps a
keyed opaque artifact id to the internal storage object. Neither the storage
file id, MinIO object key, container path, nor host path is returned to the
browser or model.

Every read checks both `user_id` and `session_id`, then verifies the private
storage metadata and exact byte size. Storage objects use a keyed internal
principal rather than the browser/API user identity, and the ordinary file
service rejects the reserved spill metadata namespace. Tool names and call ids
are stored only as bounded hashes. Spill records are not added to
`Session.files`, so they do not appear as downloadable analysis products.

The artifact id is deterministic for owner, source, and content digest. A
retry of the same tool call therefore reuses one durable record. Set a stable
`SPILL_ARTIFACT_IDENTITY_KEY` in production so ids remain stable across backend
replicas and restarts.

## Failure and lifecycle behavior

Waiting for a spill is best effort and bounded by
`SPILL_STORE_TIMEOUT_SECONDS`. A timed-out provider write is not cancelled in
the middle: it finishes the store's record-or-rollback protocol in the
background, avoiding untracked objects. The current tool notice reports
`status=unavailable`; a storage failure never changes the original tool success
bit, and the raw large body is still removed from model/event projections. This
intentionally protects Redis, Mongo, and SSE even when object storage is slow
or unhealthy.

The runner joins pending saves from all interceptor bundles before signalling
that it has closed. Session deletion waits for this barrier before deleting
owner artifacts. This can extend task cleanup when a storage provider is slow;
the five-second publication timeout is not a storage-operation deadline.

If the metadata write reports an uncertain outcome, reconciliation retries
before deleting anything. When the outcome remains unknown, an auxiliary
cleanup record retains the uploaded object for later reconciliation. If MongoDB
is completely unavailable and even that record cannot be written, the object
is retained and an opaque warning is logged; this exceptional case requires
storage reconciliation after recovery rather than guaranteeing automatic TTL
cleanup.

Deleting a session first marks its private locator records as deleting, which
immediately denies reads, and removes each record only after its storage object
is gone. Failed object cleanup remains as a tombstone and is retried by the
periodic reaper, even when creation of new spills is disabled. The reaper applies
`SPILL_ARTIFACT_RETENTION_HOURS`. A missing or expired locator returns a stable
not-found result and never reruns the original tool.
Cleanup matches the expected backing file id so an old cleanup task cannot
remove a replacement mapping. Retry candidates rotate by their last cleanup
attempt, allowing later expired objects to progress when older deletes fail.

This post-execution store cannot prevent a provider from allocating an
unbounded result before the interceptor runs. Existing sandbox/plugin response
caps remain mandatory; provider-side streaming or early spill is a separate
future hardening step.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `SPILL_ENABLED` | `true` | Enable production spill policy and read tool |
| `SPILL_MAX_INLINE_BYTES` | `50000` | Strict UTF-8 inline threshold |
| `SPILL_MAX_ARTIFACT_BYTES` | `33554432` | Per-result storage ceiling; larger results stay bounded but unavailable |
| `SPILL_PREVIEW_BYTES` | `12000` | Head/tail preview budget |
| `SPILL_READ_CHUNK_BYTES` | `16000` | Maximum bytes per authorized read |
| `SPILL_STORE_TIMEOUT_SECONDS` | `5` | Maximum time to publish one spill |
| `SPILL_ARTIFACT_RETENTION_HOURS` | `168` | Backend retention window |
| `SPILL_REAPER_INTERVAL_SECONDS` | `300` | Expired-record sweep interval |

Session/event replay is reference-based: persisted tool events keep the bounded,
redacted preview and typed locator metadata, while follow-up model context
restores only the locator, byte count, media type, and write-time content digest.
It never reinjects raw output or the preview. After the seven-day default
retention expires, the event remains replayable while full artifact retrieval
returns not found.
The SHA-256 is provenance computed before upload; paginated reads validate
owner, private metadata, and size, rather than rescanning the whole object for
every page.
