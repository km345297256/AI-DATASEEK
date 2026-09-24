# Opt-in MCP image results

MCP image input is disabled by default. Set `MCP_IMAGE_RESULTS_ENABLED=true`,
keep private spill storage enabled, and declare `vision: true` for the exact
active provider/model in `MODEL_REQUEST_CAPABILITIES`. No provider or model is
automatically switched. A text-only or unlisted route receives an explicit
omission notice; images refused at initial admission are not stored for a later
model change. Existing text-only MCP results and UI rendering remain unchanged.

The server validates strict base64, declared MIME against decoded format,
source-byte/pixel/count limits and frame count. It reuses the existing Vision
model projection (orientation correction, bounded dimensions, metadata removal,
PNG encoding). Multi-frame content is explicitly refused rather than selecting a
scientific page silently. It never fetches URLs/resources and does not add audio.

Accepted PNG copies use the existing private spill store. Opaque locators map to
storage objects owned by a keyed internal principal, not a browser user's file
account. The same session ownership checks, integrity checks, deletion,
retention and cleanup apply to text and images. No new storage collection is
introduced. Raw binary/base64 is absent from public tool results, events and
persisted Agent memory.

Processing order is image validation/reference preparation, post-execution
policy, combined text/image budgeting and spill, then durable memory projection.
`MCP_IMAGE_RESULT_MAX_TOKENS` defaults to 12,500 per tool result. Ordered image
slots are indivisible; the retained head/tail may contain no images. Complete
accepted text and image recovery locators are spilled without embedding image
bytes. A model can use `spill_artifact_read` for that manifest and the governed
`dataseek_mcp_image_read` tool for an individual image. Storage failure is explicit; it
does not retry an already completed MCP operation or claim complete recovery.

Immediately before **each physical model request**, only references whose exact
text slots survived policy are expanded in a deep copy. Reads recheck the
current owner/session, expiry, backing metadata and SHA-256, including after the
read to catch revocation. The actual model identity and current per-request
Vision limits govern expansion. Older images may remain as recovery notices
while newer images fit; this is not a cumulative task image quota. Cancellation
does not change persistent references. Missing/revoked images never fall back to
cached inline bytes, and policy-removed images cannot be resurrected.

The installed DeepSeek adapter serializes list-valued tool content as JSON
text. Its request-only compatibility projection therefore moves admitted tool
pixels into an explicitly untrusted image observation after the complete tool
batch, before request sizing/accounting. Tool IDs, order, text and error status
remain intact; each image is counted once. The internal observation and its
owning batch are compacted together, while real user messages cannot opt into
this internal type. Neither the carrier nor inline image bytes are persisted.
Other providers and ordinary text-only tool results keep their existing path.

MCP pixels remain untrusted observations. Their storage/visibility does not
authorize execution, change the MCP `isError` outcome or certify scientific
correctness. No dataset mount, host-path allowance, frontend/API/SSE schema or
deployment configuration is changed.
