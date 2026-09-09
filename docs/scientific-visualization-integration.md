# 21 组科学与办公可视化工具集成

日期：2026-09-09。项目用途：用户已确认非商业研究。

## 集成方式

本次不是嵌入外部网站，也不是把插件清单当作可执行代码。保留已有 14 个 Visualization Contract v1 插件，新增 20 个 v2 描述符；21 组上游工具中的 3Dmol 已由原有 `molecular` 插件提供，继续复用。因此总目录为 **34 个插件注册，而非 21 个新增注册**。

每个注册仍由真实 Cordis Context/Fiber 管理，使用同一启停偏好、原子目录重载和销毁机制。Node、Python、TypeScript 三端校验固定的 `adapter / reader / data_kind / view_kind / contract_version` 组合。清单不能指定脚本、URL、组件路径、主机路径、命令或凭据。

AgentLoop、PlanActFlow、工具调度和 SSE 消息结构没有替换；可视化读取不会向模型发起请求。数据集只读挂载、主机路径白名单和浏览器不接收真实主机路径的边界保留。

## 21 组能力与明确边界

| # | 上游工具 / 插件 ID | 本次可用范围 | 不包含或需要显式条件 |
|---|---|---|---|
| 1 | [Plotly.js](https://github.com/plotly/plotly.js) / `viz-plotly` | CSV/TSV 选列，NPY/NPZ/MAT 数组、变量和切片；曲线、散点、直方图、热图 | 仅有界数值；不执行 pickle 或用户 Plotly 配置 |
| 2 | [H5Web](https://github.com/silx-kit/h5web) / `viz-h5web` | HDF5/NeXus/NetCDF4 内部变量树与曲线、热图切片 | 不支持经典 NetCDF3；禁止 HDF5 外部链接和 VDS |
| 3 | [vtk.js](https://github.com/Kitware/vtk-js) / `viz-vtk` | 小型 ASCII VTP/VTI、STL、OBJ；网格及体数据切片 | 8 MiB 输入；不支持压缩/附加二进制 VTK、VTU、OpenFOAM |
| 4 | [JSROOT](https://github.com/root-project/jsroot) / `viz-jsroot` | 隔离 uproot 提取 TH1/TH2/TGraph，保留真实分箱后重建图形 | 不执行 TExec、TF1、Canvas 绘图脚本或文件自带 Streamer 代码 |
| 5 | [3Dmol.js](https://github.com/3dmol/3Dmol.js) / `molecular` | 复用已符合 Cordis 规范的分子／晶体结构预览 | 既有格式和行为保留，不重复造新注册 |
| 6 | [RDKit](https://github.com/rdkit/rdkit) / `viz-rdkit` | SDF/MOL/SMI/SMILES 二维结构、分子序号选择和元数据 | 隔离生成 PNG，不连接化学数据库 |
| 7 | [Mol*](https://github.com/molstar/molstar) / `viz-molstar` | 本地 PDB/mmCIF 生物大分子结构 | 禁外部下载、工作区导入；有限原子预算 |
| 8 | [NMRium](https://github.com/cheminfo/nmrium) / `viz-nmrium` | 已处理 1D JCAMP-DX，明确 ppm 和观测核 | 仅 AFFN/PAC 子集；不自动处理 FID、二维谱或压缩 JCAMP；只读工具栏 |
| 9 | [OpenLayers](https://github.com/openlayers/openlayers) + [geotiff.js](https://github.com/geotiffjs/geotiff.js) / `viz-openlayers` | GeoJSON；明确 EPSG:4326/3857 的轴对齐 GeoTIFF 第一波段 | 不猜测 CRS，不加载在线底图 |
| 10 | [MapLibre](https://github.com/maplibre/maplibre-gl-js) + [deck.gl](https://github.com/visgl/deck.gl) / `viz-maplibre` | GeoJSON 点、线、面 GPU 地图 | 离线空白底图；不接受远程样式、瓦片或任意属性 HTML |
| 11 | [Cesium](https://github.com/CesiumGS/cesium) / `viz-cesium` | GeoJSON 与白名单 CZML 位置／轨迹 | 不调用 ion，不下载影像、地形、3D Tiles 或外部模型 |
| 12 | [Aladin Lite](https://github.com/cds-astro/aladin-lite) / `viz-aladin` | primary HDU 二维 FITS + RA/DEC WCS 天空图 | 独立离线 frame；不联网取 HiPS/catalog；非 WCS 图像可继续用原 FITS 插件 |
| 13 | [MetPy](https://github.com/Unidata/MetPy) / `viz-metpy` | CSV/TSV 探空 Skew-T 图 | 必须明确气压、温度、露点列及各自单位，不自动猜测 |
| 14 | [IGV.js](https://github.com/igvteam/igv.js) / `viz-igv` | BED 注释、VCF 核心变异轨道 | 必须提供真实参考组装的 chrom.sizes；无自动 hg38 下载；不含 BAM/CRAM/bigWig |
| 15 | [Viv](https://github.com/hms-dbmi/viv) / `viz-viv` | 单文件 OME-TIFF，多通道及 Z/T 切片 | 不支持多文件外部引用或目录型 OME-Zarr；不支持交错 RGB |
| 16 | [NiiVue](https://github.com/niivue/niivue) / `viz-niivue` | 未压缩单文件 NIfTI-1、内嵌 raw NRRD | 不支持压缩/外链 NRRD、DICOM 转换；仅研究，不作诊断 |
| 17 | [FastQC](https://github.com/s-andrews/FastQC) + [MultiQC](https://github.com/MultiQC/MultiQC) / `viz-fastqc` | 显式启动真实 QC 作业，显示模块表和 MultiQC 汇总 | 不自动执行；64 MiB 未压缩 FASTQ；可取消，结果私有持久化 |
| 18 | [PDF.js](https://github.com/mozilla/pdf.js) / `viz-pdfjs` | PDF 逐页、缩放、本地字体/CMap/WASM | 无脚本／表单动作层；不含 OCR；最多 1000 页、按页分配画布 |
| 19 | [LibreOffice Writer](https://github.com/LibreOffice/core) + PDF.js / `viz-word` | DOC/DOCX/ODT 隔离转 PDF 后逐页显示 | 禁宏、外链和嵌入对象；字体/分页可能与原版有差异；不提供编辑 |
| 20 | [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) / [xlrd](https://github.com/python-excel/xlrd) / `viz-excel` | XLSX/XLS 工作表、行列分页、缓存值和公式文本 | 不重算公式、不执行宏、不更新外部数据；不用过时 SheetJS npm 包 |
| 21 | LibreOffice Impress + PDF.js / `viz-powerpoint` | PPT/PPTX/ODP 静态逐页预览 | 不播放动画、音视频，不显示演讲者备注，不提供编辑 |

“集成工具”不代表支持该上游软件的全部格式和功能。多文件关联、索引伴随文件、目录型分块读取和在线底图需要后续独立授权资源协议，不能通过任意路径或外链绕过当前边界。

## 协议 v2

- 旧客户端 `GET /api/v1/visualizations` 默认只得到原 14 个 v1 描述符；新客户端传 `contract_version=2`，得到 v1+v2。未知版本拒绝。
- v2 描述符保留原有字段，增加版本为 2、`data_kind: extended` 及固定 `v2-*` 适配器；没有自由执行入口。
- `POST /api/v1/files/{opaque_id}/visualization-v2`：`{plugin_id, version?, kind?, options:{...}}`，返回带 `type/reader/kind` 的表格、数值、树、PNG/PDF 或 QC 数据。
- `POST /api/v1/files/{opaque_id}/visualization-content`：只供固定 binary 适配器读取有界原始字节；同样检查文件 owner、插件启用状态和版本。不返回主机路径、签名绕过链接或第三方 URL。
- 输入格式由后端从已授权文件名推导，不接受客户端路径或读取器名称。不同读取器有各自 options 白名单；HDF5/ROOT `path` 只表示文件内部节点。
- 读取前后检查文件版本和插件状态／目录 revision；切换、卸载、取消或停用后，过期结果不得挂载。

### 预算含义

v2 原始文件输入最大 64 MiB，且服从更小的插件/渲染器限制。`max_output_bytes` 限制**派生 JSON/媒体载荷**，最大 8 MiB，不把原始 binary 输入误称为 8 MiB 输出。PNG/PDF 媒体在 worker 内最多 5 MiB；数组最多 16384 个数值；表格窗口 200×100；HDF5 树 256 节点／8 层。QC 模块表独立限制为每模块 1000×20、最多 32 模块。

复杂读取器只在无网络、非 root、只读根文件系统、无数据集挂载、无凭据的短命容器中运行。v2 每容器上限 1 GiB 内存、1 CPU、96 PID、512 MiB 临时空间、50 秒外层期限；运行容器另有不依赖后端的 55 秒硬时限及 Docker 自动删除，不改变 v1 更小的资源配置。取消后仍保留 admission，直到原生读取线程/容器完成清理。

### QC 作业

`/files/{id}/visualization-jobs` 提供显式启动、列表、状态、取消和结果接口，复用现有 AnalysisJob 状态机和 Spill Artifact Store。内部使用带前缀的文件哈希作用域，不创建虚假的 Agent 会话，也不改 AgentLoop/SSE。结果受 owner+文件作用域、私有 Artifact、实际字节数、摘要、文件版本和插件 revision 检查；取消仍允许在插件停用后执行。离开当前 QC 视图会请求取消未完成作业。

## 本地与兼容性

使用原 Compose 项目和 `./run.sh` 更新；本机仍为 `127.0.0.1:7001`，没有新增公网端口、预览服务或外部账户。

所有新增前端 SDK 按需加载。Cesium、MapLibre、Aladin、PDF.js 的 Worker/字体/WASM 及图形库分发资源从本机提供。具有全局 Worker 池的地图 SDK 使用独立本地 iframe 生命周期，关闭视图销毁其运行环境，不干扰其他插件。大工作台独立预构建，NMR 工作区偏好仅在内存中存在，不读取历史 localStorage。前端构建运行时升级到 Node 24，以满足当前 PDF.js/Cesium 的版本要求；构建堆限制为 4 GiB，运行页面仍由原 nginx 提供。

Cesium 固定上游分发包的控件和 Worker 需要动态函数构造与本地 Blob 脚本，因此仅其独立预览环境提供这两项 CSP 许可，不修改父页面或其他插件策略。插件清单及数据仍不能提交脚本：启动代码来自受版本控制的适配器，数据经过白名单校验后通过带 source/origin/随机通道校验的消息传递；不放行外部网络域。生产 nginx 对本地 `.mjs` 返回 JavaScript 类型，缺失 SDK 资源返回 404，不回退成首页 HTML。

Sandbox 的原有锁定包版本保留，新增读取器依赖增量锁定；不要通过全局升级依赖来消除预览错误。详细输入限制见前端科学和领域适配器目录 README。

## 许可与版本

非商业用途已记录，但仍保留上游授权、署名和再分发要求。NMRium 当前锁定 0.60.0，实际依赖 nmr-load-save 0.37.x、nmr-processing 12.x 均为 MIT；未因用户确认非商业就临时升级到不同 API 的新版。Aladin 3.8.2 npm 包标示 GPL-3，FastQC/MultiQC 亦有 copyleft 条款；不能把整个集成清单统称为 MIT。精确 npm 和 Python 版本以仓库锁文件为准。此记录不取代项目未来发布/再分发前的完整许可审查。

## 验证方式

1. 插件页面确认 34 个注册，分别启停新增能力。同一文件切换多个适配器，例如 GeoTIFF 图像／地图、NetCDF 地图／曲线／H5Web。
2. 运行前端 type-check、build、测试，Cordis 主机测试，backend/sandbox 完整 pytest 和 Compose 配置检查。
3. 真实隔离网关合成文件验收由 `backend/scripts/check_extended_visualization_workers.py` 执行，不用用户数据、不调用模型。
4. 浏览器验收需区分“编译/协议测试通过”和“实际 SDK/WebGL 画面通过”。结果见[验收记录](./scientific-visualization-acceptance.md)；不能仅靠清单或 mock 宣称所有画面可用。
