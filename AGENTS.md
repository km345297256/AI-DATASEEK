# AI-DataSeek contributor guide

AI-DataSeek is intentionally scoped to dataset exploration and analysis. Do not
reintroduce removed product domains (Claw, A2A, scientific-site publishing,
knowledge-base indexing, or broad platform administration) without an explicit
architecture decision.

## Services

| Service | Stack | Purpose |
|---|---|---|
| Frontend | Vue 3 + TypeScript + Vite | Dataset and general analysis UI |
| Backend | FastAPI + Beanie + Redis | APIs, Agent execution, plugins, administration |
| Sandbox | FastAPI + Chromium + VNC | Isolated analysis runtime |

## Required checks

```bash
cd frontend && npm run type-check && npm run build
cd ../backend && uv run pytest
cd ../sandbox && uv run pytest
docker compose config --quiet
```

Preserve the read-only dataset mount boundary. Host paths must remain validated
against `DATASET_HOST_PATH_ALLOWLIST`, and real paths must never be returned to
the browser or persisted in URL/localStorage/sessionStorage.
