# AI-DataSeek 阶段 1—8 架构改造总结

整理日期：2026-09-06

依据：当前项目代码、阶段专题文档及最近一次已完成的回归与本机验收记录。

范围：在 Cordis 插件架构基础上，落实阶段 1—8 的工具治理、可追踪执行、领域扩展与受控试点。

## 一、整体结论

本轮改造没有替换现有 AgentLoop，而是在原有分析流程周围补齐统一工具契约、调用治理、事件顺序、执行快照、大结果存储、作业状态、凭据审批、模型预算和领域工具选择。

阶段 1—7 已实现并接入现有流程；阶段 8 以受限试点实现，Code Mode 和领域 SubAgent 均保留独立开关、默认关闭，不代表任意代码执行或 Agent Teams 已开放。

保留的架构边界：

- **PlanActFlow / AgentLoop**：继续负责计划、执行、计划更新和最终回答，没有引入第二套主循环。
- **FastAPI 主机**：继续负责业务 API、会话、权限、持久化与运行时协调。
- **现有 SSE**：保留聊天接口、事件名称及原有恢复游标，通过可选字段扩展能力，不替换为 Typert。
- **Docker 数据隔离**：领域工具仍在沙箱执行，数据集只读挂载；宿主机路径继续受允许目录校验，不返回浏览器。
- **产品范围**：聚焦数据集探查与分析，不重新引入已移除的平台业务。

## 二、阶段总览

| 阶段 | 改造主题 | 主要交付 | 带来的价值 |
| --- | --- | --- | --- |
| 1 | Tool Contract v2、生产调用拦截器、声明式卡片 | 统一工具描述、入参/结果校验、超时/并发/审计、受控结果展示 | 新工具可以沿同一契约接入，减少专用适配和绕过治理的路径 |
| 2 | 事件 seq/version、执行快照、录制回放 | 会话事件顺序与去重、不可变环境身份、离线 JSONL 回归 | 更可靠地恢复事件，定位环境变化，重现事件映射问题 |
| 3 | Spill Artifact Store | 大文本结果外置、受权分页读取、过期和删除清理 | 降低上下文与事件存储膨胀，保留按需读取大结果的能力 |
| 4 | AnalysisJob | 持久状态机、取消、超时、租约恢复、作业卡片 | 分析执行从一次工具调用变成可查询、可取消、可关联的记录 |
| 5 | CredentialRef、调用级权限和审批 | 加密凭据、单次授权、私有子进程注入、审批卡片 | 敏感调用有明确同意边界，密钥不进入普通工具参数和展示链路 |
| 6 | ModelDriver、Token Budget、可追溯压缩 | 统一模型请求入口、估算准入与用量结算、成组压缩、模型审计 | 控制长任务的请求规模，跟踪修复/重试开销和上下文变化 |
| 7 | 领域 Preset、按需工具 | 9 个领域、任务局部工具视图、搜索/加载、Agent 配置入口 | 减少无关工具 Schema，并支持持续加入领域分析能力 |
| 8 | Code Mode、领域 SubAgent | 五工具白名单的受限编排、现有流程内的领域执行专家 | 在保留治理和预算的前提下，试验工具组合与专业分工 |

## 三、架构底座：Cordis 与原有运行时的分工

Cordis 的迁移是本轮八阶段的前提，不另算一个阶段。

```text
Vue WebUI ── REST ──> FastAPI
                         ├─ PlanActFlow / 现有 AgentLoop
                         │    ├─ ModelDriver / 预算 / 压缩审计
                         │    └─ 工具适配与调用治理 ──> Docker 沙箱
                         ├─ Mongo / Redis 会话事件 ── SSE ──> WebUI
                         └─ stdio NDJSON RPC ──> Node Cordis 插件目录与生命周期
```

Cordis 负责受信插件的注册、生命周期、校验和不可变目录快照，不直接读取数据集、不执行模型回合，也不直接向浏览器发布事件。工具实现代码仍在 Docker 沙箱运行。

同时保留 Agent 面向工具的 `get_tools()`、`get_tool()`、`ainvoke()` 与 `ToolMessage / ToolResult` 适配边界。目录发布采用先校验后切换；无效候选不会覆盖最后一个有效版本。

每次工具执行携带 `manifest_digest` 和 `execution_bundle_digest`，检查声明与执行代码是否匹配。它们是源码与依赖契约的一致性检查，不是对整个操作系统镜像的完整安全认证。

详见：[Cordis 插件架构](cordis-plugin-architecture.md)。

## 四、各阶段具体改动

### 阶段 1：Tool Contract v2、生产调用拦截器、声明式工具卡片

**已实现的改动**

1. 将工具输入 Schema、输出 Schema、执行影响、权限、超时、并发、可取消能力和展示描述统一为不可变契约；兼容旧声明，未声明输出类型的工具仍明确标记为未类型化。
2. 使用 Node/Python 都能校验的 JSON Schema draft-07 与受限正则规则。入参不合法时不启动处理器；结果不符合已声明 Schema 时，拒绝结果进入模型上下文与卡片。
3. 建立统一调用管线，加入结构化追踪、策略检查、执行期限、并发限制和取消传播。后续阶段的 AnalysisJob、审批和 Spill 继续接在同一条管线上。
4. 在正式执行前检查 Cordis、核心工具与动态 MCP 的完整命名空间；重复名称不能依赖注册顺序隐式覆盖。
5. 通过原有 `tool` SSE 事件增加可选 `presentation` 字段，由前端固定渲染器展示通用、表格、图表、地图、图像、文件和日志卡片。

**价值与边界**

- 新领域工具接入时可以复用契约、执行约束和展示机制，不必为每个工具改 AgentLoop。
- 声明式卡片不允许插件注入 HTML、JavaScript、任意 Vue 组件或任意资源 URL。
- `exclusive` 是后端进程内的并发约束，不是分布式锁；执行影响和权限声明是策略输入，不等于已获得授权。
- 工具输出与事件的大小限制继续存在，不因后续引入 Spill 就允许无限输出。

关键代码：[调用管线](../backend/app/domain/services/tools/pipeline.py)、[生产拦截器](../backend/app/domain/services/tools/interceptors.py)、[工具适配](../backend/app/domain/services/tools/plugin.py)、[声明式卡片](../frontend/src/components/DeclarativeToolCard.vue)。

### 阶段 2：事件 seq/version、执行环境快照、录制回放测试

**已实现的改动**

1. 新事件增加 `version: 1` 与会话内单调分配的 `seq`。原有事件 ID 继续作为 Redis/SSE 恢复游标，未被替换。
2. Mongo 保存逻辑事件的序号预留和内容摘要，支持并发重试幂等；同一个逻辑事件身份对应不同内容时拒绝发布。分配失败允许留空号，不复用序号。
3. 断线恢复可以同时携带 `event_id` 和 `event_seq`：先恢复持久事件，再连接 Redis 流并去除重叠；前端拒绝重复、倒序和不支持版本的事件。
4. 每个真实任务保存不可变 `ExecutionEnvironmentSnapshot`，记录 Cordis 目录、执行代码、沙箱、模型、提示词身份和有效工具策略等指纹。同一任务不能悄悄换环境。
5. 增加有版本、大小限制和完整性校验的离线 JSONL 录制格式，以及通过现有 EventMapper 的确定性回归测试。

**价值与边界**

- 可以区分“事件重复/漏接”和“执行环境已经变化”，降低排障难度。
- 老事件缺少新字段时保留兼容投影；老会话没有快照也能读取。
- 环境快照不保存密钥、宿主机路径、用户提示词或工具参数原文；敏感环境身份采用服务器密钥 HMAC。
- 录制回放用于重建和校验事件，不会重新调用模型或工具，也没有新增浏览器“一键导出/重跑”入口。
- 原始录制可能包含会话私有正文，属于受限诊断数据，不等同于脱敏公共导出。
- 当前顺序依赖单会话串行发布；Mongo 序号分配本身不解决未来多 Worker 的分布式发布排序。

关键代码：[事件模型](../backend/app/domain/models/event.py)、[Redis 事件队列](../backend/app/infrastructure/external/message_queue/redis_stream_queue.py)、[环境快照](../backend/app/domain/services/execution_environment.py)、[录制回放](../backend/app/domain/services/event_recording.py)、[前端游标](../frontend/src/utils/agentEventCursor.ts)。

详见：[事件与回放](session-events-and-replay.md)、[执行环境快照](execution-environment-snapshots.md)。

### 阶段 3：Spill Artifact Store

**已实现的改动**

1. 工具执行后的大文本不再直接无限进入 Agent 对话与事件记录；超过内联阈值时，经统一结果拦截器转存到现有 GridFS、MinIO 或混合存储。
2. 用受限预览、大小、摘要和 `spill://artifact/<id>` 引用代替大正文。公开事件和持久化投影继续脱敏。
3. 增加 `spill_artifact_read`，按 owner/session 检查权限并执行原生字节范围读取，不因读取后面的页而反复下载整个前缀。
4. 增加确定性引用、写入幂等、存储失败协调、删除墓碑和周期清理；删除会话时联动处理私有 Spill 对象。

**当前默认值**：内联阈值 50,000 字节，单结果存储上限 32 MiB，保留 168 小时。实际部署以配置为准。

**价值与边界**

- 长日志、大表格摘要等可以按需读取，避免反复挤占上下文和事件存储。
- Spill 不是普通分析产物附件，不进入会话文件下载列表，也不暴露底层存储对象 ID。
- 存储不可用时仍限制大正文，明确返回不可用状态，不把存储失败伪装成工具分析失败或重新执行工具。
- 这是执行后的外置存储，不保证处理器执行期间零内存峰值；既有沙箱输出上限仍然必要。
- 默认不是永久归档。引用过期后仍可回放事件，但不能再读取完整内容；Mongo 全不可用等极端异常可能需要恢复后的存储对账。

关键代码：[Spill 抽象](../backend/app/domain/external/spill.py)、[存储实现](../backend/app/infrastructure/external/file/spill.py)、[结果治理](../backend/app/domain/services/tools/spill.py)、[持久化投影](../backend/app/domain/services/tools/spill_projection.py)。

详见：[Spill Artifact Store](spill-artifact-store.md)。

### 阶段 4：AnalysisJob

**已实现的改动**

1. 为 Cordis 分析工具和指定核心分析工具增加持久执行记录；交互式 `shell_exec` 不冒充已经完成的分析作业。
2. 建立 `queued → running → succeeded / failed / cancelled / timed_out / interrupted` 状态机，并记录取消中的 `cancelling` 状态。
3. 使用状态与 revision 的比较更新，避免迟到回调、SSE 或轮询把终态覆盖成运行中。
4. 增加租约、心跳和故障恢复：失联执行在租约过期后标记为中断，不自动重跑可能已经产生副作用的操作。
5. 增加会话级作业查询/取消 API；工具详情卡显示状态、耗时、期限和取消入口，并关联执行快照及 Spill 引用。

**价值与边界**

- 用户可以看到调用是在排队、执行、超时还是中断；排查时可以关联到同一次执行尝试。
- 当前是 Agent 任务内可独立取消的 asyncio 执行单元，Agent 仍等待普通工具结果；不是分布式任务队列，也不是脱离会话持续运行的后台作业。
- 保留既有执行期限，当前生产工具期限上限为 120 秒；没有虚构完成百分比。
- 取消作业会停止依赖该作业的当前回合，但不回滚已经产生的文件，也不影响其他会话。

关键代码：[作业模型](../backend/app/domain/models/analysis_job.py)、[作业服务](../backend/app/domain/services/analysis_job_service.py)、[作业接口](../backend/app/interfaces/api/analysis_job_routes.py)、[前端作业卡片](../frontend/src/components/AnalysisJobCard.vue)。

详见：[AnalysisJob](analysis-jobs.md)。

### 阶段 5：CredentialRef、调用级权限与审批

**已实现的改动**

1. 增加 owner-scoped 的加密工具凭据库。对外仅返回 `CredentialRef`、版本和状态，不回读明文或密文。
2. 受信插件可声明精确的工具/slot/provider 绑定；模型不传递密钥，也不自行选择或扩大授权。
3. 对网络、凭据使用、外部副作用或命名权限要求本次调用的明确审批，而不是一次批准后永久放行。
4. 审批绑定参数摘要、所有者、会话、任务、调用、工具、环境、目录版本和凭据版本；执行前再次验证并原子消费一次性授权。
5. 审批等待放在执行期限和并发锁之外；拒绝、过期、撤销或取消会停止依赖回合，不进入自动修复/重试。
6. 凭据仅在批准后，经现有私有 REST 通道注入一次子进程的 `DATASEEK_CREDENTIAL_*` 环境，普通后续命令不继承。回显在模型、SSE 和 Spill 边界前进行脱敏。
7. 增加会话审批卡片、批准一次/拒绝按钮，以及插件页的工具凭据面板。

**价值与边界**

- 敏感工具调用可由用户逐次确认，凭据撤销会阻止未消费的授权继续使用。
- `consumed` 仅表示授权已使用，不代表分析成功；结果由 AnalysisJob/工具结果独立报告。
- `CREDENTIAL_ENCRYPTION_KEY` 为独立 Fernet 密钥，不回退到 DeepSeek API Key；未配置时禁用凭据写入和解密，不阻断普通本地分析。
- 当前不迁移既有 DeepSeek/MCP 密钥，不是通用 OAuth、Cookie、Header 或 KMS 注入系统。内置本地分析工具无需凭据，空 slot 列表是正常状态。
- 面向受信插件，不是恶意插件隔离器；审批不是操作系统级网络防火墙，也不能收回已交给执行中进程的密钥或回滚外部副作用。

关键代码：[凭据服务](../backend/app/domain/services/credential_service.py)、[审批服务](../backend/app/domain/services/tool_approval_service.py)、[调用授权](../backend/app/domain/services/tools/authorization.py)、[审批卡片](../frontend/src/components/ToolApprovalCard.vue)、[凭据面板](../frontend/src/components/ToolCredentialsPanel.vue)。

详见：[凭据与调用审批](tool-credentials-and-approvals.md)。

### 阶段 6：ModelDriver、Token Budget、可追溯压缩

**已实现的改动**

1. `create_chat_model` 统一通过 `LangChainModelDriver`，保留原有 LangChain 适配；工具绑定、解析修复等物理请求进入同一准备、计量和追踪边界。
2. 每次请求执行上下文检查；Agent 任务内使用 ContextVar 共享 token/物理请求账本，使子任务、修复与重试纳入同一任务预算。
3. 请求前预留“输入估算 + 最大输出”；获得有效 provider usage 后结算。缺失用量或调用失败时保留预留，采用保守计量。
4. 对上下文的深拷贝执行成组压缩：保留系统指令、最新用户请求、未完成的工具调用和最新完整交互；旧工具调用与回复整体处理，保留类型化 Spill 引用。
5. 超预算时以明确控制流终止依赖回合，不在修复循环中重复消耗。
6. Mongo 保存请求身份、预算、用量、压缩范围和 HMAC 等元数据；增加受会话权限控制的模型 trace 分页查询。

**当前默认工程限额**：上下文估算容量 131,072 tokens，安全预留 2,048 tokens，单 Agent 任务累计预算 1,000,000 tokens，最多 128 次物理模型请求。

**价值与边界**

- 模型调用有统一的准入和诊断依据，不再把 SDK 修复、重试视为不可见开销。
- Token Budget 是应用工程预算，不是账户配额、价格计算或硬账单上限。本地估算不等于模型 tokenizer；返回实际用量可能高于预留，已经发出的请求无法撤销。
- 数据集初始化路由和推荐问题也经过 Driver 的单次上下文检查，但不属于 AgentTaskRunner 的累计账本和任务 Mongo trace 范围。
- “可追溯压缩”指元数据审计，不是全文备份。HMAC 不可逆，持久化副本的字节截断也不可逆；不能据此宣称被压缩正文能完整回放。
- 没有新增模型 trace 前端卡片，也没有替换现有 SSE 为模型原生 token 流。

关键代码：[模型适配](../backend/app/infrastructure/external/llm/chat_model.py)、[上下文预算](../backend/app/domain/services/context_budget.py)、[模型运行时](../backend/app/domain/services/model_runtime.py)、[模型追踪](../backend/app/domain/models/model_trace.py)。

详见：[ModelDriver 与上下文预算](model-driver-and-context-budget.md)。

### 阶段 7：领域 Preset 与按需工具

**已实现的改动**

1. 在同一个 Cordis 目录上建立 9 个领域：通用数据分析、表格与工作簿、地学与遥感、图像数据、化学与分子结构、生物序列、空间与天文、文档证据、谱学与衍射。
2. 新任务可使用任务局部 `PluginToolView`，初始只暴露领域的 3—5 个插件 Schema，原有核心工具继续保留。
3. 提供 `tool_catalog_search`、`tool_catalog_load`，只能在已选领域范围内加载。单次最多加载 8 个，视图最多 48 个插件工具，失败不部分生效。
4. 新用户任务重置工具视图；执行快照记录初始选择、摘要与试点开关，后续加载使用原有 ToolEvent。
5. 增加领域预设展示、Agent 配置编辑、创建配置和“使用此配置新建会话”入口；修复此前难以找到试点开关的问题。

**价值与边界**

- 减少无关 Schema 带来的输入开销和工具选择干扰，领域扩展不要求每次把整个目录交给模型。
- 无显式 Agent 配置时，默认 `general/on_demand`；旧配置没有 `tool_runtime` 时保留 `general/all`，两个试点仍关闭。
- `all` 指选定领域的全部可用工具，不一定是整个插件目录。
- Preset 是选择机制，不新增权限、不全局启用插件，也不替代 Docker 或调用审批。
- 回归覆盖了默认初始 Schema token 估算少于旧全量模式三分之一；这不是整项任务 token、时延或账单降低同等比例的保证，工具发现可能增加模型轮次。

关键代码：[领域定义](../backend/app/domain/services/domain_presets.py)、[按需工具视图](../backend/app/domain/services/tools/tool_selection.py)、[运行配置](../backend/app/domain/services/tool_runtime_config.py)、[配置界面](../frontend/src/components/settings/AgentProfileRuntimeSettings.vue)。

### 阶段 8：Code Mode 与领域 SubAgent 受控试点

#### 8.1 Code Mode

- 使用 Python 外形的受限 AST 解释器，不执行任意 Python，也不使用 `eval/exec`。
- 禁止导入、循环、递归、属性访问、函数定义、异常捕获和程序内部的工具发现/委派。
- 当前仅准入五个经审查的只读工具：`data_format_inspect`、`hierarchical_store_inspect`、`cf_semantics_validate`、`workbook_inspect`、`table_profile`；工具还必须属于当前领域并已加载。
- 默认限制为 12,000 字节源码、512 个 AST 节点、32 条语句、16 次串行工具调用、120 秒总期限；参数、中间结果和最终结果也受大小限制。
- 每个内部调用仍经过权限、AnalysisJob、超时、审计和 Spill，不产生治理旁路。工具事件仅记录源码字节数与 HMAC，不广播源码。

它用于受控组合少量工具，不是独立代码编辑器或任意 Python/Shell 自动化。它也不是事务：后一步失败不撤销前面已经完成的只读调用；原有私有 Agent 记忆仍按既有规则保存模型工具调用。

#### 8.2 领域 SubAgent

- 在原有 PlanActFlow 调度器中创建有领域工具视图的 `ExecutionAgent`，没有引入嵌套 AgentLoop。
- 通用 Preset 可增加表格、地学两名专家；单领域 Preset 可增加对应专家。最多 4 个领域 Agent，每个最多 4 次执行迭代。
- 专家拥有独立记忆、工具视图和 trace 身份，共享父任务的模型预算、Docker 沙箱和数据上下文。
- 不提供 Shell、浏览器、文件工具包、MCP、Code Mode、技能创建和嵌套委派；领域插件仍可在既有权限允许时生成分析产物，因此不是“所有专家全局只读”。
- 父流程继续负责计划更新与最终汇总。启用领域专家后，数据集问题交给原计划流程选择专家步骤，不使用直接数据集快速路径。

**状态边界**：两个开关独立、默认关闭。未实施 Agent Teams、递归委派、自修改插件、模型生成 Workflow 或新的通信协议。

关键代码：[受限解释器](../backend/app/domain/services/code_mode.py)、[Code Mode 工具](../backend/app/domain/services/tools/code_mode.py)、[原有流程集成](../backend/app/domain/services/flows/plan_act.py)。

阶段 7—8 详见：[领域预设与试点模式](domain-presets-and-code-mode.md)。

## 五、WebUI 与兼容性结果

本次对 WebUI 的改造围绕已有聊天和插件界面展开：

- 插件页可查看 Cordis 运行状态、目录版本、工具数量、领域预设和工具凭据。
- 工具调用可以展示声明式结果、AnalysisJob 状态及一次性审批卡片。
- 领域预设页可直接打开 Agent 配置；选择器也提供管理配置入口。
- 保存配置后可显式选择用于新会话，不自动发送消息、创建会话或重写已有会话的运行配置。

本机常用入口：

- [Cordis 运行时](http://localhost:7001/plugins?tab=runtime)
- [领域预设](http://localhost:7001/plugins?tab=presets)
- [直接打开 Agent 配置](http://localhost:7001/plugins?tab=presets&settings=agent-profiles)
- [工具凭据](http://localhost:7001/plugins?tab=credentials)

旧 SSE 字段、旧 Agent 配置、旧事件与会话读取继续兼容。事件 `seq/version`、作业 `revision/schema_version` 和审批 `revision` 各自解决不同对象的顺序与版本问题，不能互相替代。

## 六、验证与部署记录

最近一次完整验收包含阶段 1—8 和后续数据集管理改动，结果如下。本次整理 Markdown 没有重新执行整套测试，也没有重新部署服务。

| 验证项 | 最近一次已完成结果 |
| --- | --- |
| 前端类型检查、构建、测试 | 类型检查与构建通过，75 项测试通过 |
| 后端完整回归 | 1147 项通过，29 项跳过 |
| 沙箱完整回归 | 181 项通过，3 项跳过 |
| Compose 配置 | 校验通过，继续使用单一 `ai-dataseek` 项目 |
| Cordis 实际运行时 | healthy；15 个插件、280 个工具；后端/沙箱 manifest 与执行包摘要一致 |
| 数据集配套验收 | 18 个跳转、9 个领域筛选、桌面/手机布局通过；37 个数据文件 SHA256 一致且数据卷只读 |

关键回归覆盖：

- 阶段 1：`test_tool_execution_pipeline.py`、`test_tool_execution_interceptors.py`、`test_phase1_vertical_integration.py`。
- 阶段 2：`test_event_sequence.py`、`test_execution_environment_snapshot.py`、`test_event_recording_replay.py`。
- 阶段 3：`test_spill_artifact_store.py`、`test_spill_durable_lifecycle.py`。
- 阶段 4：`test_analysis_job_service.py`、`test_analysis_job_integration.py`。
- 阶段 5：`test_credential_service.py`、`test_tool_approval_service.py`、`test_tool_authorization_integration.py`。
- 阶段 6：`test_context_budget.py`、`test_model_driver.py`、`test_model_budget_cancellation.py`、`test_model_trace_pagination.py`。
- 阶段 7—8：`test_domain_presets.py`、`test_tool_runtime_flow.py`、`test_code_mode.py`、`test_code_mode_flow.py`。

本次实际部署遵循用户指定的本机 `127.0.0.1:7001`，不新增公网环境或第二套栈。容器更新统一通过 `./run.sh`。

重建镜像不会原地升级已经存在的会话沙箱。旧沙箱与新执行包不一致时会拒绝工具调用；应使用新会话，或在保存未持久化成果后另行规划迁移，不通过关闭摘要校验绕过一致性约束。

## 七、后续使用与扩展原则

1. 新领域工具先补契约、测试和依赖，再加入受信 Cordis 目录及对应 Preset，避免在 AgentLoop 中堆叠领域特例。
2. 对网络、凭据和外部副作用显式声明，并使用已有逐次审批；不要把 Preset 或 Code Mode 开关当授权。
3. 大输出使用 Spill，正式交付文件使用已有分析产物链路，两者保留不同的权限和生命周期。
4. Code Mode 白名单扩充必须逐个审查真实实现，不能因为有只读声明就自动准入。
5. 试点回退可以选择 `general/all` 并关闭两个开关；这不会删除已有会话、产物或前六阶段的治理机制。
6. 多 Worker、分布式长作业、完整正文录制及不受信第三方插件属于后续独立架构工作，不能从当前功能推导为已经支持。

## 附录：阶段 8 之后的数据集管理配套交付

这一部分是后续新增功能，不计入原先八阶段：

- 增加[数据集管理页](http://localhost:7001/datasets)，支持搜索、领域筛选、本地持久登记、信息编辑和软归档。
- 按 9 个系统领域各准备 2 个真实国际开放数据集，共 18 个、37 个文件，约 3.6 MB；来源、许可、样本范围和校验摘要均有记录。
- 点击名称或“进入探查”复用已有数据集探查页；新任务可选择 Agent 配置，并提示领域不匹配。
- 本地登记保留用户指定的默认目录；旧临时 `tds_` 提交的 TTL/配额语义不变。归档不删除原始数据。
- 真实文件验证推动了 CSV 分隔符、数值误判日期、TIFF 发现与预览范围、XRDML 坐标与有限值检查等兼容修复，未变更 AgentLoop 或 SSE。

完整清单、许可说明与接口见：[数据集管理文档](dataset-management.md)。
