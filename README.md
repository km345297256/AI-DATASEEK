# AI-DataSeek

English | [中文](README_zh.md)

AI-DataSeek is a focused AI dataset exploration and analysis platform extracted from AI-MANUS. It keeps the complete dataset-to-sandbox analysis path while removing unrelated product areas.

## Included capabilities

- Temporary dataset setup and third-party submission API.
- Scientific dataset exploration at `/dataset/seek/:datasetId`, with recursive file discovery, model-generated analysis questions, and read-only dataset mounts.
- General data-exploration tasks (`/chat` and `/chat/:sessionId`).
- Skill, MCP, and Renderer plugins.
- Dataset management outside the system-administration area.
- System administration limited to resource usage, tasks, MCP, and skills.
- Per-session Docker sandboxes, task history, file results, and previews.
- Token usage accounting for operations and capacity analysis, without per-user quotas.

The transitive runtime modules required by these capabilities are intentionally retained: Agent execution, session events, safety review, auditing, permissions, file storage, MongoDB, Redis, and the sandbox runtime. See [docs/architecture-scope.md](docs/architecture-scope.md) for the exact boundary.

## Runtime topology

```text
Browser -> Frontend -> Backend -> MongoDB / Redis / MinIO
                            \
                             -> per-session Docker sandbox
                                  -> dataset directory (read-only)
```

The stable integration stack uses port `7000`; the isolated development stack uses port `7100`.

## Quick start

Requirements: Docker 20.10+ and Docker Compose.

```bash
cp .env.example .env
# Set API_KEY, API_BASE, MODEL_NAME, and a strong MINIO_SECRET_KEY.
./run.sh up -d --build
```

Open `http://localhost:7000`.

AI-DataSeek has a fixed no-login access model. Browser and API requests do not
use Bearer tokens or `X-API-Key`; every caller operates as the same built-in
system administrator. Token consumption is recorded for usage reporting only
and never checked against or deducted from a user quota.

For a Snap-packaged Docker daemon, expose host dataset paths through:

```env
DATASET_DOCKER_HOST_ROOT=/var/lib/snapd/hostfs
```

Allowed dataset roots are controlled by `DATASET_HOST_PATH_ALLOWLIST`. Submitted server directories are validated and mounted read-only into the task sandbox.

## Development

```bash
cp .env.example .env
./dev.sh up -d --build
```

Development defaults:

- Frontend (development endpoint): `http://localhost:7100`
- Backend: `http://localhost:8001`
- MongoDB: `localhost:27018`
- MinIO API/console: `localhost:9010` / `localhost:9011`

Useful checks:

```bash
cd frontend && npm ci && npm run type-check && npm run build
cd ../backend && uv sync && uv run pytest
cd ../sandbox && uv sync && uv run pytest
docker compose config --quiet
```

## Third-party integration

The `/dataset/setup` page only simulates a third-party system. Production integrations should call the temporary dataset submission API directly and redirect users to `/dataset/seek/:datasetId`. See [the third-party API guide](docs/dataset-seek-third-party-api.md).

## Project layout

```text
AI-DataSeek/
├── frontend/   Vue 3 data exploration UI
├── backend/    FastAPI APIs, Agent execution, plugins, and administration
├── sandbox/    isolated scientific analysis environment
├── docs/       integration and architecture documentation
└── docker-compose.yml
```

## Security notes

- AI-DataSeek performs no application-level caller authentication. Deploy it only on a trusted network.
- Before exposing it beyond a trusted network, put it behind network-level access control such as a VPN, firewall allowlist, or authenticated reverse proxy/API gateway, and terminate HTTPS there.
- Anyone who can reach the service has the shared system-administrator authority; there is no per-caller isolation or attribution.
- Never put model keys or real host paths in URLs or browser storage.
- Restrict `DATASET_HOST_PATH_ALLOWLIST` to dedicated dataset roots.
- Dataset host paths remain validated against that allowlist and are mounted into analysis sandboxes read-only; real paths must not be returned to the browser or persisted in URL, `localStorage`, or `sessionStorage`.
- Mounting the Docker socket grants the backend control over local containers; deploy it only on a trusted host.
- Replace every placeholder secret in `.env.example` before production use.

## License

See [LICENSE](LICENSE).
