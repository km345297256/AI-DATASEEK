# AI-DataSeek 后端

这是数据集探查、沙箱分析、插件及精简系统管理能力的 FastAPI 后端。

## 保留的接口域

- 数据集临时提交、递归目录检查、预览及只读沙箱挂载。
- 分析会话、事件、文件、分享和 Sandbox/VNC。
- Skill、MCP、Renderer 插件。
- 资源用量、任务回放、MCP 和技能管理。
- 上述链路依赖的 Agent 配置、安全审核、权限、审计和 Token 用量统计。

OpenClaw、A2A、知识库/Qdrant、科学站点发布和客户端浏览器连接域已移除。

## 访问模型

后端固定为免登录模式，不要求也不解析 Bearer Token 或 `X-API-Key`。
浏览器与 API 调用全部使用同一个系统内置管理员身份。系统保留 Token 用量
记录以供运维报表使用，但不校验用户额度，也不扣减用户余额。

该模式仅面向可信网络部署。如需将服务暴露到可信网络之外，必须在网络边界
通过 VPN、防火墙 IP 白名单、带认证的反向代理或 API 网关限制访问，并启用
HTTPS。任何能够访问后端的调用者都拥有系统管理员权限。

数据集宿主机路径仍必须解析到 `DATASET_HOST_PATH_ALLOWLIST` 范围内，并以
只读方式挂载到分析沙箱。真实路径不得返回浏览器，也不得持久化到 URL 或
浏览器存储中。

## 本地运行

```bash
uv sync
API_KEY=test uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

需要 MongoDB 和 Redis。完整运行方式以仓库根目录的 `docker-compose.yml` 为准。

## 测试

```bash
uv run pytest
```

部分 API 集成测试要求 `http://localhost:8000` 已有后端服务。
