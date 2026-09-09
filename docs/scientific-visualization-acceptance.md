# 科学与办公可视化插件验收记录

日期：2026-09-09。范围为[21 组集成说明](./scientific-visualization-integration.md)所列的已实现子集，不代表上游 SDK 的全部能力。

## 分层验证

| 验证层 | 已完成结果 | 说明 |
|---|---|---|
| 前端回归 | 314 项通过，类型检查通过 | 包含同文件多视图选择、停用销毁、竞态、办公读取去重和原有消息/UI功能 |
| Cordis 主机 | 36 项通过 | 真实 Context/Fiber 生命周期、严格清单、启停与版本兼容 |
| 后端全量回归 | 2242 项通过，32 项条件跳过 | 包含最终网关增强、新增读取、作业、授权及原有功能 |
| Sandbox 全量严格回归 | 542 项通过，无跳过 | 包含 130 项新读取器用例，不调用模型 |
| 新读取器真实 Docker 网关 | 9 类读取器，17 个成功场景、6 个拒绝场景、1 个主动取消 | 实际检查 24 个隔离容器，非 root、无网络、无挂载、只读根目录与资源限额 |
| 网关崩溃与自动回收 | 临时网关进程被强制结束后，阻塞读取器在 55.096 秒由独立时限终止并自动删除 | 本轮无残留；另外 30 项协议截断、取消和自动删除竞争边界用例通过 |
| 本机真实 HTTP | 最终生产后端上，新旧目录协商、11 个 v2 结构化视图、PDF 原字节以及旧接口通过 | 另有 18 项拒绝用例，含严格布尔确认；完整 QC 创建、成功、私有结果读取、取消和停用门禁均通过 |
| 前端生产构建 | 本机及 Node 24 Docker 构建通过 | Docker 构建显式限制 4 GiB 堆；生产仍为 nginx，不新增 Node 服务 |
| 实际浏览器 | 23 个场景全部通过 | 22 个新适配场景加原有 3Dmol；真实 SDK 绘制与关闭行为，详见下表 |
| 上线检查 | 原 Compose 项目、仅 `127.0.0.1:7001` 对外发布前端 | nginx 配置检查通过；页面显示 34 个插件；模块 Worker 返回 JavaScript，固定宿主返回 HTML，缺失 SDK 返回 404 |

浏览器验收使用真正的 Vue 组件和锁定的 SDK、真实 Canvas/SVG/WebGL 绘制；读取接口返回合成测试数据，并拦截所有请求，不访问用户文件、不连接在线底图或第三方服务。这个测试层验证浏览器实际绘制和生命周期；真实解析、HTTP 授权以及 Docker 隔离分别由上述其他测试层覆盖，不能混称为单一端到端测试。

验收不以“出现 canvas 元素”为通过条件：还检查实际像素或绘图调用、视图尺寸、关闭后停止绘制、Worker 和 iframe 释放、非预期请求和控制台错误。测试环境为本机 Chromium/Chrome 无头浏览器和 SwiftShader WebGL，使用合成文件，不代表所有用户硬件或所有上游格式均已覆盖。

| 浏览器组 | 最终通过场景 | 特别检查 |
|---|---|---|
| 科学数值与结构（6） | Plotly、H5Web、vtk.js、JSROOT、Mol*、NMRium | 有效曲线/分箱/体切片/分子绘制；可用绘图区高度；NMR 历史工作区污染不影响当前输入 |
| 地理/天文/生物影像（9） | OpenLayers GeoJSON、OpenLayers GeoTIFF、MapLibre/deck.gl、Cesium GeoJSON、Cesium CZML、Aladin FITS、IGV BED、Viv OME-TIFF、NiiVue volume | MapLibre 拖动后父层 deck 相机同步；Cesium 完整图层加载与定位，不只显示标记或空画布 |
| 办公与报告（7） | PDF.js、Word、PowerPoint、Excel、RDKit、MetPy、FastQC/MultiQC | PDF 字形像素与缩放；Excel 仅一次初始请求及字面公式；MetPy 明确列；QC 必须点击启动后运行 |
| 原有分子预览（1） | 3Dmol | 原授权下载流程、真实 2D 键线和 3D 绘制；连续两次打开/关闭无新资源累积 |

新增场景最终均无控制台错误、外部请求尝试、非预期请求或卸载后的运行 Worker；独立框架移除后停止绘制。原有 3Dmol 上游保留一个固定的 SurfaceWorker Blob 库级缓存，连续两次挂载为同一条，不累积、没有运行 Worker；本轮保留此兼容基线，不将其误报为“所有旧 SDK 对象全部清零”。

## 业务数据保护

HTTP 验收只创建带专用前缀的合成文件，并在 `finally` 按本轮确切 ID 删除；作业与 Artifact 按本轮文件哈希作用域通过已有 `delete_owner` 清理。临时改变的 FastQC 开关恢复原值。

该轮共清理 13 个上传、2 个作业、1 个 Artifact。前后文件、会话、会话事件、模型轨迹、Token 记录、数据集、作业和 Spill 数量一致；会话、作业和 Spill 元数据摘要一致。没有访问已有用户文件或调用模型。

更新最终后端后重复执行相同 HTTP 验收，清理与快照校验再次通过。期间用户新增的正常业务数据保留，不与更早测试基线混比。

## 可复现入口

- `frontend`：`npm run type-check`、`npm test`、`npm run build`。
- `plugin-host`：`npm test`。
- `backend` 与 `sandbox`：各自 `uv run pytest`；本机未安装 uv 时使用现有 Compose 镜像的一次性测试容器，不修改运行中的服务依赖。
- `backend/scripts/check_extended_visualization_workers.py`：真实隔离读取器与主动取消检查。
- `backend/scripts/check_extended_visualization_http.py`：本机 HTTP 回归及精确临时数据清理。
- `frontend/tests/browser/domains-smoke.mjs`：科学、领域、办公 SDK 的合成浏览器场景；需要 Playwright、Chromium 和已经构建的本地 SDK 资源。`VISUALIZATION_BROWSER_CASES` 可按逗号分隔的场景名选择子集。
- `./run.sh config --quiet`：部署配置检查；服务更新继续使用 `./run.sh`，不创建第二个栈或前端端口。

## 已知使用边界

本次所有新增预览均在本机资源边界内运行。大文件分块、多文件伴随索引、在线底图、DICOM 转换、完整 NMR 原始数据处理、Office 编辑或动画均未默认为可用功能，详细边界见集成说明。前端构建仍会提示某些按需 SDK 块较大；这不表示它们被首页同步加载，也不应通过取消资源预算来规避。

独立硬时限针对已经启动的读取容器。Docker 的 create/start 不是原子操作；后端恰在二者之间被强杀时，可能留下没有运行进程的 created 元数据，需后续运维精确回收，不能声称任意崩溃点都绝无 Docker 元数据残留。

非商业用途不免除许可证要求。锁定版本和许可说明已写入集成文档，未来发布或再分发仍应核对上游授权、署名与源码义务。
