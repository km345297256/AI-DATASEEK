# AI-DataSeek Frontend

Vue 3, TypeScript, Vite, and Tailwind frontend for AI-DataSeek.

## Retained pages

- `/dataset/setup` — temporary third-party submission simulator.
- `/dataset/seek/:datasetId` — scientific dataset exploration and analysis.
- `/chat` and `/chat/:sessionId` — general data exploration tasks.
- `/chat/plugins` — Skill, MCP, and Renderer plugins.
- `/chat/datasets` — dataset administration.
- `/chat/admin` — resource usage, tasks, MCP, skills, and users only.

## Development

```bash
npm ci
BACKEND_URL=http://127.0.0.1:8000 npm run dev
```

The development server defaults to port `5173` when run directly. The root development Compose file maps AI-DataSeek to port `5174` to avoid conflicts with the original project.

## Checks

```bash
npm run type-check
npm run build
node --test tests/*.test.mjs
```

## Docker

```bash
docker build -t ai-dataseek-frontend .
docker run --rm -p 7000:80 -e BACKEND_URL=http://backend:8000 ai-dataseek-frontend
```

For a complete deployment, use the root `docker-compose.yml`.
