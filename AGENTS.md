# AI-DataSeek contributor guide

AI-DataSeek is intentionally scoped to dataset exploration and analysis. Do not
reintroduce removed product domains (Claw, A2A, scientific-site publishing,
knowledge-base indexing, or broad platform administration) without an explicit
architecture decision.

## Git workflow

Unless the user explicitly specifies another branch, use `v2` as the default
branch for commits and pushes in this repository. Do not commit directly to
`main` by default. This branch preference does not authorize automatic commits
or pushes; perform those actions only when the user requests them.

## Services

| Service | Stack | Purpose |
|---|---|---|
| Frontend | Vue 3 + TypeScript + Vite | Dataset and general analysis UI |
| Backend | FastAPI + Beanie + Redis | APIs, Agent execution, plugins, administration |
| Sandbox | FastAPI + Chromium + VNC | Isolated analysis runtime |

## Deployment

AI-DataSeek has one supported Compose stack: `docker-compose.yml`, project
`ai-dataseek`, with the frontend exposed at `http://39.106.98.67:7000`. Use
`./run.sh` for all container updates. Do not recreate a separate development
stack or expose a second frontend port.

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
