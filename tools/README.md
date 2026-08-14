# AI-DataSeek Tool plugins

Each direct child directory is one trusted Tool plugin and contains:

- `manifest.json`: plugin metadata and one or more Tool JSON schemas.
- `handler.py`: a `build_command(tool_name, arguments)` function that returns an argv list.

The backend discovers manifests at startup and exposes their schemas to the Agent. The sandbox runs the same installed plugin through `ai-dataseek-tool`; arbitrary paths and unregistered names are rejected.

To add a Tool, add or update one plugin directory. Do not add another wrapper to `ShellToolkit` or another Tool name to the execution-agent allowlist.
