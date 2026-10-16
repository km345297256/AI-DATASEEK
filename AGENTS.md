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
`ai-dataseek`. Unless the user explicitly requests otherwise, publish the frontend
on server port `8888` and use `https://dataseek.4fair.cn/` as the public access URL.
The direct server endpoint is `http://39.106.98.67:8888`. Use `./run.sh` for all
container updates. Do not recreate a separate development stack or expose a
second frontend port. Before publishing, verify that the resolved frontend port
is `8888` and the public URL uses the HTTPS domain.

On the public server, publish from `/data2/current` using
`./run.sh production --apply` (add `--services frontend` for a frontend-only
update; use `--apply --stack` only to start the migrated full stack with all
seven images pinned in the release manifest). Retired `/root` deployment entries
reject publication and container changes. Use `./run.sh production --stop` for
controlled shutdown before a data migration; it archives running dynamic
sandboxes before stopping them and preserves fixed-service containers and volumes.
Persistent production volumes bind only `/data2/state/`
(or its Snap hostfs view). Run `./run.sh production` for a read-only preflight. This entry point
locks deployments, checks the release image manifest and endpoint contract,
and verifies the running service. Do not bypass it with direct Docker container
creation or unguarded Compose updates on the public server. Ordinary `run.sh`
container changes are blocked in production release directories; configuration
and status queries remain available. See
`deploy/production/README.md` for the narrowly scoped unmanaged-frontend repair.

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
