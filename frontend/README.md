# AI-DataSeek Frontend

Vue 3, TypeScript, Vite, and Tailwind frontend for AI-DataSeek.

## Retained pages

- `/dataset/seek/:datasetId` — scientific dataset exploration and analysis.
- `/chat` and `/chat/:sessionId` — general data exploration tasks.
- `/chat/plugins` — Skill, MCP, and Renderer plugins.
- `/chat/admin` — resource usage, tasks, MCP, and skills only.

## Development and deployment

```bash
cd ..
./run.sh up -d --build
```

The frontend is built and deployed with the repository's sole Compose stack and is available at `http://39.106.98.67:7000`.

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
