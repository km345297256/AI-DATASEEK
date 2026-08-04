# AI-DataSeek Backend

FastAPI backend for dataset exploration, sandboxed analysis, plugins, and the reduced administration surface.

## Retained API domains

- Dataset submission, recursive inspection, preview, and read-only sandbox mounting.
- Analysis sessions, events, files, sharing, and sandbox/VNC access.
- Skill, MCP, and Renderer plugins.
- Resource usage, task replay, MCP, and skill administration.
- Agent profiles, safety, permissions, audit, and token usage accounting required by those flows.

The OpenClaw, A2A, knowledge-base/Qdrant, scientific-site publishing, and client-browser connector domains are intentionally absent.

## Access model

The backend is permanently login-free. It does not require or interpret Bearer
tokens or `X-API-Key` credentials; all browser and API calls use the same
built-in system-administrator identity. Token usage records are retained for
operations reporting, but no user quota is checked or decremented.

This model is intended for trusted-network deployments. Before exposing the
service outside a trusted network, enforce access at the network edge with a
VPN, firewall allowlist, or authenticated reverse proxy/API gateway, and use
HTTPS. Every caller that can reach the backend has system-administrator
authority.

Dataset host paths must still resolve beneath `DATASET_HOST_PATH_ALLOWLIST`.
They are mounted read-only into analysis sandboxes, and real paths must never be
returned to the browser or persisted in URLs or browser storage.

## Local run

```bash
uv sync
API_KEY=test uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

MongoDB and Redis must be available. For the supported full stack, use the repository's `docker-compose.yml`.

## Tests

```bash
uv run pytest
```

Some API integration tests expect a running backend at `http://localhost:8000`.
