# AI-DataSeek 前端

AI-DataSeek 的 Vue 3、TypeScript、Vite、Tailwind 前端。

## 保留页面

- `/dataset/seek/:datasetId`：科学数据探查与分析。
- `/chat`、`/chat/:sessionId`：通用数据分析探查任务。
- `/chat/plugins`：Skill、MCP、Renderer 插件。
- `/chat/admin`：仅资源用量、任务、MCP、技能四项管理。

## 开发与部署

```bash
cd ..
./run.sh up -d --build
```

前端随根目录唯一的 Compose 服务构建和部署，通过 `http://39.106.98.67:7000` 访问。

## 检查

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

完整部署请使用仓库根目录的 `docker-compose.yml`。
