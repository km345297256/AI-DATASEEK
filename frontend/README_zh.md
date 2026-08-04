# AI-DataSeek 前端

AI-DataSeek 的 Vue 3、TypeScript、Vite、Tailwind 前端。

## 保留页面

- `/dataset/setup`：第三方临时提交模拟页面。
- `/dataset/seek/:datasetId`：科学数据探查与分析。
- `/chat`、`/chat/:sessionId`：通用数据分析探查任务。
- `/chat/plugins`：Skill、MCP、Renderer 插件。
- `/chat/datasets`：数据集管理。
- `/chat/admin`：仅资源用量、任务、MCP、技能、用户五项管理。

## 开发

```bash
npm ci
BACKEND_URL=http://127.0.0.1:8000 npm run dev
```

直接运行时开发服务器默认使用 `5173`。根目录开发 Compose 使用 `5174`，避免与原项目冲突。

## 检查

```bash
npm run type-check
npm run build
node --test tests/*.test.mjs
```

## Docker

```bash
docker build -t ai-dataseek-frontend .
docker run --rm -p 7100:80 -e BACKEND_URL=http://backend:8000 ai-dataseek-frontend
```

完整部署请使用仓库根目录的 `docker-compose.yml`。
