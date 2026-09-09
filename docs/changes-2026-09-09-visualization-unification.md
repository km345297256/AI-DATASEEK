# 2026-09-09 本批改动：统一可视化、高清文档阅读与断连修复

## 提交范围

- 分支：`v2`；对比基线：`763b93bd9262ee3266b7c466690efc2ee7074462`。
- 本文汇总该基线之后的已有工作区改动，并随代码、测试及专项文档一起提交。
- 上一批扩展适配器说明保留在 [科学可视化与数据集预览改动说明](changes-2026-09-09-scientific-visualization.md)。
- 本次提交工作不部署、不更新运行服务、不迁移业务数据，也不执行真实模型分析。
- 本次包含统一可视化协议、PDF／Office 高清阅读、DOCX 隔离阅读、ONLYOFFICE 本机只读集成，以及提交前发现的断连资源泄漏修复。此前并行开发已结束，本说明按最终合并范围整理。

## 一、插件统一采用同一公开契约

- 当前 36 个插件统一使用严格整数 `contract_version: 2`。这里的“统一”不是新增 v3，也不再并存原 v1/v2 两个目录。
- 去掉 `data_kind` 和适配器键的 `v2-` 前缀，原 20 个 `v2-*.json` 清单迁移为不带前缀的文件名。
- 新增能力声明 `capabilities`：明确支持的操作、输入模式（整文件、分页或前缀）以及共享预览能力。
- 文件面板中的基础、科学、办公适配器统一接收 `{file, plugin}`，管理页展示能力，不再按 v1/v2 分类。
- 原 34 个稳定插件 ID、格式匹配、优先级、默认开关、权限及输入输出预算与基线逐项比较一致，已有启停偏好无需重建。PDF、Word、PowerPoint 三个插件的发布版本从 `2.0.0` 升为 `2.1.0`；其余原插件发布版本保持不变。
- 新增 `viz-docx` 和 `viz-onlyoffice`，发布版本均为 `1.0.0`。DOCX 默认可用，ONLYOFFICE 仓库默认关闭；两者优先级均低于既有 Word 预览，不擅自替换用户当前默认阅读方式。

清单文件迁移属于代码重构，不是删除数据或用户配置；旧文件及内容仍可从 Git 历史恢复。

## 二、单一规范生成三端校验代码

新增 `contracts/visualization-adapters.json`，集中定义适配器的读取器、视图和能力组合。统一改造原批次为 33 个适配器、34 个插件；办公预览增量加入后，当前为 35 个适配器、36 个插件，存在适配器复用。

`scripts/sync-visualization-contract.mjs` 从该规范生成：

- `plugin-host/src/visualization-adapters.generated.ts`
- `backend/app/domain/models/visualization_adapters_generated.py`
- `frontend/src/visualizations/adapters.generated.ts`

三份生成代码有意纳入版本控制，供各自独立构建上下文使用，不是误提交的构建缓存。`--check` 只校验、不改文件，发现规范或副本不一致即失败，已接入回归脚本和 Cordis 测试。

后续新增适配器应修改批准规范、生成副本并补充读取器和测试，不能只在某一端添加字符串或新后缀来绕过契约。

## 三、统一读取入口和结果结构

新增后端 `unified_visualization.py` 与前端共享 `visualizations/runtime.ts`。普通可视化调用统一为：

```text
POST /api/v1/files/{opaque_file_id}/visualization
```

请求使用 `plugin_id`、`operation`、可选 `version/kind` 与白名单 `options`。操作分为：

- `bytes`：读取授权原始字节，浏览器有界解码。
- `page`：文本或 CSV 分页。
- `preview`：科学、办公等结构化预览。
- `prepare`：分子结构元信息准备，或签发 ONLYOFFICE 短期只读阅读资源；该请求不下载全文件或创建分析任务。
- `job`：仅通过作业子资源显式启动，当前用于 FastQC。

非字节结果统一为 `VisualizationResult`：包含协议版本、插件 ID、文件版本、目录 revision、类型化 payload、metadata、warnings 和 sampled。不同 worker 的私有格式由宿主转换，不再形成另一套公有协议。

原始字节仍采用二进制流，不套 Base64 JSON；响应带文件版本、插件和目录标识，前端校验后读取。QC 作业结果也统一封装，作业状态和私有产物继续复用 AnalysisJob 与 Spill Artifact Store。

## 四、明确的 API 迁移要求

本批移除了旧可视化专用接口，不维护兼容分支。服务端、前端及 Cordis host 必须同批更新，使用旧接口的外部调用方需要迁移；不能把本次改动描述为所有旧 API 完全兼容。

| 旧用法 | 当前用法 |
| --- | --- |
| `GET /visualizations?contract_version=...` 获取不同目录 | `GET /visualizations` 获取完整统一描述符，不再按参数协商 |
| `GET /files/{id}/preview` | 统一入口的 `operation: page`，分页参数放入 options |
| `POST /files/molecular-preview/prepare` | 统一入口的 `operation: prepare`，主文件使用路径中的不透明 ID |
| `POST /files/{id}/visualization-v2` | 统一入口的 `operation: preview` |
| `POST /files/{id}/visualization-content` | 统一入口的 `operation: bytes` |
| 原科学 `/files/{id}/visualization` 顶层变量参数 | 同一路径，增加 operation，变量、切片及 HDU 等放入 options |
| `/files/{id}/visualization-jobs/...` | `/files/{id}/visualization/jobs/...` |

表内省略共同的 `/api/v1` 前缀。原上传、下载、签名资源、数据集预览准备接口及显式 Shapefile 压缩包导入流程保留。压缩包导入可能生成文件，不应与只读可视化 prepare 混称。

AgentLoop、PlanActFlow、FastAPI、分析工具目录和 SSE 事件结构没有被替换。

## 五、读取效率与生命周期修正

- 原始字节每次底层读取最多 8 MiB，HTTP 分块最多 1 MiB；大于 1 MiB 时使用临时磁盘缓冲，最多两路并发，直到流关闭才释放名额，减少跨隔离读取器的重复调用。
- 文本继续按 64 KiB 页读取，CSV 按 128 KiB 页读取，FASTQ 仍可前缀抽样；这些预算不等于源文件总大小上限，不把所有旧插件统一收紧到 64 MiB。
- 分子 prepare 改为只读取元数据，真实字节读取仍受原 50 MiB 边界限制。
- 前端隐式版本缓存绑定本次加载的 AbortSignal，关闭重开不继承过期缓存；分页和切片继续显式维护同一读取版本。
- 基础预览组件也迁入共享 runtime，复用插件开关、预算、版本、取消和资源释放检查。
- 修正 OBJ 首次挂载未渲染及 Blob URL 无扩展名导致的识别问题。
- 交付前重新校验完整插件声明、enabled、文件版本及目录 revision；原生读取取消后，底层工作完成前不提前释放并发名额。
- 修复请求断连与字节结果完成交接之间的竞态：未交付结果从后台任务统一回收，重复取消不会打断后台清理；响应头或正文发送失败也会关闭临时流并归还并发名额。正常传输期间不会提前释放。

本次没有跨机器或真实大文件耗时基准，不宣称所有格式统一后都会加速，也不提供没有测量依据的提速百分比。

## 六、授权、安全与已有功能边界

- 数据集仍按当前 `DATASET_HOST_PATH_ALLOWLIST`、只读来源和不透明文件引用读取，真实宿主路径不进入浏览器、URL 或浏览器存储。
- `resource_id` 不是任意同用户文件访问权。数据集 Shapefile 校验 owner、dataset、anchor 和完整逻辑路径关系；HTML/Markdown 的伴随资源须有同会话或明确内部目录证据，无证据时不按同名猜测绑定。
- 每次读取同时校验文件归属、私有 Spill 类型、插件匹配及声明操作；共享能力标记不等于公开文件，也不能替代签名或身份授权。
- FastQC 仍需明确确认才启动；插件停用后仍可取消本人已有任务。
- 没有更换沙箱隔离边界，原 NetCDF/FITS/FASTQ 科学语义和显示限制保留。办公预览增强调整了 PDF 导出与页数限制，仍受原容器无网络、只读来源及进程／内存／超时预算约束。
- 未新增通用浏览器 range/tile/chunk 服务、任意目录/外链资源集、在线底图、DICOM 转换或 Office 编辑。

## 七、PDF 与办公文档阅读增强

### PDF、Word、PowerPoint 高清显示

- PDF.js 将文档点值换算为 CSS 尺寸，绘制密度独立适配 DPR，支持 50%–400% 缩放、适应宽度／整页和全屏；缩放及显示器密度变化会重新绘制。
- 单画布最多 8M 像素、最长边 8192，临时画布绘制后释放。达到预算时提示清晰度限制，不偷偷改变用户选择的缩放比例。
- LibreOffice 同时使用私有 profile 与现代 CLI 过滤选项，兼容旧转换器；固定无损图片压缩、不降采样、请求嵌入字体，不接受文件提供的转换参数。
- PDF 读取／解析前仍限制 5 MiB，最多解析 1000 页，超过 100 页用 pypdf 保留前 100 页并标记抽样，再次核验输出预算。超限明确失败，不通过降低画质掩盖预算问题；缺失字体仍可能替换。

### DOCX 可选择文字的隔离阅读

- 锁定 `docx-preview@0.4.0` 与 `jszip@3.10.1`，提供本机 HTML 排版、文字选择及 50%–300% 缩放。复杂文档仍可切换原 Word 转 PDF 阅读。
- 文档在 opaque-origin iframe 内排版，不开放同源权限。可信阅读脚本仅从固定同源地址有界读取，拒绝重定向并校验类型，以 nonce 执行；用户文档通过消息传递，不拼入页面源码。
- 修复放大后居中布局造成左侧内容无法滚达的问题：窄页保持居中，200%／300% 放大后两端都可横向滚到，不改变实际缩放比例；已加入不同 DPR 的几何回归。
- ZIP 最多 16 MiB／512 条记录，解包最多 32 MiB，XML 单项 2 MiB、合计 8 MiB、深度 128；流式核验实际解压长度。拒绝路径穿越、宏、嵌入对象、外部资源和 HTML altChunk。
- 图片仅允许核验后的 PNG／JPEG、合计 16M 像素；不加载嵌入字体。限制 XML／DOM 排版节点和分页放大，卸载时销毁 iframe 和相关资源。

### ONLYOFFICE 本机只读阅读

- 已实现统一 `prepare → resources` 分派、租约存储、独立 iframe、状态检查、原件授权读取和关闭撤销，不再只是未就绪占位组件；未配置服务时仍明确提示不可用。本次同时修正了遗留的“预留／尚未接通”插件文案，默认关闭策略不变。
- 通过同一 Compose 栈的可选 `office` profile 引入文档服务与精确路由网关，不增加第二套应用栈或第二个前端发布端口。Office 使用独立本机来源，不在宿主页执行第三方 SDK。
- 文档服务只加入独立 internal 网络，不挂载数据集、宿主文件或 Docker socket；网关只允许必要路径。前后端阻止 Office 来源、`Origin: null` 等访问主应用 API，不将来源隔离当作授权替代品。
- 原件最多 64 MiB，复用统一有界读取；每次核验 owner、私有文件边界、文件版本、插件启停和目录 revision。最多 16 个活动租约，每个寿命 15 分钟、最多 8 次原件 GET；Redis 不保存文件内容，令牌键使用散列。
- 配置 JWT 固定 `view`，禁编辑、下载、打印、复制、评论、修订、宏及插件，不提供原件保存回调。关闭／切换会撤销原件授权；已看到的内容、截图和上游转换缓存并非可撤销 DRM。
- 本地双密钥只保存在 `.local/office-viewer.env`，同时排除 Git 和 Docker 构建上下文；加载时不作为 shell 代码执行。后端对自有能力 URL 脱敏，前端按归一化路径关闭 Office 能力访问日志，覆盖编码路径；分享反向代理异常和第三方诊断日志前仍须审查。
- 适用范围为当前 Compose 固定的 `AUTH_PROVIDER=none` 本机部署。自定义 SSO 模式尚未适配，私有读取可能被登录中间件拦截；本次没有放宽认证规则或声称支持该模式，详见专项文档的限制说明。

部署、依赖、安全限制及原实施验收分别见 [PDF／Office 清晰阅读改造](pdf-office-preview-quality.md) 和 [ONLYOFFICE 本机只读阅读](onlyoffice-local-viewer.md)。本轮 Git 提交工作没有执行这些文档中的部署命令。

## 八、本批附带的分析文档

新增 [SciDB 文件格式与可视化差距分析](scidb-format-visualization-gap-analysis.md)，记录此前基于用户格式统计表所做的需求归纳、候选读取器和基础能力建议。

这是一份待确认的选型分析，不表示压缩包浏览器、结构树、音视频、EDF/BDF、CZI/SCN、Origin 等建议已经实现或安装。本次提交仅将该已有文档纳入版本控制，不重新分析源工作簿、不扩大开发范围；统计值和上游链接仍以原报告的口径和时间为准。

## 九、验证结果

### 已完成：字节响应断连清理修复

`file_routes.py` 原先在等待 `request.is_disconnected()` 时，后台读取可能已完成；取消异常发生后，局部变量尚未接收结果，清理又跳过已完成任务，导致临时流和两路字节预览名额泄漏。

修复将未交付结果的回收绑定后台任务完成状态；未完成任务在取消前注册清理回调，并保护后台清理不被重复父任务取消打断。专用流响应在 ASGI 调用的 `finally` 中幂等关闭结果，覆盖生成器尚未开始、响应头发送失败及正文发送失败，不依赖背景任务一定运行。后台清理异常不会替换原请求取消异常，日志只记录错误类型。

新增 13 个确定性回归场景，使用真实字节读取服务、临时流及并发槽，靠事件同步控制竞态，不依赖固定延时：

- 断连检查期间结果完成，以及同一时机的父任务取消。
- worker 吞取消后返回字节、清理抛错、父任务重复取消。
- 正常响应正文及背景任务的所有权移交、幂等释放。
- ASGI 2.4 响应头／首个正文发送时的断连与取消，共 4 个场景。
- 完整成功发送，以及连续两次断连后仍可正常取得两路并发名额。

先在未修复路由运行首批 8 项测试，得到预期的 **6 失败、2 通过**；应用修复并加入 ASGI 场景后，**13 项全部通过**。独立源码复审未发现本修复的阻塞问题。

最终复核又在 ONLYOFFICE 原件流发现同类问题，已抽出接口层 `visualization_streaming.py`，由公共预览和 provider 原件路由共享回收与响应封装。新增 6 项办公生命周期回归；修复前相关用例出现 5 项预期失败，修复后全部通过，原公共 13 项保持通过。另增加私有接口 schema 回归：三个仅供网关使用的端点不进入 OpenAPI，消除新引入的重复 operation ID 警告。

### 本轮最终验证

以下只记录并行开发结束后，本次提交工作重新运行的检查；此前滚动检查不重复累计。

| 检查 | 本轮结果 |
| --- | --- |
| 前端类型检查、生产构建、单元测试 | 类型检查和构建通过；341 项测试通过 |
| 后端全量 pytest | 2,374 通过、32 条件跳过、0 失败 |
| 沙箱严格全量 pytest | 552 通过、0 跳过、0 失败 |
| Cordis host 构建及测试 | 构建通过；52 项测试通过 |
| 合成浏览器回归 | 47/47 通过，含 DOCX DPR 1／2／3 下 50% 居中、200%／300% 两侧可达回归；无外部／异常请求、页面／控制台错误及 Worker 泄漏 |
| 三端契约副本、Compose 和脚本检查 | 35 个适配器副本一致；默认 7 服务及可选 Office 配置、相关脚本语法通过 |
| 前端／Office 网关 Nginx 检查 | 两份配置语法通过；真实临时 Nginx 验证普通及编码 Office 路径不写访问日志，其他路径保留日志 |

自动化单元／集成测试合计 **3,319 通过、32 跳过、0 失败**，浏览器场景另列。后端 32 项跳过为 22 项在线文件／沙箱测试、3 项显式真实 Mongo 测试、7 项已被替代的旧快速路径测试；仅保留 1 个既有 Jupyter OpenAPI 警告。沙箱保留 27 个科学依赖／框架警告；前端仍有上游依赖及打包体积等构建提示。

后端及沙箱使用现有 Compose 镜像的一次性 `--rm --no-deps` 测试容器，代码只读挂载、使用隔离测试配置，依赖只装入临时容器；没有构建、替换或重启运行服务。严格沙箱检查启用 `AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1` 与 `AI_DATASEEK_REQUIRE_VISUALIZATION_V2=1`，不依赖缺库跳过。

真实模型、生产数据库、需要写业务夹具的 HTTP 和 ONLYOFFICE 原生三格式上传验收不在本轮重新执行范围。[高清文档记录](pdf-office-preview-quality.md)、[ONLYOFFICE 记录](onlyoffice-local-viewer.md) 和 [统一协议验收记录](visualization-unification-acceptance.md) 中的实机部署、原件摘要、业务数量快照和清理情况属于其原实施记录，不冒充本次重新验证。

合成浏览器回归使用实际 SDK／绘制和受拦截的合成读取响应，不访问用户文件。10 GB CSV／FASTQ 场景仅模拟大小元数据和有界响应，不实际读取 10 GB，也不是大文件性能基准。通过测试不等于保证任意复杂办公文档均完全保真。

## 十、提交卫生

- 配置密钥、日志、依赖目录、临时报告和构建产物不纳入提交；三份规范生成的源码副本按设计保留。
- 未引用的 `frontend/src/visualizations/extended/DocumentPreview 2.vue` 与提交基线旧页面内容完全一致，作为本地备份保留，既不删除也不提交。
- 20 个旧清单文件的删除是已列明的重命名迁移，不涉及用户数据，历史版本仍可从 Git 恢复。
- 默认提交及推送目标为 `v2`，不修改 `main`，不强制覆盖远端分支。

## 相关文档

- [统一可视化插件协议](visualization-plugin-contract.md)
- [统一协议改造与验收记录](visualization-unification-acceptance.md)
- [21 组工具集成范围](scientific-visualization-integration.md)
- [PDF／Office 高清阅读](pdf-office-preview-quality.md)
- [ONLYOFFICE 本机只读集成](onlyoffice-local-viewer.md)
- [SciDB 格式增量建议](scidb-format-visualization-gap-analysis.md)
