# AI-DataSeek

[English](README.md) | 中文

AI-DataSeek 是从 AI-MANUS 中抽取出的数据集智能探查与分析系统。新项目保留了从数据集提交、目录递归识别、只读挂载到 Agent 分析的完整链路，并移除了无关产品功能。

## 保留能力

- `/dataset/setup` 临时数据集测试入口与第三方提交接口。
- `/dataset/seek/:datasetId` 科学数据探查：递归展示文件、模型生成分析与可视化问题、只读挂载数据目录。
- 通用数据分析探查任务：`/chat` 与 `/chat/:sessionId`。
- Skill、MCP、Renderer 三类插件及数据成果预览。
- 独立的数据集管理入口，不计入系统管理菜单。
- 系统管理仅保留：资源用量、任务管理、MCP 管理和技能管理。
- 会话历史、分析文件和沙箱桌面。
- 用于运维和容量分析的 Token 用量统计，不设置用户额度。

为保证这些功能可真实运行，Agent 执行、会话事件、安全审核、审计、权限、文件存储、MongoDB、Redis 和 Sandbox 等传递依赖也会保留。详细边界见 [架构范围说明](docs/architecture-scope.md)。

## 运行拓扑

```text
浏览器 -> Frontend -> Backend -> MongoDB / Redis / MinIO
                              \
                               -> 每任务独立 Docker Sandbox
                                    -> 数据集服务器目录（只读）
```

稳定联测环境默认使用 `7000` 端口；隔离的开发环境使用 `7100` 端口。

## 快速启动

要求：Docker 20.10+、Docker Compose。

```bash
cp .env.example .env
# 修改 API_KEY、API_BASE、MODEL_NAME 以及 MINIO_SECRET_KEY。
./run.sh up -d --build
```

访问 `http://localhost:7000`。

AI-DataSeek 固定采用免登录模式。浏览器和 API 请求均不使用 Bearer Token
或 `X-API-Key`，所有调用者统一以系统内置管理员身份操作。Token 消耗只用于
用量报表，不校验用户额度，也不从用户余额中扣减。

如果宿主机使用 Snap 版 Docker，需要配置：

```env
DATASET_DOCKER_HOST_ROOT=/var/lib/snapd/hostfs
```

`DATASET_HOST_PATH_ALLOWLIST` 控制允许访问的数据根目录。系统会先校验第三方提交的服务器目录，再以只读方式挂载到分析沙箱。

## 开发环境

```bash
cp .env.example .env
./dev.sh up -d --build
```

默认开发端口：

- 前端（开发入口）：`http://localhost:7100`
- 后端：`http://localhost:8001`
- MongoDB：`localhost:27018`
- MinIO API/控制台：`localhost:9010` / `localhost:9011`

常用检查：

```bash
cd frontend && npm ci && npm run type-check && npm run build
cd ../backend && uv sync && uv run pytest
cd ../sandbox && uv sync && uv run pytest
docker compose config --quiet
```

## 第三方接入

`/dataset/setup` 页面仅用于模拟第三方系统调用。正式业务系统应直接调用临时数据集提交接口，并将用户跳转至 `/dataset/seek/:datasetId`，详见 [第三方接入接口文档](docs/dataset-seek-third-party-api.md)。

## 项目结构

```text
AI-DataSeek/
├── frontend/   Vue 3 数据探查界面
├── backend/    FastAPI、Agent、插件与系统管理接口
├── sandbox/    隔离的科学数据分析环境
├── docs/       接口与架构文档
└── docker-compose.yml
```

## 安全说明

- AI-DataSeek 不提供应用层调用者认证，只能部署在可信网络中。
- 如需跨出可信网络或接入公网，必须先通过 VPN、防火墙 IP 白名单、带认证的反向代理或 API 网关实施网络层访问控制，并启用 HTTPS。
- 任何能够访问服务的调用者都拥有共享系统管理员权限，系统不提供按调用者隔离或审计归属。
- 模型密钥和真实服务器路径不得出现在 URL 或浏览器存储中。
- `DATASET_HOST_PATH_ALLOWLIST` 应只配置专用数据目录。
- 数据集宿主机路径仍须通过该白名单校验，并固定以只读方式挂载到分析沙箱；真实路径不得返回浏览器，也不得持久化到 URL、`localStorage` 或 `sessionStorage`。
- Backend 挂载 Docker Socket 后可管理本机容器，只能部署在可信主机。
- 生产部署前必须替换 `.env.example` 中的全部示例密钥。

## 许可证

见 [LICENSE](LICENSE)。
