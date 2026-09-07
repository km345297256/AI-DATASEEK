# CredentialRef and call-level approvals

Phase 5 adds single-call consent and an encrypted tool-credential vault. The
existing PlanActFlow/AgentLoop, FastAPI host, SSE event names, AnalysisJob and
read-only dataset mounts remain unchanged. It does not enable the removed
platform administration or introduce a second approval-driven Agent loop.

## Authorization boundary

The pinned registry policy remains the ceiling: an approval cannot enable an
unknown tool, override an explicit denial or add an unsupported effect.
Registered, purely local analysis retains its existing behavior. A declaration
of `network`, `credential_use`, `external_side_effect`, or any named permission
requires explicit consent for **this invocation**. Host-owned browser/search
calls are classified as network operations; MCP tool calls conservatively
require consent for network/external effects. Existing MCP connection/discovery
configuration and its separate server-level approval semantics are not migrated.

The invocation order is trace → call-authorization guard → AnalysisJob →
execution deadline → concurrency lock → single-use grant admission → tool.
The approval wait is outside the execution deadline and lock, defaults to five
minutes, and does not occupy an AnalysisJob worker. A rejected, expired,
cancelled or unavailable authorization stops the dependent turn without a
compiler repair or automatic tool retry. Other sessions remain unaffected.

Before displaying a request, the guard freezes the call's arguments and binds
them with a purpose-specific HMAC to owner, session, task, call ID, tool,
execution snapshot, sandbox identity, catalog generation, execution contract,
and credential reference/revision. The database receives only the digest and
a bounded, sanitized argument preview—not the original arguments or command.
Oversized/unreviewable requests fail closed.

After concurrency admission, the digest is checked again and an atomic Mongo
transition consumes the approved grant once. After its event notification, the
vault re-reads and revalidates the credential bindings before releasing values.
A revoke during grant consumption or notification therefore stops execution;
the consumed grant stays consumed even if this final credential check fails.
Browser responses cannot provide a grant or change its scope. Retrying a tool
requires a new approval, even when a framework reuses the old tool-call ID.

Approval states are `pending`, `approved`, `rejected`, `consumed`, `expired`,
and `cancelled`. `consumed` means authorization was used, not that the analysis
succeeded; AnalysisJob/the ordinary tool result still report execution outcome.
Pending and approved grants expire, and a different/restarted backend runtime
cannot consume them. Restarting never resumes an approved operation by itself.

## Credential vault and plugin contract

`CREDENTIAL_ENCRYPTION_KEY` is a dedicated Fernet key. It has no fallback to the
DeepSeek/model API key. Missing or invalid configuration disables vault writes
and decryption, not ordinary local analysis. Back up the key privately with the
encrypted database; replacing or losing it makes old credentials unreadable.
Existing DeepSeek and MCP configuration is deliberately left unchanged.

New trusted plugins can declare up to eight purpose-specific slots:

```json
{
  "execution": {
    "timeout_seconds": 90,
    "cancellable": true,
    "concurrency": "exclusive",
    "effects": ["sandbox_read", "network", "credential_use"],
    "permissions": ["example:data-read"],
    "credentials": [{"slot": "api_key", "provider": "example"}]
  }
}
```

Both Cordis and Python validate this additive Tool Contract v2 field. Slot and
provider are bounded identifiers; duplicate slots and environment-variable
overrides are rejected. Credentials require the `credential_use` effect.

An owner creates a binding for an exact `(tool_name, slot, provider)` declared
by the healthy current Cordis catalog. Only one active binding per owner/tool/
slot is allowed. The public identity is `cred_<32 hex characters>` plus version
and status. The API never reads back plaintext or ciphertext. Revocation clears
the stored ciphertext and invalidates unconsumed approvals using that binding;
replacement requires creating a new reference. It does not recall a secret
already delivered to an executing process or roll back prior side effects.

The broker selects bindings server-side; models do not supply secret values or
grant flags. Only after consent/admission does the private sandbox adapter send
the required values over the existing internal REST connection. Values appear
in one child process's `DATASEEK_CREDENTIAL_<UPPERCASE_SLOT>` environment—not
in command text, arguments, the Cordis host, or the sandbox server's environment.
The ordinary next command does not inherit them. Older/unsupported sandbox
adapters fail closed. The plugin adapter redacts echoed values and common JSON,
URL and base64 representations before schema validation, model output, SSE or
Spill storage, then releases the private shell channel.

This is a trusted-plugin boundary, **not** a sandbox for adversarial plugins.
A trusted handler receives the secret and must not persist it to output files,
encode it to evade redaction, or send it to an unrelated endpoint. Processes in
the same Docker sandbox are not mutually isolated secret tenants. Effect
declarations are policy inputs, not OS network filtering: arbitrary shell/code
execution and legacy MCP discovery retain their prior isolation/security model.
No third-party untrusted package loading or automatic secret migration is added.

## API, SSE and UI

- `GET/POST /api/v1/credentials`
- `POST /api/v1/credentials/{reference}/revoke`
- `GET /api/v1/sessions/{session_id}/tool-approvals`
- `GET /api/v1/sessions/{session_id}/tool-approvals/{approval_id}`
- `POST /api/v1/sessions/{session_id}/tool-approvals/{approval_id}/decision`

Credential mutations require `X-Credential-Action: create` or `revoke`; approval
decisions require `X-Tool-Approval-Action: decide` and an expected revision.
Ownership and session access are checked independently of the headers. A share
viewer cannot approve or revoke anything. API errors and logs use bounded
classifications and do not echo secret-form inputs or private provider errors.

The unchanged `tool` SSE event adds optional `tool_approval`. Approval revision
is independent of the session event `seq/version`. Old recordings without the
field retain their canonical checksums. Conversation tool rows and their detail
panels display the request, sanitized parameters, permissions, expiry and
**Approve once / Reject**. Polling reconciles missed SSE updates; old responses
cannot reopen terminal approvals, and share pages never poll or offer decisions.

The credential panel is at `/plugins?tab=credentials`. It only offers tool slots
actually declared by the current catalog. The currently bundled local analysis
tools do not require credentials, so an empty slot selector is expected until a
trusted integration declares them. There is no need to save the model key here.

## Verification and rollout

Backend coverage includes exact-call grants, single consumption, owner/session
isolation, rejection/expiry, frozen inputs, credential revocation, encrypted
storage, fixed-error timeouts, API intent/revision checks, private execution
transport, redacted output and SSE/recording compatibility. Sandbox tests verify
request masking and per-child environment isolation. Frontend tests cover
revision reconciliation, safe credential forms and read-only sharing.

Build backend, frontend and sandbox together with `./run.sh build backend
frontend sandbox-image`, then update the existing services with `./run.sh up -d
--no-build --no-deps backend frontend`. This changes the sandbox execution bundle
digest. Existing sandbox containers are preserved, not destructively upgraded;
start a new analysis session to adopt the new image. There is still one local
frontend at `127.0.0.1:7001` for this deployment.
