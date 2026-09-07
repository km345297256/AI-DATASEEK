# 阶段 1—8 兼容性回归与分析效率验证

测试日期：2026-09-06—2026-09-07。对象：当前工作区代码及本机 `http://localhost:7001` 的现有部署。

本报告是本轮实际执行记录，与[阶段 1—8 改造总结](phase-1-to-8-change-summary.md)配套；不是仅根据设计文档推断的验收。

## 1. 结论与范围

本轮发现并修复移动端工具详情面板过窄、治理卡片堆叠后被裁切的问题，同时落地一项数据集查询优化。保留 PlanActFlow / AgentLoop、FastAPI、SSE、Docker 只读数据集边界，没有替换主循环、修改用户 Agent 配置或自动开启试点。

效率优化：数据集内置目录的存在性检查由逐项查询改为一次批量、仅 ID 投影的查询，已通过专项回归、真实数据库对比并更新至本机后端。UI 修复只调整移动端宽度与治理卡片区域滚动；不改工具执行和桌面主布局。其余新增文件是测试和报告。

“通过”限于以下已执行的场景，不能等同于对所有历史功能、任意数据规模、模型答案或并发负载的绝对保证。本轮没有发起真实模型请求，也没有使用真实外部服务凭据。

## 2. 自动化结果

| 检查层 | 最终结果 | 说明 |
| --- | --- | --- |
| 前端 TypeScript | 通过 | `npm run type-check` |
| 前端测试 | 79 通过，0 跳过 | 原 76 项 + 3 项工具面板布局/可访问性回归 |
| 前端生产构建 | 通过 | 有既存非阻断构建警告，见第 7 节 |
| Cordis Node host | 19 通过，0 跳过 | 包含生产目录加载、契约、生命周期、失败回滚、跨语言摘要 |
| 后端全量 | 1,154 通过，29 跳过 | 最终 21.02 秒；首轮 1,147 通过后新增 7 项回归 |
| 沙箱严格全量 | 190 通过，0 跳过 | 显式启用地学 CLI 检查；14.71 秒，不含安装/启动 |
| Compose 配置 | 通过 | 唯一现有部署，前端仅绑定 `127.0.0.1:7001` |
| 浏览器专项 | 通过 | Chromium 桌面/移动端；真实 GET + 受控 mock 写入/事件 |

前端、后端、沙箱与 Cordis host 合计 **1,442 项测试通过、29 项跳过、0 项失败**；浏览器专项、真实文件往返和数据正确性断言另外记录，不重复加入这个合计。新增 16 项长期回归测试。

前后端测试中的 mock 是明确的测试边界：模型、部分仓储和审批决策使用测试替身；Cordis 集成使用真实 Node/Cordis 目录。不能把这些测试写成“真实云模型全链路验收”。

### 后端 29 个跳过项

- 11 项旧文件 API 测试要求显式启用 live integration。另用当前部署的真实接口完成空文件、中文文件名、1 MiB 二进制上传、信息查询、两种下载路由和删除后的 404 验证；3 个本轮测试文件均已清理，没有读取或删除原文件。
- 11 项旧 `DockerSandbox` live-client 测试固定要求 localhost 沙箱。另补 6 项真实沙箱 HTTP 服务层回归，使用一次性容器里的 Uvicorn、FastAPI 与生产 FileService；不等同于这 11 项客户端测试已经通过。
- 7 项旧预选 quicklook / unpack / catalog 快路径测试已明确标记为被当前 Agent 工具循环设计替代。本轮保留标记，没有删除跳过项或把它们计入通过数；现行快路径、结果呈现、工具循环由当前回归验证。

沙箱原先另有 3 项环境条件跳过（`ncdump`、`h5dump`、`projinfo`）；本次以 `AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1` 实际执行通过，最终沙箱无跳过。

## 3. 阶段 1—8 兼容性矩阵

| 阶段 | 本次重点验证 | 保留的旧行为 / 边界 |
| --- | --- | --- |
| 1：契约、拦截器、卡片 | Schema 校验、重复命名拒绝、生产管线接线、取消/期限、声明式投影、Cordis 有效快照回退 | 旧工具适配接口与 shell 结果仍可使用；声明式卡片不取代所有旧渲染器 |
| 2：事件与快照 | 序号幂等、重试、环境身份、录制校验、现有 SSE 投影、旧/新混合历史 | 无 `seq/version` 的旧事件可读；原 `event_id` 恢复游标保留；录制不重新执行模型/工具 |
| 3：Spill | 阈值、失败回退、owner/session 权限、范围读取、重复写和过期/墓碑清理 | 普通分析产物仍走原附件链路；Spill 私有对象不混入普通附件 |
| 4：AnalysisJob | 状态/revision、终态保护、取消、期限、租约、重复接线与工具结果关联 | 仍在原 Agent 任务中等待结果；不扩展为脱离会话的后台队列 |
| 5：凭据与审批 | 一次性授权、参数/身份绑定、拒绝和失效、敏感字段脱敏、私有子进程传输 | 审批不被重试绕过；分享页不能批准/取消或轮询私有状态；不改真实凭据 |
| 6：模型预算与压缩 | ModelDriver 接线、估算准入、取消/重试结算、上下文成组压缩、追踪与分页 | 仍使用现有模型调用流程；不以删除工具调用/结果配对的方式压缩 |
| 7：领域与按需工具 | 9 个领域实际目录、初始/可用工具、按需加载、绑定一致性、快路径 | 旧配置缺 `tool_runtime` 时仍是 `general/all`；不自动缩减旧配置的工具能力 |
| 8：受控试点 | Code Mode 语法/白名单/限额、嵌套治理、SubAgent 隔离和预算、配置入口 | 两个开关默认关闭；不引入第二套 AgentLoop、任意 Python 执行或 Agent Teams |

另新增一项治理叠加测试：经实际 PlanActFlow 工具 wrapper，走“审批 → AnalysisJob → 期限/并发 → 单次准入 → Spill”完整拦截链。重复配置不叠加重复拦截器；测试处理器只执行一次，授权只消费一次，最终 Job 与 Spill 对应。这是内存仓储/模拟处理器的集成测试，不是外部网络工具实调。

### 浏览器旧功能

- 截图复核发现移动端面板仍用父宽度的一半，且作业/审批/Spill 区域裁切；修复为移动端全宽、治理卡片区域独立可滚动，保留下方原工具视图。回归不仅检查 DOM 可见，也检查实际可视区域与滚动可达性。
- `/`、`/home`、`/chat` 可进入；插件页的 skills、mcp、renderers、runtime、presets、credentials 六个页签可进入。
- 数据集管理的 9 领域筛选、搜索、18 个数据集逐一进入探查页通过；本机登记默认地址正确，路径不进入 URL、localStorage、sessionStorage。
- Agent 设置入口、深链、保存后选择新会话配置通过；未保存编辑不能冒充已保存配置。保存/编辑/归档/登记均 mock，不更改用户记录。
- 旧无版本 shell 历史、混合新版消息、表格卡片、Spill 提示、完成 Job 与终止审批可共存。
- 合成 SSE 验证恢复时发送旧 ID 与 `event_seq`；重复、倒序、未知 version 不渲染。
- 分享回放只读；即使历史含 running Job / pending 审批，也不提供控制按钮或请求私有轮询。
- 修复后桌面和移动端补充历史/SSE/布局检查各 18 项，共 36 项通过。移动端面板实测宽度 390 px、左边界 0；治理区可滚动至底部 Spill，旧 shell 输出容器仍保留约 203 px 高度。旧文件视图、键盘关闭同样通过。测试记录无页面 JS 错误、HTTP 5xx、意外写入或真实模型请求。

## 4. 真实数据与文件正确性

对实际部署数据卷的 18 个开放数据集、37 个原始文件逐项检查大小与 SHA-256，全部一致；完成分析后再次检查，37 个文件均未改变。专用审计容器通过只读卷挂载运行，没有写入原数据。

实际执行了 30 项数据正确性断言、53 个工具/输入组合、107 次工具 CLI 调用，包括：

| 类型 | 验证例子 |
| --- | --- |
| CSV / XLS / XLSX | Iris 150×5、Wine 178×14、Wine Quality 红 1599×12 / 白 4898×12、Concrete 1030×9；均值与独立 pandas 计算一致；XLSX 是临时格式回归样本 |
| NetCDF / Shapefile | NOAA 12×73×144，年 1 时间轴保留 cftime；Natural Earth 177 个要素、EPSG:4326 |
| TIFF / PNG | 四个 400×400 TIFF 及两个预览 PNG 解码，模式、像素范围、帧数符合实际文件 |
| FASTA / PDB | 两个参考序列长度正确；1CRN 327 原子、1UBQ 660 原子 |
| FITS | WFPC2 100×100 与 WCS；FOS 2×2064 与 2 个 HDU；不把误差样本误称为完整波长-流量数据 |
| PDF / XRDML | 两篇论文文本可读；五条谱点数、角度轴及扫描解析正确 |

衍生 CSV 的分组均值逐项对照，PNG 可完整解码且尺寸 1600×960，PDF→TXT 含预期关键词。输出均在测试临时目录生成并回收，不占用用户附件列表。

文件专项另外验证了真实后端存储接口和独立沙箱 HTTP 服务：空内容、全字节二进制、1 MiB、中文名均逐字节一致；缺失文件 404、缺 multipart 文件 422。两层分别验证，不冒称跨 Agent、DockerSandbox、文件库的完整链路均已执行。

## 5. 分析效率：已落地优化与可复现基线

### 5.1 本轮新增：消除数据集目录 N+1 查询

旧 `ensure_seed_data()` 每次请求解析清单后，对 18 个内置数据集执行 18 次顺序 `find_one`，且读取完整记录。现在一次 `$in` 查询仅取 `dataset_id`，缺失项仍进入原有校验和幂等登记流程。

对同一已有 18 项数据的真实 Mongo，在同一进程交替运行部署前实现与工作区候选：预热 2 轮，正式各 25 轮。不启用应用 lifespan、不建索引、不登记新种子；禁止进入复制分支。

| 指标（仅存在性检查步骤） | 优化前 | 优化后 |
| --- | ---: | ---: |
| Mongo find 请求 / 次 | 18 | 1 |
| 中位耗时 | 20.497 ms | 9.461 ms |
| 样本 p95 | 35.381 ms | 14.937 ms |

该步骤中位耗时下降约 53.8%，查询次数下降约 94.4%。这不表示整次分析提速 53.8%；没有包含模型、容器准备或实际数据计算。

安全与兼容取舍：不缓存数据库存在状态；归档记录不会被自动启用；删除记录后下次仍可恢复；源文件 SHA、目录匹配、防符号链接/路径逃逸、原子复制、挂载只读逻辑保留。新增 5 项测试覆盖 1/18/100 个已有记录的单查询、空目录零查询、归档与删除后的重查恢复。

### 5.2 阶段 7 的实际收益：减少工具声明上下文

使用真实 Cordis 的 15 插件 / 280 工具目录，经 PlanActFlow 和 ModelDriver 绑定。对比旧配置 `general/all` 与无已保存配置的默认 `general/on_demand`。

| 指标 | 旧配置全工具 | 默认按需工具 |
| --- | ---: | ---: |
| 初始插件工具数 | 280 | 3 |
| 模型 Schema 总数（含核心工具） | 319 | 44 |
| 规范化 Schema 序列化大小 | 147,614 B | 21,535 B |
| 工具 Schema 本地 Token 估算 | 54,312 | 7,882 |
| 固定样例的总输入 Token 估算 | 59,074 | 12,645 |

工具 Schema 的本地估算减少约 85.5%。9 个领域按需初始视图均为 44—46 个模型 Schema、约 7,882—8,185 个估算工具 Token；样例数据集快路径进一步为 21 个 Schema、4,463 个估算工具 Token。

数字来自离线装配：显式包括现有搜索和 Spill 读取工具，搜索/存储使用替身、无额外 MCP 工具、试点关闭；实际配置启用附加工具后计数会不同。估算器为 `utf8_bytes_div3_v1`，不是 DeepSeek 的实际 tokenizer 或计费账单；没有用这组数据推断回答质量、总账单节省或总任务提速。

重复绑定的 25 轮进程内测量还确认：Schema 保持一致，额外 Cordis RPC 为 0。绑定/规范化/序列化中位耗时约 31.75 ms（按需）、34.54 ms（全工具）；样本小且宿主负载未固定，仅留作基线，不据此扩大缓存或绕过治理。

### 5.3 数据工具耗时基线

真实小样本的子进程 CLI 中位数：TIFF 完整性检查约 36 ms、PDB 检查约 17—18 ms、XRDML 检查约 66—69 ms、Iris 表格分析约 190 ms、Shapefile 检查约 333 ms、NOAA NetCDF 检查约 648 ms、PNG 图表生成约 433 ms。

每项 3 次，包含解释器启动/导入/读取/计算/JSON 输出，不含模型、SSE、Job、容器创建。不同格式和样本不能横向当作算法效率排名，也不能将测试总耗时的波动当成架构收益。

保持旧配置兼容的同时，新分析建议显式选择匹配领域与按需工具。Code Mode / SubAgent 继续按场景试点；多 Agent 可能增加模型调用，不能默认认为更快。本轮未替用户修改这些配置。

## 6. 本机部署和用户数据保护

- 全程使用现有 `./run.sh`；测试容器不发布第二个前端端口，后端优化与前端布局修复分别通过对应服务的 `build` / `up -d --no-deps` 生效。
- 更新前确认无 running / waiting / pending 会话。更新前后：6 条 completed 会话、410 条事件、62 条文件记录、18 条目录数据集、1 条旧临时登记保持一致；会话状态/附件/更新时间等投影的整体摘要一致。
- 模型用量记录数、Agent 配置记录数保持一致；未修改用户配置、密钥、真实会话或数据集状态。
- 正在运行的后端源码摘要与已验证候选一致；Cordis 保持 healthy、15 插件、280 工具，清单与执行包摘要不变。
- 2026-09-07 收尾时再次检查部署配置、端口与上述状态摘要，结果仍一致。
- 当前没有存活用户会话沙箱，因此不声称检查了所有历史沙箱挂载。实际验证的是部署数据卷在专用审计容器中的只读挂载，以及自动测试中的挂载校验规则。
- 仅清理本轮新建的 3 个测试附件和临时产物；未删除原有数据。

## 7. 未覆盖项与后续建议

1. 未调用真实 DeepSeek；真实模型答案正确率、首次响应、总分析时延、真实计费 Token 尚未测量。当前证明的是模型边界与工具治理兼容，不是回答质量保证。
2. 未在真实外部账户上使用 CredentialRef / 批准调用。审批、凭据、取消、租约故障与预算主要使用隔离/确定性测试；未在用户正在运行的任务上注入故障。
3. 未做 GB/TB 数据、大规模文件树、高并发会话、长时间 SSE 断线和压力测试；浏览器仅 Chromium，未覆盖 Safari/Firefox。
4. 前端仍有既存大 chunk、3Dmol eval 和 CSS 嵌套构建警告；后端测试有短测试 JWT key 与 Jupyter 重复 OpenAPI operation ID 警告；沙箱有科学计算依赖、时间精度及弃用警告。均未导致本轮失败，不因此认定所有依赖完全兼容。
5. 优先建立固定问题/固定数据/固定模型配置的端到端基准，分别记录模型等待、容器准备、工具处理、产物传输耗时，再决定优化热点。不要为了减少 Schema 或增加并行而削弱审批、预算、只读挂载和旧配置兼容。
6. 大规模压测与真实模型基准应使用专用测试数据、会话和明确调用预算；本轮没有默默开启这些可能占用大量资源或产生费用的测试。

## 8. 代码与复测入口

本轮长期保留的新增回归：

- [目录查询与幂等恢复](../backend/tests/test_dataset_seed_efficiency.py)：5 项。
- [Cordis、领域视图、模型预算与治理叠加](../backend/tests/test_runtime_efficiency_regression.py)：2 项。
- [沙箱真实 HTTP 文件传输](../sandbox/tests/test_file_transfer_regression.py)：6 项。
- [工具面板布局与关闭按钮](../frontend/tests/toolPanelLayout.test.mjs)：3 项源码守卫；实际几何和滚动另外通过浏览器验证。

应用改动：[DataCenterDatasetService](../backend/app/application/services/data_center_dataset_service.py) 查询优化，以及 [ToolPanel](../frontend/src/components/ToolPanel.vue) / [ToolPanelContent](../frontend/src/components/ToolPanelContent.vue) 移动端布局修复。已有 [数据集管理测试](../backend/tests/test_dataset_management.py)仅补批量查询的测试替身。

在项目根目录，使用现有依赖环境可复测：

```bash
(cd frontend && npm run type-check && npm test && npm run build)
(cd plugin-host && npm test)
(cd backend && uv run pytest)
(cd sandbox && AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1 uv run pytest)
./run.sh config --quiet
```

本轮后端实际在一次性镜像中运行，避免依赖本机 Python：

```bash
./run.sh run --rm --no-deps --entrypoint sh \
  -v "$PWD:/workspace:ro" -w /workspace/backend \
  -e TOOL_PLUGINS_DIR=/workspace/tools \
  -e PLUGIN_RUNTIME_TOOLS_DIR=/workspace/tools \
  -e PLUGIN_RUNTIME_EXECUTION_CONTRACT_DIR=/workspace/sandbox \
  backend -c 'uv pip install --python /opt/venv/bin/python --index-url https://pypi.org/simple pytest pytest-asyncio pytest-mock requests && uv run --no-sync pytest -o addopts= -o log_cli=false -p no:cacheprovider -q -ra'
```

浏览器、真实数据 CLI、数据库微基准和部署核验的本机辅助脚本/详细结果位于 `.cache/phase1-8-regression/`，包括 `frontend-result.md`、`sandbox-result.md/json`、`runtime_metrics.json`、`catalog-benchmark-result.json`、`live-file-result.json` 和部署前后摘要。该缓存目录不作为 Git 中长期测试资产；其关键结果已归档到本文。目录基准对比依赖更新前镜像仍存在，本机已更新后不能直接重跑脚本冒充“优化前后”对照。
