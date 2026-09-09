# AI-DataSeek Cordis plugin host

This process owns the plugin catalog and plugin lifecycle. Tool execution stays
inside AI-DataSeek's existing Python AgentLoop and sandbox boundary; the host
does not expose an HTTP port or execute tool handlers.

## Build and run

Node.js 22.19 or newer is required.

```bash
npm ci
npm run build
node dist/index.js \
  --tools-dir ../tools \
  --execution-contract-dir ../sandbox
```

The host reads one JSON-RPC 2.0 request per line from stdin and writes one
response per line to stdout. Logs are written only to stderr. Supported methods
take no parameters:

- `host.health`
- `catalog.snapshot`
- `plugins.reload`
- `visualizations.snapshot`
- `visualizations.reload`
- `shutdown`

For example:

```json
{"jsonrpc":"2.0","id":1,"method":"catalog.snapshot","params":{}}
```

`plugins.reload` validates the complete `tools/*/manifest.json` set, its handler
files, and the sandbox source contract before building a new Cordis context.
A failed reload returns a JSON-RPC error and keeps the last valid catalog
generation active. The catalog includes manifest and execution-bundle digests
which the sandbox verifies again before executing a handler.

## Tool Contract v2

Every tool returned by `catalog.snapshot` is normalized to Tool Contract v2.
The existing `parameters`, `scopes`, and `timeout_seconds` fields remain in the
snapshot so the current Agent adapter continues to work. A manifest can add:

```json
{
  "name": "example_report",
  "description": "Produce a bounded tabular report.",
  "parameters": { "type": "object", "properties": {} },
  "output_schema": {
    "type": "object",
    "properties": { "rows": { "type": "array" } },
    "required": ["rows"]
  },
  "execution": {
    "timeout_seconds": 60,
    "cancellable": true,
    "concurrency": "parallel",
    "effects": ["sandbox_read", "network"],
    "permissions": ["dataset:read", "service:example"]
  },
  "presentation": {
    "kind": "table",
    "title": "Example report"
  }
}
```

`output_schema` describes the normalized `ToolResult.data` value, not raw
process stdout and not the outer result envelope. Missing or explicit `null`
means legacy/untyped output; it must not be treated as successful output
validation. Current manifests remain valid and receive conservative defaults:

- `output_schema: null`
- `execution.timeout_seconds`: the legacy `timeout_seconds`, or 90
- `execution.cancellable: false`
- `execution.concurrency: "exclusive"`
- `execution.effects: ["sandbox_read", "sandbox_write"]`
- `execution.permissions: []`
- `presentation.kind: "auto"`

Supported explicit effects are `sandbox_read`, `sandbox_write`, `network`,
`credential_use`, and `external_side_effect`. Unknown effects, presentation
kinds, nested fields, duplicate entries, invalid schemas, and conflicting
legacy/nested timeouts fail the complete catalog reload. Presentation metadata
is a static hint only: runtime data and URLs must be carried by the sanitized
backend event DTO, never embedded in a manifest.

Timeouts are bounded to 1–120 seconds. `exclusive` means calls to the same
plugin/tool identity are serialized within one backend process; `parallel`
opts out of that lock. The execution interceptor includes both time spent
waiting for that lock and time spent running the tool in the timeout budget.

## Checks

```bash
npm test
```
## Visualization Contract v1

The separate Cordis visualization catalog reads `visualizations/*.json` and
exposes `visualizations.snapshot` / `visualizations.reload` over the existing
stdio RPC transport. Its Context, fibers, revision and atomic reload are
independent of Agent tool catalogs and execution-bundle digests. Manifests are
data-only: trusted adapter/reader enums, explicit file matchers, read permission
and bounded budgets; arbitrary entries, scripts and URLs are rejected.

User enable/disable preferences belong to the authenticated user in the backend,
not the shared Cordis fiber. Disabling one user's view cannot dispose another
user's shared catalog registration. Frontend mounts and authorized reads enforce
the effective user state. See [the contributor contract](../docs/visualization-plugin-contract.md)
for extension, lifecycle, migration and compatibility details.
