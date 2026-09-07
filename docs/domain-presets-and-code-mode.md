# Domain presets and experimental execution modes

Domain presets narrow the existing Cordis tool catalog for a task. They do not
install plugins, grant permissions, bypass per-call approval, or create a second
Agent management system. Profile selection continues to use the existing
DataSeek Agent Profile selector.

## Configuration contract

An Agent Profile carries one credential-free `tool_runtime` object:

```json
{
  "preset_id": "geoscience",
  "selection_mode": "on_demand",
  "code_mode_enabled": false,
  "domain_subagents_enabled": false
}
```

- `preset_id` selects one reviewed domain scope. The current IDs are `general`,
  `tabular`, `geoscience`, `image_science`, `chemistry`, `sequence`, `space`,
  `documents`, and `spectroscopy`.
- `selection_mode: "on_demand"` begins with the preset's small initial tool set
  and may load more tools only inside that same preset. `"all"` exposes every
  available tool in the selected preset, not every tool in the full catalog.
- `code_mode_enabled` and `domain_subagents_enabled` are independent,
  experimental opt-ins. Both remain off unless a deployment default or an
  explicitly selected profile enables them.

Existing saved profiles that have no `tool_runtime` retain legacy behavior:
`general`, `all`, and both experiments disabled. A task with no selected profile
uses the deployment settings `TOOL_PRESET_ID` (default `general`),
`TOOL_SELECTION_MODE` (default `on_demand`), `CODE_MODE_ENABLED` (default
`false`), and `DOMAIN_SUBAGENTS_ENABLED` (default `false`). Loading the UI does
not migrate or save an existing profile.

An execution SubAgent may additionally carry a nullable `preset_id`. A non-null
domain preset is rejected for other handler types. Enabling the profile-level
Domain SubAgent pilot lets the existing PlanActFlow schedule the supported
preset-specific execution SubAgent; it does not introduce a nested Agent loop.

## API and UI

`GET /api/v1/plugins/presets` returns public, read-only preset metadata, the
no-profile defaults, and the current Cordis catalog revision. Each item reports
its plugin IDs, initial tool names, and the initial/available counts for that
specific catalog snapshot. It contains no tool arguments, schemas, credentials,
or host paths.

The existing Plugins page exposes this catalog through the **Domain Presets**
tab. Its **Configure Agent / Enable pilots** button opens the existing profile
editor directly. A navigational shortcut is
`http://localhost:7001/plugins?tab=presets&settings=agent-profiles`; the transient
`settings` query is consumed after opening and never changes a saved profile.
The catalog itself does not edit the runtime catalog.

The existing Settings dialog exposes an **Agent Profiles** tab. Selecting a
saved profile allows an administrator to update only its `tool_runtime` object
through `PUT /api/v1/agent-profiles/{profile_id}`. Model configuration, prompts,
and explicit SubAgent entries are not resent by this editor. Changes apply to
new tasks that select the profile; an already-created session is not silently
retargeted.

When no profile exists, the page shows the no-profile defaults and does not
create anything automatically. The explicit **New profile from current model**
action calls `POST /api/v1/agent-profiles/runtime-preset` with only a name and
`tool_runtime`. The backend copies its current non-secret model name, provider,
temperature, and output limit. It does not copy `api_key` or `api_base` into the
profile. After creation, the user selects that profile from the Agent selector
in a new chat, or clicks **Use this profile in a new chat** in the editor. This
explicit action selects the saved profile and navigates to the new-chat page;
it sends no message and creates no session until the user submits one. The
selector also has a **Manage Agent profiles** shortcut. Experiment switches
appear before optional preset/default details so they are easy to find.

## On-demand selection and provenance

The complete catalog stays pinned to the task's Cordis snapshot. A task-local
`PluginToolView` exposes only the preset's initial 3–5 plugin schemas, plus the
existing core toolkits. `tool_catalog_search` returns bounded public metadata;
`tool_catalog_load` atomically loads at most eight schemas per call, up to 48
loaded plugin tools per view. Loading outside the preset or past the limit fails
without a partial load. Loading never enables a plugin globally. A reset or
disabled view also invalidates previously obtained tool handles.

Every new user task resets its views to their initial selection. The execution
snapshot records the preset, selection mode, initial/available counts, selection
digest, experiment flags, and domain-agent count. Tool-load events use existing
SSE ToolEvents; subsequent ModelDriver requests bind the updated schemas. The
snapshot describes the initial environment, not a mutable final selection.
Old snapshots omit the optional selection field and retain their fingerprint.

The real-Cordis regression test compares the complete normalized model schemas:
the no-profile initial schema token estimate is less than one third of legacy
`general/all`. This is a deterministic estimate, not provider billing or a
guaranteed reduction in the total tokens needed to finish a task. Discovery
itself can require additional model turns.

## Code Mode execution limits

This pilot is a small, Python-shaped AST interpreter, **not Python execution**.
It uses neither `eval` nor `exec`, creates no host shell or network client, and
cannot import modules, loop, recurse, use attributes, define functions, catch
exceptions, or discover/delegate/load tools from inside a program. The existing
Agent invokes `code_mode_run` as an ordinary tool; no additional AgentLoop is
introduced. There is no separate interactive code editor in this pilot.

Only these explicitly reviewed Contract v2 tools currently opt in:

- `data_format_inspect`
- `hierarchical_store_inspect`
- `cf_semantics_validate`
- `workbook_inspect`
- `table_profile`

Each declaration requires both `code_mode` and `dataset_fast_path` scopes,
exactly `sandbox_read` effects, no permission requirements, and no credentials.
The tool must also belong to the selected preset and already be loaded. Other
plugin tools and all shell, browser, MCP, file, discovery, and delegation tools
remain unavailable to Code Mode. Extending this list requires reviewing the
actual implementation, updating its manifest, and rebuilding the matching
backend and sandbox execution bundle; this is not an automatic catalog opt-in.

After loading `table_profile` through ordinary tool discovery, an example is:

```python
profile = await tools.call("table_profile", {"input_path": "/home/ubuntu/datasets/example/table.csv"})
{"profile": profile["data"]}
```

Use the active tool schema and the dataset's actual sandbox reference. Host
absolute paths, traversal, Windows paths, and file URIs are rejected. Absolute
paths are confined to `/home/ubuntu/datasets`, `/home/ubuntu/upload`, and
`/home/ubuntu/output`; the underlying sandbox still enforces its existing path
validation and read-only dataset mounts.

Default bounds are 12,000 UTF-8 source bytes, 512 AST nodes, 32 statements,
16 sequential tool calls, and 120 seconds for the whole program. Each arguments
object/intermediate public result/final output is limited to 256,000 JSON bytes;
the final result envelope is limited to 512,000 bytes. Values are also bounded
to depth 16 and 10,000 total container entries per checked value. Only JSON values and string-keyed
dictionary lookup are supported.

The complete program is statically checked before its first call. Every inner
call then resolves the current tool view and invokes the same governed tool
wrapper, including policy, authorization, AnalysisJob, timeout, audit, and Spill
processing. Only the sanitized public result projection is handed to the next
statement. The final result includes ordered inner call IDs, allowing it to be
correlated with ordinary job/audit records. Code Mode does not grant permissions
or bypass approvals. The pilot's five read-only tools need no credentials.

Failure stops the sequence; Code Mode errors are non-retryable at the tool
transport layer, and cancellation/budget-stop signals propagate unchanged.
This is **not a transaction**: runtime failure (including a later eligible but
unloaded tool) can occur after earlier read-only calls have completed. Static
validation cannot guarantee that every result lookup or external tool succeeds.

Code Mode ToolEvents carry only the source byte count and a keyed HMAC, not the
program text. Normal private Agent memory still retains model tool calls under
the existing retention rules. This metadata is an audit identity, not a full
source recording or an executable replay format.

## Domain SubAgent pilot

Enabling Domain SubAgents creates bounded `ExecutionAgent` instances inside the
existing PlanActFlow scheduler. `general` adds tabular and geoscience specialists;
a domain-specific preset adds its specialist. Existing explicit execution
SubAgent configurations can select other presets, with at most four active
domain agents. Each has isolated memory, a private on-demand view, a safe model
trace identity, and at most four execution iterations. The parent retains the
normal plan update and final summary; domain outputs use ordinary step results.

Specialists share the parent task's ContextVar ModelDriver token/call budget,
Docker sandbox, dataset context, and governed plugin wrappers. They receive
dataset-catalog and Spill tools but no shell, browser, file toolkit, MCP, skill
creation, user-question, Code Mode, or nested delegation tools. They can create
analysis artifacts through their domain plugins where the existing policy
permits it; they are not globally read-only. Enabling this pilot sends dataset
questions through the existing planner rather than the direct dataset fast path
so that it can select specialist steps.

There are no Agent Teams, self-modifying plugins, model-generated workflows,
recursive task delegation, or replacement communication protocols.

## Verification and rollback

After updating the local service, open `http://localhost:7001/plugins?tab=presets`
to check the nine presets, initial counts, and catalog revision. Use Settings →
Agent Profiles to explicitly save a trial profile, then select it in a **new
chat**. Existing saved profiles and conversations are not rewritten. In a
tabular/on-demand chat, ask to inspect a supplied table and check for catalog
search/load followed by ordinary tool calls. With Code Mode explicitly enabled,
ask to compose the reviewed read-only tools; with Domain SubAgents enabled,
inspect the existing plan's specialist steps.

For a profile-level rollback, choose `general/all` and disable both experiments.
For no-profile defaults, set `TOOL_SELECTION_MODE=all`, `TOOL_PRESET_ID=general`,
`CODE_MODE_ENABLED=false`, and `DOMAIN_SUBAGENTS_ENABLED=false`, then update the
existing Compose stack through `./run.sh`. This does not remove tools, artifacts,
session history, or prior-stage governance. Do not disable execution-bundle
verification to make an old sandbox accept a new plugin catalog.

An already-paused sandbox is not updated when its image tag is rebuilt. If it
contains the previous manifests, its existing session will reject new plugin
calls with a catalog/execution-bundle mismatch. Keep that container and its
files intact and use a new session with the rebuilt image. Reusing an old
session requires an explicitly planned environment migration after preserving
unpersisted artifacts; normal session-file recovery does not guarantee recovery
of every temporary file. This release does not automatically delete or rebuild
old user sandboxes as a migration step. The existing node monitor's configured
idle-sandbox reclamation policy still applies independently of this release;
this guidance does not disable that policy or guarantee indefinite retention of
temporary sandbox files. Persisted session artifacts remain in their file store.

Regression coverage lives in `test_domain_presets.py`, `test_tool_runtime_flow.py`,
`test_tool_runtime_api.py`, `test_code_mode.py`, and `test_code_mode_flow.py`, along
with the existing ModelDriver, authorization, AnalysisJob, Spill, snapshot, SSE,
Cordis catalog, frontend, and sandbox suites.
