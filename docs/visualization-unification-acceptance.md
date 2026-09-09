# 可视化插件统一协议改造与验收

日期：2026-09-09。当前开发规范见[统一协议](visualization-plugin-contract.md)，工具的实际格式范围与许可证见[21 组工具集成说明](scientific-visualization-integration.md)。本记录取代旧验收记录作为本次协议迁移的验收依据，不扩大任何上游 SDK 的能力承诺。

## 本次改动

- 34 个插件统一为严格 `contract_version: 2`，去掉 `data_kind`、适配器和清单文件名的 `v2-` 前缀。不再并存两套公有协议或做版本协商。
- 一份 `contracts/visualization-adapters.json` 定义批准的 adapter、reader、视图和能力组合，生成 Node、Python、TypeScript 的构建副本。同步检查已接入回归脚本，副本漂移会失败。
- `capabilities` 明确操作、整文件/分页/前缀读取模式与共享能力。所有组件接收 `{file, plugin}`，统一 SDK 负责读取、结果验证、版本、预算和取消。
- `POST /api/v1/files/{id}/visualization` 按 `bytes / page / preview / prepare` 分派；结构化结果统一包含文件版本、目录 revision、类型化 payload、metadata、warnings 和 sampled。字节流保留二进制，不套 Base64。
- 质控作业使用 `/api/v1/files/{id}/visualization/jobs`，仍复用 AnalysisJob 和私有 Spill Artifact；明确确认才启动，停用后仍可取消本人已有任务。
- 旧可视化专用接口移除；普通文件上传、下载、签名资源以及显式 Shapefile 压缩包解压导入流程保留。私有解析 worker 的内部格式不是第二套公有插件协议。
- 插件 ID、发布版本、格式匹配、优先级、默认开关和原有预算逐项保留；已有启停偏好不重建。管理页改为显示能力而非 v1/v2 分类。

AgentLoop、PlanActFlow、模型调用、SSE 事件格式、工具目录和只读数据挂载边界未改动；不新增公网服务或第二个 Compose 栈。

## 回归结果

| 层次 | 最终结果 | 覆盖说明 |
|---|---|---|
| 前端 | 类型检查通过；324 项单元测试通过；本机和 Docker 生产构建通过 | 原 UI、消息、文件生命周期及新增统一 SDK 回归 |
| Cordis | 52 项通过，0 跳过 | 真正的 Context/Fiber 生命周期、严格规范、生成副本漂移与目录隔离 |
| 后端全量 | 2306 项通过，32 项条件跳过，2 条警告 | 统一入口、原功能、科学读取、分页、分子准备、资源关系、作业与安全边界 |
| 沙箱全量 | 542 项通过，0 跳过，27 条警告 | 按镜像原科学依赖优先级执行；不改解析器、依赖锁文件或隔离策略 |
| 真实浏览器 | 30 个场景一次运行全部通过 | 使用实际插件清单的能力和预算，加载真实 SDK 并检查绘制、取消、释放和非预期请求 |
| 实际 HTTP：通用/科学/办公/QC | 统一目录、11 个结构化视图、PDF 原字节、CSV/文本/下载和 NetCDF 曲线通过；20 项拒绝检查通过 | 完整 QC 成功、私有结果、取消、停用门禁；旧入口返回 404 |
| 实际 HTTP：原科学预览 | NetCDF 地图/曲线、两组 FITS 图像/曲线、FASTQ 质量抽样共 7 次预览通过；5 项拒绝检查通过 | 保留变量、坐标、维度、切片、抽样和版本语义 |
| 实际 HTTP：数据集直接预览 | TIFF、CSV、NetCDF、FITS、Shapefile 配套文件和本机 HOST_PATH Markdown 共 6 类检查通过；3 项非法路径拒绝通过 | 不上传源文件；同一文件重复点击复用引用；仍通过允许目录与只读隔离检查 |
| 本机上线 | 已同步更新原 Compose 的 backend/frontend；nginx 与 Compose 配置检查通过 | 实际页面显示统一协议和 34 个插件，仍仅发布 `127.0.0.1:7001` |

浏览器 30 场景为：OpenLayers GeoJSON/GeoTIFF、MapLibre/deck.gl、Cesium GeoJSON/CZML、Aladin FITS、IGV BED、Viv OME-TIFF、NiiVue、Plotly、JSROOT、H5Web、VTK、Mol*、NMRium、3Dmol、PDF.js、Word、PowerPoint、Excel、RDKit、MetPy、FastQC，以及原 CSV、NetCDF 曲线/地图、FASTQ、Shapefile、图片、OBJ。

浏览器读取响应采用合成夹具并拦截外部请求，不访问用户文件；真实解析与权限由实际 HTTP 和沙箱测试分别覆盖。这不是“所有格式、所有硬件都已端到端验证”的声明。10 GB CSV/FASTQ 场景使用文件大小元数据和有界合成返回，验证不请求整文件，不实际下载 10 GB 数据。

最终浏览器原始报告位于本次工作机临时目录 `dataseek-visualization-browser-US9DSK/results.json`，该次运行 30/30 通过；临时目录可能被系统清理，可用版本化测试脚本复跑。

## 效率与兼容修正

- 字节输入按最多 8 MiB 读取，HTTP 按 1 MiB 交付；大于 1 MiB 的文件提前使用临时磁盘缓冲，最多两路并发，直到流关闭才释放名额。避免每 1 MiB 都重复调用隔离文件读取器；不把所有旧插件错误地收紧到 64 MiB。
- CSV 继续每页最多 128 KiB，文本每页 64 KiB；FASTQ 继续前缀抽样。这些预算不当成源文件总大小限制。
- 分子准备只读取元数据，不为检测格式提前下载整个文件；真实内容读取仍有原 50 MiB 限制。
- 前端文件版本缓存跟随每次加载的 AbortSignal，关闭重开不会永久携带过期版本；分页和科学切片仍显式保持同一次读取的版本。
- 修复原 OBJ 首次挂载未启动渲染，以及 Blob URL 无扩展名导致读取器识别失败的问题；已检查真实绘制。
- 最终交付前同时复核当前插件 enabled、完整声明和目录 revision；收紧输出预算也会生效。原生读取取消后，实际任务完成前不释放并发名额。

以上是读取策略和资源上界的改进，没有做跨机器、跨文件的耗时基准，因此不宣称统一后所有格式均加速或给出百分比。

## 数据保护与已知边界

HTTP 测试累计创建并精确清理 18 个专用测试上传、2 个作业、1 个 Artifact；恢复 FastQC 与 NetCDF 地图的原开关。测试前后记录的文件、会话、事件、模型轨迹、Token、数据集、作业、Artifact 计数及对应快照摘要一致；没有发起模型调用或创建 Agent 会话。只删除本轮确切 ID 的合成数据，不触碰用户文件。

数据集直接预览仅按原机制创建/续期不透明的短期文件引用，保存在独立引用集合中并自动过期；不创建普通文件、会话或模型调用记录。重启前 25 个会话均已完成，活动 AnalysisJob 为 0；没有中断正在执行的分析任务。

关联文件必须有可验证关系：数据集 Shapefile 使用私有 owner/dataset/anchor 和完整路径 stem，不能按同名跨数据集读取。HTML/Markdown 保留同会话资源及有明确内部目录的历史资源；无目录、无会话、无关联证据的独立上传不自动按文件名猜测绑定，需要后续显式资源集合设计。服务端内部路径只用于关系判断，不返回浏览器、不作为任意主机文件读取入口。

本次不新增通用浏览器 Range/tile/chunk 服务、任意远程资源、在线底图、DICOM 转换或 Office 编辑。旧 SDK 的既有构建警告及 3Dmol 固定库级 Blob 缓存仍保留：缓存不随重复挂载累积，也没有遗留运行 Worker。

## 复验入口

- `node scripts/sync-visualization-contract.mjs --check`
- `bash scripts/check-regressions.sh --containers`（已有镜像的一次性测试容器；不更新运行服务）
- `frontend/tests/browser/domains-smoke.mjs`（需要本机 Playwright、Chrome 和已构建 SDK 资源）
- `backend/scripts/check_extended_visualization_http.py`
- `backend/scripts/check_visualization_http.py`
- `backend/scripts/check_dataset_file_preview.py`

实际 HTTP 脚本会操作自身专用测试夹具并负责清理，不能与会修改业务记录的工作并行跑，否则只读快照完整性检查会如实失败。
