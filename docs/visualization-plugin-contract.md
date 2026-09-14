# Cordis 统一可视化插件协议

当前公有协议版本为严格整数 **2**，当前合并目录的 **83 个插件**使用同一描述结构、能力声明、调用入口与结果封装。版本用于契约兼容检查，不再区分基础插件、科学插件或办公插件；清单不再接受 `data_kind`，适配器键不再使用 `v2-` 前缀。本次不维护旧版可视化接口兼容分支。

完整工具范围、上游许可和实际格式限制见[21 组工具集成说明](scientific-visualization-integration.md)及[第一批格式增强](changes-2026-09-10-visualization-batch-one.md)。已有插件的稳定 ID、默认开关、优先级、权限及资源预算保持不变，已保存的个人启停选择继续生效；新增能力的插件正常递增自身发布版本并扩展经过验证的格式匹配。

## 目标和边界

文件格式、读取器、展示视图和启停状态是独立维度。同一个 NetCDF 可以由地图、曲线或 HDF5 型变量树插件展示；它们使用同一协议，只声明不同能力。不应通过新增一套协议表达新的学科或 SDK。

可视化继续使用独立 Cordis Context、服务、fiber 和 revision，不进入模型工具列表。目录重载和个人启停不改变 Agent 工具 catalog digest，不替换 AgentLoop、PlanActFlow、SSE 或 FastAPI。读取器源码随正常镜像构建更新；现有分析沙箱依旧遵循自己的执行版本隔离。

公有契约统一不意味着必须重写所有解析器：NetCDF/FITS/FASTQ 读取器、扩展科学/Office 读取器、分页读取和浏览器解码可以保留各自私有实现与资源配置。宿主负责把已校验的私有结果转换成统一 `VisualizationResult`；旧 worker 内部版本字段不是另一套可对外选择的插件协议。

## 使用方式

1. 打开 `/plugins?tab=renderers`，按场景启停插件。当前本机无登录部署采用共享系统身份，界面标记“本机共享配置”；后端偏好仍按身份存储，不接受请求指定其他身份。
2. 在文件面板的“可视化”下拉框中选择匹配文件的已启用视图。停用的插件不再匹配，也不回退到未注册的硬编码组件。
3. 科学视图按能力提供变量、维度、切片、HDU 或单位选择；完整 QC 需要明确启动，不会因打开文件自动运行。

同页起停立即更新；其他标签在获得焦点、手动刷新或最多 15 秒目录轮询后更新。关闭插件不会删除原文件或已经保存的分析结果。

### 数据集列表直接预览

数据集探查页的数据文件可通过“小眼睛”打开同一预览面板；同格式多个插件仍可切换。无匹配能力、目录或所有候选均停用时不提供预览按钮。准备期间重复点击不重复请求；切换、关闭或停用后的过期响应不得重新打开旧面板。

`POST /api/v1/datasets/{dataset_id}/files/preview` 接受登记清单中的精确逻辑路径及启用的 `plugin_id`，返回公开 `FileInfo` 与已授权的同目录 Shapefile 配套文件。该入口只是创建或复用不透明预览引用，不读取模型、不创建分析会话、不复制源数据、不向普通文件列表添加记录；引用按身份隔离，7 天后过期，可再次准备刷新。

实际读取重新核验数据集归属、登记文件、存储位置、插件状态和版本。本机路径必须满足 `DATASET_HOST_PATH_ALLOWLIST` 及 Docker-host 映射；拒绝路径穿越、符号链接和非普通文件。真实路径不返回浏览器、不进入 URL/localStorage/sessionStorage。读取受插件预算及数据集 helper 的 64 MiB 范围边界限制；分页或前缀读取不复制整个大文件。删除引用不删除源文件。

Shapefile ZIP/RAR 解压导入是现有的**显式文件导入工作流**，可能生成新文件，不属于只读可视化的 `prepare` 操作。普通 Shapefile 插件仅声明 `bytes`，依次读取已授权主文件和伴随文件。

## 一份批准规范、三个构建副本

唯一受版本控制的适配器规范是 `contracts/visualization-adapters.json`，它描述批准的 `adapter → readers / view_kind / capabilities` 组合。当前共有 77 个批准适配器键、83 个插件注册。NetCDF/FITS 曲线共用一个适配器，DuckDB／DBF／Access 共用 `database-table`，SQL／PG 目录共用 `database-dump`，BSON／Redis 共用 `database-records`，MySQL SDI／SST 共用 `physical-database`；插件均独立启停，各读取器保留严格、独立的数据语义。第五批为 68 个插件／67 个适配器；[main 迁移](changes-2026-09-11-main-visualization-migration.md)增加六个插件，[数据库文件集成](database-visualization-integration.md)再增加三个，[转储与记录扩展](database-dump-records-integration.md)增加四个，本轮[物理文件扩展](physical-database-integration.md)增加两个。所有数据库文件插件只提供有界只读预览，不提供 SQL 执行、恢复或远程连接。其他新增能力与方言限制见[第二批](changes-2026-09-10-visualization-batch-two.md)、[第三批](changes-2026-09-10-visualization-batch-three.md)、[实验数据领域](changes-2026-09-10-domain-visualization-batch-three.md)和[领域扩展路线](domain-visualization-expansion-roadmap.md)。

运行：

```bash
node scripts/sync-visualization-contract.mjs
node scripts/sync-visualization-contract.mjs --check
```

生成三个独立构建上下文内的副本：

- `plugin-host/src/visualization-adapters.generated.ts`
- `backend/app/domain/models/visualization_adapters_generated.py`
- `frontend/src/visualizations/adapters.generated.ts`

这些副本必须纳入版本控制，不手工编辑。`--check` 发现源规范或任何副本漂移时失败，已经接入回归入口与 Cordis 测试；前端独立 Docker 构建不需要越过自己的上下文读取根规范。

## 声明式描述符

受审查的插件文件位于 `plugin-host/visualizations/*.json`。描述符、能力对象、预算对象严格拒绝额外或缺失字段；目录最多 260 个插件、单文件最多 16 KiB，ID 唯一。不接受脚本、动态组件路径、远程 URL、命令或凭据。

```json
{
  "contract_version": 2,
  "id": "netcdf-map",
  "version": "1.0.0",
  "name": "NetCDF 地图",
  "description": "具有经纬坐标的 NetCDF 栅格地图视图",
  "extensions": ["nc", "nc4", "netcdf"],
  "filenames": [],
  "view_kind": "map",
  "adapter": "scientific-map",
  "reader": "netcdf",
  "default_enabled": true,
  "priority": 50,
  "permissions": ["file:read"],
  "limits": {
    "max_input_bytes": 67108864,
    "max_output_bytes": 524288
  },
  "capabilities": {
    "operations": ["preview"],
    "input_mode": "whole",
    "shared": false
  }
}
```

| 字段 | 当前规则 |
| --- | --- |
| `contract_version` | 只能是整数 `2`，不接受字符串、布尔值或其他协议版本 |
| `id` / `version` | 稳定插件 ID / 插件自身发布版本；发布版本不必与协议版本相同 |
| `extensions` / `filenames` | 小写后缀或完整文件名，无通配符、路径、重复项；声明格式必须有真实读取实现 |
| `view_kind` | `image / map / series / table / text / structure / document / tree / media / graph`，表示用户场景；`structure` 保留原分子结构含义，`tree` 用于数据结构树，`graph` 专用于科学关系网络 |
| `adapter` / `reader` | 批准的前端适配器键 / 非空读取器键；不是 URL 或可执行入口 |
| `capabilities.operations` | 非空、无重复、符合批准组合的操作列表，见下表 |
| `capabilities.input_mode` | `whole / page / prefix / window`，明确输入预算针对整文件、每页、前缀或一次选区累计取回字节；window 另有源大小硬上限 |
| `capabilities.shared` | 是否允许该插件用于已有共享预览；不是文件访问授权或数据公开开关 |
| `default_enabled` / `priority` | 未保存个人偏好时的初始值 / -1000～1000 的排序整数 |
| `permissions` | 严格 `["file:read"]`，不授予写入、任意网络或凭据 |
| `limits` | 严格正整数预算，受读取器及存储更严格硬限制；不能通过增大清单预算绕过它们 |

Node、Python、前端都从同一个批准规范校验完整组合，不能仅改 `operations` 或 `shared` 扩权。

| 操作 | 含义 | 当前插件 |
| --- | --- | --- |
| `bytes` | 读取授权文件的有界原始字节，浏览器本地解码 | 普通图片/TIFF/OBJ/HTML/Markdown/Shapefile、分子结构及 vtk/Mol*/地图/医学影像/PDF、音视频等 |
| `page` | 文本/CSV 分页，不以整个源文件大小禁止预览 | `text`、`csv` |
| `preview` | 宿主调用批准读取器，返回有界结构化结果 | NetCDF/FITS/FASTQ 采样、Plotly/H5Web/JSROOT/NMR/RDKit/MetPy/Word/Excel/PPT、结构树和压缩包目录 |
| `prepare` | 返回只读元信息，不创建分析任务或写入数据 | `molecular` 的分子元信息、OnlyOffice 已有只读资源准备，以及 TIFF/Plotly/H5Web/Viv/OpenLayers 的有界内容画像 |
| `job` | 显式长任务，复用 AnalysisJob 和私有 Artifact | `viz-fastqc`；未声明即时 `preview` |

共享预览保留原九类插件的 `shared:true`：image、tiff、shapefile、molecular、obj、html、markdown、text、csv。其余插件为 false。共享资源仍走现有签名授权或当前身份的文件/插件校验，不因能力声明跳过服务端检查。

## 统一调用与结果

所有普通读取使用：

```text
POST /api/v1/files/{opaque_file_id}/visualization
```

```json
{
  "plugin_id": "netcdf-map",
  "operation": "preview",
  "kind": "map",
  "options": {
    "variable": "precipitation",
    "indices": {"time": 0}
  }
}
```

公共请求字段为 `plugin_id`、`operation`、可选文件 `version`、可选 `kind` 和有界 `options`。`operation` 必须由当前插件声明；`job` 使用下述作业资源，不通过普通读取隐式启动。变量等参数放入 `options`，不再直接放在请求顶层。

- `page` 仅接受 `offset`、`delimiter`、`header_pending`。
- NetCDF/FITS/FASTQ 预览仅接受其明确支持的 `variable / x_dimension / indices / hdu` 等参数。
- 扩展读取器分别校验列名、单位、内部变量路径及切片等参数。HDF5/ROOT 的 path 是文件内部节点，不是主机路径。
- `bytes` 可为已授权伴随资源带上不透明 `resource_id`；服务端验证它与主文件的关系，不能读取任意同身份文件，也不能传递路径或第三方 URL。
- `prepare` 当前不接受额外参数。

非字节响应仍由应用统一 `APIResponse` 包装，`data` 为：

```typescript
interface VisualizationResult {
  contract_version: 2;
  plugin_id: string;
  version: string;       // 文件版本，不是插件发布版本
  revision: string;      // Cordis 目录代际，不含个人启停偏好
  kind: 'page' | 'series' | 'raster' | 'table' | 'array'
      | 'tree' | 'media' | 'report' | 'molecule' | 'resources' | 'features' | 'graph';
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
  warnings: string[];
  sampled: boolean;
}
```

公共封装一致，`payload` 的科学字段按批准读取器/结果类型验证，不把它解释成可执行对象。图像/PDF 使用 `media`；变量树使用 `tree`；表格、数组、采样曲线与报告有明确类型。`resources` 为协议可表达的结果类型，不代表已经实现通用多对象或瓦片服务。前端科学 SDK 内部适配函数仍可转换字段，但不会新增对外接口或客户端协议协商。

`bytes` 返回流而不做 Base64 JSON 包装，以免扩大内存占用。响应提供 `X-Preview-Version`、`X-Visualization-Revision`、`X-Visualization-Plugin`，以及 `no-store`、`nosniff`、attachment 和准确长度。统一协议允许明确的字节流和类型化 JSON 两种载荷，二者共享身份、插件、版本与预算边界。

### 目录与作业资源

| 方法 / 路径（均以 `/api/v1` 开头） | 行为 |
| --- | --- |
| GET `/visualizations` | 返回当前合并目录的完整 83 个统一描述符及 enabled，不再协商两个目录 |
| PATCH `/visualizations/{id}/state` | 仅接受严格布尔 `{"enabled":false}` 或 true |
| POST `/files/{id}/visualization/jobs` | 显式启动声明 job 的任务；当前为 FastQC，options 需明确 confirm |
| GET `/files/{id}/visualization/jobs` | 当前文件作用域作业列表 |
| GET `/files/{id}/visualization/jobs/{job_id}` | 作业状态 |
| POST `/files/{id}/visualization/jobs/{job_id}/cancel` | 取消，必须带 `X-Analysis-Job-Action: cancel` |
| GET `/files/{id}/visualization/jobs/{job_id}/result` | 成功后返回同一 VisualizationResult 封装 |

作业状态沿用 AnalysisJob，结果保存到私有 Spill Artifact Store，文件哈希作用域不伪造 Agent 会话。结果读取核验 owner、文件作用域、实际字节数、分块位置、摘要、文件版本及目录 revision。退出 QC 视图会请求取消未完成作业；插件停用或文件消失后仍允许取消本人已经拥有的任务。

旧 `visualization-v2`、`visualization-content`、`visualization-jobs` 可视化入口已收敛到上述资源结构，不作为兼容 API 保留。原通用下载、数据集引用准备与 Shapefile 导入接口属于各自工作流，不代表存在另一版可视化协议。

## 预算与隔离

预算解释由能力显式声明，不再通过版本或适配器前缀猜测：

| 读取方式 | 当前预算和限制 |
| --- | --- |
| `page` | 文本每页 64 KiB、CSV 每页 128 KiB；不限制整个源文件大小，缩小清单预算后读前拒绝越界请求 |
| `prefix` | FASTQ 采样最多 2 MiB、1,000 条完整四行记录、前 500 个位置；仅未压缩 fastq/fq、Phred+33 |
| 整文件科学预览 | NetCDF/FITS 最大 64 MiB；派生科学结果最多 512 KiB，并服从更小清单预算 |
| 扩展科学/Office/QC | 一般最大 64 MiB 输入、8 MiB 派生结果；PNG/PDF worker 媒体最多 5 MiB，数组最多 16,384 值，表格窗口 200×100 |
| 原有字节适配器 | 保留各自原预算，不统一扩大/缩小：例如 TIFF 64 MiB，部分原适配器清单 256 MiB；分子实际另受 50 MiB 限制 |
| 浏览器 SDK | 继续执行各适配器更严格的解码限制，例如 vtk/Mol* 8 MiB 输入、栅格/体素/原子数限制，详见工具说明 |
| 内容画像 `prepare` | 最多读取 64 KiB；识别 CDF/HDF5/MATLAB/TIFF 标签，不解析科学数组或 TIFF 像素；20 秒期限，两个并发槽 |
| JSON/XML/YAML 结构树 | 4 MiB 输入；源最多 4,096 节点、32 层，显示最多 256 节点、8 层；1 MiB 派生结果 |
| ZIP/TAR/GZIP 目录 | 64 MiB 输入、1 MiB 派生结果；ZIP/TAR 最多 4,096 成员、每页 200 行；只读目录不解压，GZIP/TGZ 只看首流头部 |
| 音视频 | 视频整文件最多 64 MiB，音频最多 16 MiB；原生播放取决于浏览器编码支持；仅未压缩受限 WAV 提供逐声道波形 |
| EDF/BDF `window` | 源 ≤8 GiB；每次累计读取 ≤8 MiB、单次范围 ≤1 MiB、≤128 次范围请求；≤8 通道、60 秒、总计 16,384 样本，不重采样 |
| ZIP/TAR 文本成员 | 归档 ≤64 MiB，成员展开 ≤256 KiB；每页 200 行、每行 1,024 字符；stored/deflate ZIP 或未压缩 TAR；明确 member_id + 文件 version，不落盘、不递归 |
| ASC/GRD/KML | ≤16 MiB；源网格 ≤1,048,577 单元、输出 ≤128×128；KML ≤512 要素、16,384 坐标；派生结果 ≤2 MiB |
| MCA | ASCII 单谱 ≤4 MiB、8,192 通道；只保留安全整数原始计数及明确的双点线性标定；派生结果 ≤2 MiB |
| CZI | 单文件单场景 ≤64 MiB；每次 ROI ≤1024×1024、单子块解码预算 ≤16 MiB；C/Z/T 显式选择；PNG ≤5 MiB、统一结果 ≤8 MiB |
| HDF5 / NetCDF4 window | 源 ≤8 GiB、累计读取 ≤8 MiB /128 次；单块解码 ≤4 MiB、所选块合计 ≤16 MiB、输出 ≤16,384 数值；原始索引与存储值 |
| OME-Zarr window | 本地 NGFF 0.4 / Zarr v2，精确登记 ≤2,048 对象；源 ≤8 GiB、累计读取 ≤32 MiB /256 次；单对象与单块解码各 ≤4 MiB；ROI ≤128² /64 块 |
| 大型 CZI window | CZI 1.0 DV 未压缩单 scene 0；源 ≤8 GiB、累计 ≤32 MiB /4,096 次；ROI ≤1024²；不替代原小文件 CZI 插件 |
| 仪器图像 window | ESRF EDF 内联未压缩二维帧、SPE 2.x；源 ≤8 GiB、累计 ≤32 MiB /2,048 次；显式帧与 ROI，不混同生理 EDF |
| Parquet / Arrow window | 源 ≤8 GiB、累计读取 ≤8 MiB /128 次；单文件、128 列目录、200×32 输出；元信息 ≤1 MiB、块 ≤4 MiB、声明与实际解码量均校验 |
| NeXus NXdata window | 源 ≤8 GiB、累计读取 ≤8 MiB /128 次；明确 1D/2D 信号、固定长度元信息、同文件轴/误差；值与坐标/误差合计 ≤16,384 |
| Newick 系统树 | 整文件 ≤4 MiB，输出 ≤1 MiB；单树 ≤1,000 节点／64 层，缺失枝长不补零 |
| MGF / mzML 质谱 | 整文件 ≤16 MiB，每谱 ≤16,384 对值（仅此读取器允许 32,778 标量），目录每页 64 / 总计 1024 谱；输出 ≤2 MiB |
| XRDML / canSAS1d | 整文件 ≤16 MiB /32 扫描 /65,536 点，每扫描 ≤16,384 点；输出 ≤2 MiB |
| FCS 事件 window | 源 ≤8 GiB、累计 ≤8 MiB /128 次；≤8392 事件、≤16,384 输出值，按实际完整行读取 |
| Ripple 谱像 window | 精确双文件 ≤8 GiB、累计 ≤8 MiB /256 次；头 ≤64 KiB；图像 ≤128²、能谱 ≤16,384 通道；输出 ≤2 MiB |
| ENVI 双文件 window | 精确头文件/数据配对合计 ≤8 GiB、累计读取 ≤8 MiB /256 次；头 ≤64 KiB；图像 ≤128²，光谱 ≤128 波段；两文件共同版本 |
| GRIB 气象 window | 源 ≤8 GiB、累计读取 ≤8 MiB /128 次；每页 ≤8 消息，单消息 ≤1 MiB，完整解码网格 ≤16,384 点；仅批准的 GRIB2 规则网格简单打包 |
| MiniSEED / SAC window | 源 ≤8 GiB、累计读取 ≤8 MiB /128 次；每页 ≤16 记录，显式单记录窗口 ≤16,384 样本；不拼接、不校准 |
| 科学关系网络 | 输入 ≤4 MiB、输出 ≤1 MiB；静态简单图 ≤1,000 节点/3,000 边；有界本地布局，不读外部网络资源 |
| DICOM window | 未压缩小端单文件；源 ≤8 GiB、累计 ≤8 MiB /128 次；ROI ≤128²，源脱敏声明与用户确认是像素准入条件，不作诊断 |
| AnnData spatial window | H5AD dense/CSR/CSC，源 ≤8 GiB、累计 ≤8 MiB /128 次；坐标及显式选定特征，≤8392 点，解码块另有 4/16 MiB 上限 |
| LAS pointcloud window | LAS 1.2/1.4 批准点格式；源 ≤8 GiB、累计 ≤8 MiB /128 次；≤16384 连续点，非空间抽样；不支持 LAZ/COPC |
| GRO trajectory | 整文件 ≤16 MiB；≤64 帧、8392 原子/帧、全文件 ≤131072 原子记录；保留 nm、nm/ps，不推断化学键 |
| VTU simulation mesh | 单 Piece ASCII 整文件 ≤16 MiB；4096 节点/2048 单元/32 场/9 分量；显式节点或单元场分量，非有限场值为 null |

第四个领域批次新增受限的 `geometry` 请求/结果类型：仅 `spatial-window / pointcloud-window / gro-trajectory / simulation-mesh` 可使用，各自的 `spatial / array+point_attributes / trajectory / mesh` 负载需通过专用结构及请求绑定校验。DICOM 使用已有 image 请求及数组结果。五类均要求先 tree、后携带客户端 version 明确选择，不允许隐式主机版本替代。详细边界见[重型格式受限首版](changes-2026-09-10-domain-visualization-batch-four.md)。新预算不扩大旧通用数组/曲线限制。

`max_output_bytes` 校验派生的统一 JSON 结果封装（payload、metadata、warnings 等），不把原始字节流误计为派生结果。外层应用 APIResponse 只有固定包装开销。QC 读取还校验其 Artifact 的真实大小与摘要，并对归一化结果执行当前插件和 8 MiB 硬限制。原始字节流按 `max_input_bytes` 限制。

字节存储读取按最多 8 MiB 分块，HTTP 输出每块最多 1 MiB；源文件大于 1 MiB 时提前使用临时磁盘缓冲，避免整文件驻留。较大的存储读取块减少数据集 helper/存储的反复启动和往返，但不改变整文件输入预算。每后端进程字节读取最多两个并发槽，槽保持到响应关闭；取消时等待仍在运行的底层范围读取结束再释放。存储提供方可能有内部缓冲，因此不把“两个槽 × 单块大小”宣称为整个服务的精确内存用量。输入取回和科学解析也各有原有并发限制，不宣称整个系统仅两个任务。存储不支持有界范围读取时拒绝，不降级成整文件无限制读取。

隔离容器保留读取器专属的私有资源配置，而不是多种公有协议：

- NetCDF/FITS/FASTQ：512 MiB 内存、1 CPU、32 PID、96 MiB 临时空间、30 秒期限。
- 扩展科学/Office/QC：1 GiB 内存、1 CPU、96 PID、512 MiB 临时空间、50 秒外层期限；容器另有 55 秒独立硬时限与自动删除。
- 生理 EDF/BDF 窗口：512 MiB /1 CPU /32 PID /16 MiB tmpfs；第三批四类窗口：1 GiB /1 CPU /96 PID /512 MiB tmpfs；均为 50 秒请求、55 秒容器硬截止。

解析容器均无网络、无端口、无数据集或宿主目录挂载、无凭据、非 root、只读根文件系统。取消/超时强制回收；Docker 不可用时不降级到 FastAPI 进程执行复杂解析。普通受限文本分页与原始字节交付不因此新增解析容器。

此协议没有新增通用 tile/range/chunk 对外协议、大图零拷贝、任意多对象资源集或分布式分析。第三批仅增加经过精确授权的本地单图像 OME-Zarr 作用域，不等于开放浏览器通用块 URL、目录访问或公网对象存储。

### 第二批：受控窗口与选择绑定

EDF/BDF 仍走同一 `POST /files/{id}/visualization`，`options` 为 `channels / start_seconds / duration_seconds`。第二批批准 `signal-window → edf` 的 `window` 组合；第三批新增明确列出的四种组合，不授予任意偏移接口、路径、网络或凭据。宿主与无网络隔离 worker 使用私有 `dataseek-window-v1` JSONL 往返请求字节范围，逐次核验 owner、文件版本、插件状态、目录代际和总预算。50 秒请求期限、10 秒单次读取期限；取消后保留并发槽至底层读取和容器回收结束。资源限制按上表的读取器配置独立执行，不因同后缀存在另一插件而放宽本次调用的预算。

新结果类型 `features` 只承载已清洗的 KML Point/LineString/Polygon GeoJSON，不接收外部 GeoJSON URL、NetworkLink、样式脚本或任意属性。无明确 CRS 的 ASC/DSAA 只显示纯数值热图；用户可以按数据说明显式选择 EPSG:4326/3857，不自动猜测。

归档成员是独立插件，原目录插件不解包的语义不变，优先级仍高于新成员插件。`member_id` 是归档完整内容摘要、索引和成员路径派生的不透明标识；不承担授权，相同字节副本可能具有相同标识。实际请求仍由 file_id/owner/version/插件作用域约束。只允许当前版本的小型 UTF-8 文本，不做 HTML/XML/脚本执行，也不创建提取文件。ZIP 独立验证解压 EOF、实际长度及 CRC，TAR 不伪称具备成员内容校验和。

CZI 首次 `kind:tree` 只检查结构，`kind:image` 要求显式文件 version、`indices:[C,Z,T]` 与 `roi:[x,y,width,height]`。第二批 `viz-czi` 仍是有界整文件取回后选择像素；第三批另立 `viz-czi-window` 仅对批准的未压缩方言执行真正范围读取。读取器除结构校验外，均绑定返回格式、选择、文件长度或坐标系至实际请求，拒绝“结构合法但窗口不同”的响应。

### 第三批：变量切片与精确多对象作用域

HDF5/NetCDF4 使用 `variable` 不透明 ID、`selection` 整数/切片数组和 `decode:raw`；OME-Zarr 使用 `level / indices / roi`；仪器图像使用 `frame / roi`。首次 `tree` 不自动读取像素，后续 `series/image` 强制携带其 `version`。不隐式应用 CF 解码、仪器标定或填充值掩膜。

OME-Zarr 仅从已登记数据集的 `.zarr/.zattrs` 进入。私有作用域清单只含精确数值层/块键、虚拟偏移和大小，不能含主机路径、文件 ID、用户、凭据或 URL。每次范围必须完全位于一个授权对象内，禁止跨对象拼读；所有成员的 stat 与当前数据集声明参与版本，结束再核验全部成员，未读取块变化也使结果失效。Host 批量 stat 沿用现有 allowlist/只读/nofollow 边界，元信息不送浏览器。

三层描述符校验只对 `.zattrs` 增加精确点文件特例，未开放任意隐藏文件名。只支持 NGFF 0.4 / Zarr v2 单图像；缺失块、未知压缩、超预算和不支持方言均拒绝，无整文件或公网降级。完整预算、语义及验收见[第三批记录](changes-2026-09-10-visualization-batch-three.md)。

### 领域扩展：系统树与三类科学窗口

Newick 使用批准的 `tree` 结果内专用 `phylogeny` 对象，不冒充关系网络或文件树。ENVI 只从已登记数据集 `.hdr` 进入，继承已有对象作用域的身份、stat、只读范围与最终快照校验，私有资源精确限定为 `header` 和 `data`，浏览器不能指定配套文件路径。GRIB 和地震记录首次仅目录探测，数值窗口及非首目录页必须绑定客户端版本；GRIB 显示 ROI 不代表 native 解析只解码 ROI，STEIM 按命中记录解码后取样。限制与科学语义详见[领域扩展第二批](changes-2026-09-10-domain-visualization-batch-two.md)。

### 有界内容画像与多视图选择

高频格式先通过已启用且声明 `prepare` 的候选插件读取有界签名与标签，返回 `kind: resources`、`metadata.purpose: content-profile`；`payload.profile` 仅含版本、容器/方言枚举、受控特征和证据、实际读取字节数、截断标志。候选仍先通过文件名、身份和插件开关检查；画像不能注册插件、放宽预算或绕过最终读取器校验。

MATLAB v7.3 推荐 H5Web，普通 MAT 推荐 Plotly；普通 `.tif` 检出 OME 或 GeoTIFF 标签后可优先推荐相应视图，坐标/轴信息仍由正式读取器验证。原 NetCDF 地图与曲线默认顺序不变，HDF5 签名不能直接判定为 NetCDF4。共享预览不发起该画像请求，原共享 TIFF 字节预览继续可用。

个人手动选择优先于自动推荐。文件公开版本标记、目录代际或候选变化时取消旧画像；相同目录轮询不重复探测或重挂载。H5Web/Plotly/NetCDF/FITS 等本批涉及的读取器监视稳定标识，避免仅因描述对象重建而重复下载；GeoTIFF 波段切换复用本次已授权字节和 TIFF 对象。

结构树数字使用原始数值文本，避免 JavaScript 大整数精度损失；XML 不处理 DTD、实体、处理指令，YAML 不加载构造器、别名、锚点或自定义标签。目录读取拒绝越界路径、链接、加密/超限 ZIP 等，GZIP 声明不等于完整性验证。媒体关闭/切换时停止播放并释放 Blob、画布和载入任务。具体格式子集和暂不支持项见第一批报告。

## 生命周期与兼容边界

1. 宿主校验完整目录，为每个插件创建真实 Cordis fiber，`owner.effect()` 注册、dispose 注销。
2. `visualizations.snapshot` 按需初始化独立目录；`visualizations.reload` 在独立 Context 构建候选，成功原子替换，失败保留上一有效目录。
3. 每次操作核验文件归属、私有 Spill 限制、插件启用、匹配格式、声明操作及版本。交付前核对当前文件版本、完整描述符、目录 revision 和启停状态。
4. 前端实例随文件、插件与目录代际改变而卸载，取消请求，清理 canvas/WebGL/worker/blob；已停用或过期的结果不能重新挂载。
5. 个人开关存于 Mongo `visualization_preferences`，稳定 ID 不变所以不需重建偏好。不销毁全局共享 fiber，避免一个身份停用影响其他身份。
6. Cordis 不健康、插件未知或停用时拒绝读取，不退回静态清单。目录重载是宿主内部 RPC，不向普通用户开放任意代码安装。
7. 原 `/renderers` 配置记录不删除，但未经适配的远程 API/组件配置不是可执行插件。

原 NetCDF 地图仍限定规则经纬网和显式切片，最多 128×128；曲线最多 1,000 点，不隐式平均聚合。原 FITS 保留 BSCALE/BZERO/BLANK 语义和图像 HDU 子集，不假装具备 Aladin 的 WCS 功能。统一描述与传输不能改变这些科学语义。

## 如何新增插件

1. 明确支持格式、视图、读取粒度、共享策略和真实资源预算。优先复用批准读取器/适配器；同格式不同场景使用不同稳定 ID。
2. 新增适配器组合时，修改唯一规范 `contracts/visualization-adapters.json`，生成三端副本并运行 `--check`。不要分别修改三个批准映射，不新增 `v3-*` 或 data_kind 分类来逃避能力建模。
3. 添加清单并落实可信适配器的懒加载、取消、过期结果保护与销毁。所有组件接收统一 `{file, plugin}`，使用共享 runtime，不直接下载任意 URL 或绕过插件开关。
4. 新读取器必须接入现有授权 file ID 入口，实现 options 白名单、输入/输出预算、无适用数据错误、科学语义与类型结果；复杂解析放入隔离 worker，不执行文件自带脚本。新增 job 使用现有 AnalysisJob/Artifact，不发明另一套任务系统。
5. 添加正常样例、无效契约、错类型/参数、超限、停用/版本变化、取消清理、共享限制及关联资源越权测试；验证原 Agent 工具目录不变、同格式多视图与已保存开关保留。
6. 运行 `bash scripts/check-regressions.sh --containers`，并根据新 SDK 执行真实隔离读取及浏览器画面验收。编译通过或 mock 返回不等于真实图形已验证。
7. 依赖/代码变化通过唯一 `./run.sh` 流程更新现有 Compose 服务。插件清单不是远程代码热安装系统，不另建开发栈或开放新端口。

## 第五批增量契约（2026-09-11）

新增 `sqlite-table`（whole/table）、`radar-window`（window/image）、`ugrid-window`（window/map）三个批准组合，均仅声明 preview、不可共享、独立启停。初始请求为 tree；SQLite 数据分页、雷达 image 和 UGRID geometry 必须显式携带客户端版本。`geometry` 的批准读取器增加 `ugrid-window → ugrid`，不扩大其他读取器权限。SQLite typed-cell 以字符串保留整数，以固定标记表达 NULL、非有限值、BLOB 长度和被省略文本；不允许用户 SQL。预算、方言与科学语义详见[第五批实现及验收](changes-2026-09-11-domain-visualization-batch-five.md)。

## 历史验收记录（统一协议改造前，2026-09-09）

以下保留原迁移阶段的验收事实与数字，不能作为本次统一协议改造已经通过的证明。当前协议、入口及开发流程以上文为准；本次验收由独立报告记录。

| 检查 | 结果 |
| --- | --- |
| 后端全量回归 | 收尾工作区 2,064 通过，32 既有/条件跳过，0 失败；包含最终浮点值校验和历史临时签名比较测试 |
| 前端 | 220 通过；类型检查、生产构建通过 |
| 沙箱严格全量回归 | 412 通过，0 跳过，0 失败（含 101 项科学预览测试） |
| Cordis host | 36 通过（含真实 fiber 注销、独立目录重载和原工具目录兼容） |
| 真实隔离容器 | NOAA NetCDF 地图 9,344 栅格值 / 曲线 12 点；NASA FITS 图像 10,000 值 / 曲线 688 点；合成 FASTQ 4 位置质量，取消测试通过 |
| 真实 HTTP 文件入口 | 7 次成功预览覆盖 5 类视图；缺失文件/插件 404、格式不匹配 422、版本变化 409、停用后 403 均符合预期 |

以上自动化测试合计 2,732 项通过、32 项条件或既有跳过，没有失败。真实 HTTP 验收创建的 4 个临时样本已全部按返回文件 ID 删除，NetCDF 地图开关已恢复原状态。收尾只读核对仍为 138 个文件、22 个会话、1,595 条事件、51 个数据集、414 条模型记录和 403 条 Token 记录；HTTP 验收前后的业务摘要一致。浏览器打开旧会话会沿用原有“清除未读”行为，更新会话时间戳，因此不将整个部署过程描述为数据库逐字节不变。

历史内容只读核对覆盖 22 个已有会话、1,595 条事件、16 个旧历史页，实际内容差异为 0。附件/截图的已知下载 URL 会每次生成新的 `expires/signature`，比较只对这两个临时参数归一化，仍检查正文、序号、文件 ID、主机、路径和其他查询字段；为此新增 14 项比较边界测试。

前端实测插件起停、刷新后持久化、恢复 TIFF 后原有 400 × 400 显微图像显示正常，检查时无浏览器错误。验证没有提交分析消息或调用真实模型。部署沿用 `./run.sh` 和唯一现有 `127.0.0.1:7001` 前端绑定；未提交或推送 Git。

复测入口：`bash scripts/check-regressions.sh --containers`；真实隔离容器读图可在具有后端依赖、现有 Docker 访问和 `SANDBOX_IMAGE` 配置的环境中执行 `PYTHONPATH=backend python backend/scripts/check_visualization_worker.py`。脚本只读取随项目提供的公开示例和合成 FASTQ，输出统计，不打印文件正文。

真实 HTTP 验收脚本为 `backend/scripts/check_visualization_http.py`，应在现有后端配置和依赖环境中运行，默认访问 Compose 内的 `http://frontend`，可通过 `DATASEEK_VERIFY_BASE_URL` 指定现有服务地址。该脚本不是只读检查：会上传 4 个唯一命名的临时公开/合成样本，短暂切换当前身份的 NetCDF 地图开关，并在 `finally` 中恢复开关、按本次返回的精确 ID 删除样本。不会创建会话、调用模型或删除原文件；结束时必须检查清理结果和前后业务摘要。

读取器语义参考官方 [netCDF4 文档](https://unidata.github.io/netcdf4-python/)、[Astropy FITS 图像读取说明](https://docs.astropy.org/en/stable/io/fits/usage/image.html)；容器边界采用 [Docker SDK 容器接口](https://docker-py.readthedocs.io/en/stable/containers.html)。具体支持范围仍以上述实现和回归样例为准。

### 数据集列表直接预览增量验收（2026-09-09）

- 最终后端全量 2,159 通过、32 既有/条件跳过；前端 235 通过，类型检查和生产构建通过；沙箱 412 通过；Cordis host 36 通过。合计 2,842 通过，0 失败，Compose 配置与差异检查通过。
- 真实 HTTP 覆盖 TIFF 原始预览、CSV 分页、NetCDF 地图及同文件曲线、FITS 图像、Shapefile 主文件与配套文件、本机目录 Markdown 分页；重复点击复用同一引用，三类不安全或未登记路径被拒绝。
- 实测确认 HOST_PATH 在浏览器中隐藏内部挂载前缀。列表与预览现共用公开路径转换规则，支持新旧登记形式；不同源目录投影成同一路径时拒绝猜测文件。
- 浏览器实测从文件列表按钮打开 128 × 73 的 NetCDF 地图，再切换到 12 点曲线；小屏幕下打开预览自动收起数据集侧栏。测试标签临时阻止了原有自动推荐问题请求，未改生产设置。
- 验收前后均为 151 个已存文件、23 个会话、1,668 条事件、51 个数据集、430 条模型记录、419 条 Token 记录，HTTP 验收业务摘要一致；未上传、删除源文件、创建会话或调用模型。旧会话分页与文本/CSV 预览复测通过。

复测脚本：`backend/scripts/check_dataset_file_preview.py`（实际 HTTP，生成可复用的过期预览引用，无原文件写入）和 `backend/scripts/check_dataset_host_preview.py`（本机目录 stat-only/范围读取及 helper 清理）。均在现有后端配置、依赖和 Docker 访问环境中运行；不另建开发栈或开放新端口。
