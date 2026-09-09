# ONLYOFFICE 本机只读阅读

本项目通过现有 Cordis 协议 2 插件 `viz-onlyoffice` 提供办公文档阅读。它不是独立插件协议，不改变 AgentLoop、SSE、分析任务或文件下载流程。既有 Word、Excel、PowerPoint、PDF 预览保留；ONLYOFFICE 默认关闭，完成部署后在插件页手动启用，再在预览方式中选择。其优先级低于原 Word 预览，不改变既有默认视图。

2026-09-09 本机验收完成后，已按用户授权启用该插件；刷新 `http://localhost:7001` 即可选择。上述「默认关闭」是仓库及其他未配置部署的默认策略。

## 部署与版本

使用原 `docker-compose.yml` 和 `./run.sh`，不开第二套应用或第二个前端端口。此服务为可选 `office` profile，未配置时原系统正常运行，选择本插件会明确提示未就绪。

当前适配范围是该 Compose 栈固定的 `AUTH_PROVIDER=none` 本机模式。自定义 SSO 认证部署尚未适配：网关会剥离用户 Authorization／Cookie，而 SSO 中间件可能在私有能力校验前重定向 frame／status／原件请求。不要通过放行整个私有路径或关闭认证来绕过；SSO 部署需要另行设计精确的网关身份接入和回归验证，不能把本机验收视为其兼容性保证。

插件页的启停控制当前用户的阅读能力和会话，不会停止共享的文档服务进程。不使用此能力时，可以用 `./run.sh stop onlyoffice office-gateway` 停止两个辅助服务以释放资源；原 PDF、DOCX 和 LibreOffice 转换预览仍可使用。重新启动使用原栈 `./run.sh up -d onlyoffice office-gateway`。

1. 首次运行 `bash scripts/prepare-office-viewer.sh`。脚本仅在不存在时创建两把独立随机密钥，写入忽略提交/镜像构建的 `.local/office-viewer.env`，权限为 600；重复运行不覆盖已有密钥。不要把该文件提交或发送给他人。
2. 本机 `.env` 设置 `ONLYOFFICE_ENABLED=true`、`COMPOSE_PROFILES=office`，保留 `FRONTEND_BIND_HOST=127.0.0.1` 和 `FRONTEND_PORT=7001`。不要将 Office 来源替换为远程域名。
3. 用 `./run.sh --profile office pull onlyoffice` 拉取，再用 `./run.sh --profile office up -d --build backend frontend onlyoffice office-gateway` 更新原栈。此文档不会自动执行部署。
4. 初次启动字体索引和文档服务需要时间；`./run.sh --profile office exec -T onlyoffice curl -fsS http://localhost/healthcheck` 返回 `true` 后再打开文档。日志可用 `./run.sh --profile office logs --tail=100 onlyoffice office-gateway backend` 查看，不要分享未经审查的原始日志。

锁定官方 `onlyoffice/documentserver:9.3.1.2` 多架构索引：

```
sha256:290a0a1406a485acc8a6bc20418ac7158c5eec78b4844c218f8f975a9800ddbc
```

已核验其中 ARM64 子镜像为 `sha256:a4bfccb3045be0a79c6f6e9c5f37147074836bf30aede3a18448f93b7fa693fc`。不使用浮动 `latest`；后续升级须重做三种办公格式、JWT、浏览器路由和网络隔离验收。官方支持 ARM64 Docker 部署。容器上限 4 GiB 内存、2 CPU，不挂载数据集目录、宿主机文件或 Docker socket。官方镜像自带 `VOLUME` 会产生文档服务专属的匿名缓存/内部数据库卷；这不是用户数据目录，但不能声称“无任何卷”或重建就清空缓存。只读服务不把这些缓存写回原件。[官方 ARM64 安装](https://helpcenter.onlyoffice.com/zh/docs/installation/docs-community-install-docker-arm64.aspx)、[官方 Docker 项目与资源要求](https://github.com/ONLYOFFICE/Docker-DocumentServer)。

转换副本目录 `/var/lib/onlyoffice/documentserver/App_Data/cache/files` 单独使用 512 MiB `tmpfs` 硬容量上限，并计入容器 4 GiB 内存上限；满额时后续转换可能明确失败，不回写原件。它不是数据集挂载。镜像其余内部数据库/日志匿名卷仍须监控容量，不能把这项上限说成整套服务磁盘配额。

固定镜像的默认 `expire.files` 为 86,400 秒，`filesCron` 每小时回收、每次至多 100 项；该定期清理不保证活跃会话立即回收。默认内部会话闲置时限 1 小时、绝对时限 30 天，与本项目 15 分钟原件租约不同。不要用广泛的 Docker volume prune 清理其他业务数据。

## 一条插件入口，两个隔离来源

浏览器主应用仍为 `http://localhost:7001`；ONLYOFFICE 使用同一前端端口上的独立来源 `http://office.localhost:7001/office-viewer/…`。这是安全边界：办公 SDK 不在宿主页执行，不能读取主应用 DOM 或同源存储。标准浏览器将 `.localhost` 解析到回环地址；不需要公网 DNS。

```
主应用 → POST /api/v1/files/{id}/visualization
          {plugin_id:"viz-onlyoffice", operation:"prepare", options:{}}
        ← 协议 2 resources（短期阅读地址，不含真实文件路径）

独立 Office iframe → 前端 Office 虚拟主机 → scoped gateway → ONLYOFFICE
                                                    ↘ 私有 frame/status 能力
ONLYOFFICE → 专用 internal 网络 → gateway 的 /files/{随机令牌}
                              → 后端已鉴权、有界的原件读取
```

ONLYOFFICE 只加入 `internal: true` 专用网络；不能连接主应用网络、数据库、MinIO、Docker daemon 或公网。唯一双网络桥接容器是只读文件系统、非 root、无额外 Linux capabilities 的精确路由网关。网关没有通用 URL 转发功能；其源文件端口仅接受固定长度随机能力令牌。外部 DNS 不使用宿主机公共解析器。

前端 Office 虚拟主机仅转发 `/office-viewer/`，其他路径包括 `/api/` 均返回 404。主应用来源不提供 Office UI，也不公开 provider 私有端点。后端和前端同时拒绝来自 Office 来源的 API 请求，包括无需 CORS 预检的简单 POST、尾点/大小写域名、`Origin: null` 和 WebSocket；独立来源本身不是匿名管理 API 的授权。iframe 不开放弹窗、顶层导航、表单提交或下载，CSP 限制资源连接为自身及本地数据/blob。

## 授权、只读与限制

- `prepare`、frame、status 和原件读取都检查文件所有者、私有产物边界、版本、插件启用状态和 Cordis revision。对外仅返回统一 `resources`，不回传宿主机路径或存储地址。
- 原件不超过插件预算且最多 64 MiB。沿用统一有界分段读取和两槽临时文件预算；不调用全量无界下载。读前、读中和交付前继续检查文件及插件状态。
- 每个阅读令牌寿命 15 分钟，最多同时 16 个；每个最多 8 次原件 GET。Redis 只存文件 ID、用户 ID、版本和时限，不存文件内容；令牌在 Redis key 中只保存散列。
- 浏览器关闭/切换组件会撤销原件读取授权；wrapper 每 10 秒核验租约，失效或到期销毁编辑器。停用插件后无法签发或拉取原件。
- JWT 覆盖文档配置并带到期时间，文档模式固定为 `view`；禁编辑、下载、打印、复制、评论、修订、宏和插件，不提供保存回调或任何原件写回端点。[官方文档权限](https://api.onlyoffice.com/docs/docs-api/usage-api/config/document/permissions/)、[宏与插件控制](https://api.onlyoffice.com/docs/docs-api/usage-api/config/editor/customization/customization-standard-branding/)、[JWT 部署配置](https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-docker.aspx)。
- 这里的“只读”是系统能力限制，不是 DRM：不能撤销用户已经看见的像素、截图、内存或浏览器缓存；ONLYOFFICE 自己签名的转换缓存 URL/已建立连接不等同于后端原件租约，不能承诺令牌撤销后立即抹除全部已加载内容。客户端销毁和定期状态检查限制正常阅读会话，原件再次读取始终重新鉴权。
- 不接入外部文档链接、AI 服务、协作插件或宏执行；外部资源可能缺失。原文件中没有安装的字体会被本地字体替换。复杂排版仍需与原办公应用比对。原有独立文件下载功能不受本插件的查看器下载开关影响。
- 后端日志对自有能力 URL 脱敏；主应用前端按归一化 URI 禁止记录 Office 能力路径的访问日志，编码路径也不能绕过。网关和 Office 虚拟主机关闭访问日志。反向代理异常日志及第三方容器诊断日志仍可能含短期资源地址，分享日志前须审查。

## 许可证

使用开源 Community Edition，按其 AGPL-3.0 条款部署，保留 ONLYOFFICE 标识和版权信息。非商业用途不自动免除开源许可证义务；分发镜像、修改上游或向其他用户提供网络服务前，应核对该固定版本许可证与源码提供义务，不把“免费”和“无条件授权”等同。本集成没有购买许可证或去除品牌。[官方开源仓库及许可证](https://github.com/ONLYOFFICE/Docker-DocumentServer)。

## 验收

后端 `test_onlyoffice_viewer.py` 覆盖统一结果、预算、owner/version/revision/启停竞争、短期令牌、JWT 权限、网关头、断连资源回收、简单跨域请求、WebSocket 与日志脱敏；`test_office_deployment_guards.py` 保证 profile、无挂载及隔离网关边界。前端 `onlyOfficeViewer.test.mjs` 验证独立本机来源、端口、到期和组件释放。

上述单元测试不能替代真实服务验收。部署后执行 `cd frontend && node tests/browser/onlyoffice-native-smoke.mjs`：脚本仅上传三份本机合成 DOCX、XLSX、PPTX，经真实统一入口和原生文档服务打开。除画布外，还从固定版本 SDK 的只读模型中断言标题与 `Signal=7`，保存截图，验证两个来源隔离、原件摘要不变、停用后拒绝新读取并销毁查看器、网关泛路径拒绝。测试结束删除本次上传，恢复原插件启停状态，不读取用户文档；可用 `OFFICE_TEST_FORMAT=docx` 限定单一格式。SDK 模型 getter 仅用于验收，不是产品协议或开放脚本执行接口。

首次实机验收（2026-09-09，原生 ARM64 9.3.1.2）三格式均实际渲染并匹配已知合成内容；源码摘要保持不变、无外部请求、停止后释放 iframe。固定网关构建和启动均执行真实 `nginx -t`，Office 容器检查无默认 IPv4 外网路由、无法解析通用 backend/外部域名或直连外部地址。结论只覆盖这些合成样本与当前配置，不能保证任意复杂办公文件均完美排版；无法打开时保持明确错误，不展示假成功。

本次增强内容验收证据目录：`/var/folders/n2/wz3bk3pj2ps8y5qk2zx257v40000gn/T/dataseek-onlyoffice-native-PDMcxp`，包括三份截图和 `results.json`。DOCX 匹配已知标题及表格 `Signal=7`，XLSX 精确匹配 A1/A4/B4，PPTX 匹配原生形状文本标题并在截图核验表格。三份合成上传已在结束时删除，插件恢复测试前的关闭状态。该目录是测试输出位置，不是数据集文件路径或产品响应字段。

保存的本次 JSON 中 `failures/consoleErrors` 里出现的 `status 404` 均在脚本主动停用插件后产生，是验证授权拒绝的预期结果，不是打开失败；三格式在主动停用前的非预期资源失败数组均为空。后续 runner 已将已知的停用/撤销拒绝归到 `expectedRejections`，与真正的加载失败分开，不改写既有证据。针对新增 provider、统一入口和部署边界的后端目标测试共 47 项通过。
