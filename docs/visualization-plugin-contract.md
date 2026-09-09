# Cordis 统一可视化插件协议

当前公有协议版本为严格整数 **2**，所有 **36 个插件**使用同一描述结构、能力声明、调用入口与结果封装。版本用于契约兼容检查，不再区分基础插件、科学插件或办公插件；清单不再接受 `data_kind`，适配器键不再使用 `v2-` 前缀。本次不维护旧版可视化接口兼容分支。

完整工具范围、上游许可和实际格式限制见[21 组工具集成说明](scientific-visualization-integration.md)。已有插件的稳定 ID、发布版本、默认开关、优先级、格式匹配、权限及资源预算保持不变，已保存的个人启停选择继续生效。

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

唯一受版本控制的适配器规范是 `contracts/visualization-adapters.json`，它描述批准的 `adapter → readers / view_kind / capabilities` 组合。当前共有 35 个适配器键、36 个插件注册；NetCDF/FITS 曲线共用一个适配器。

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

受审查的插件文件位于 `plugin-host/visualizations/*.json`。描述符、能力对象、预算对象严格拒绝额外或缺失字段；目录最多 256 个插件、单文件最多 16 KiB，ID 唯一。不接受脚本、动态组件路径、远程 URL、命令或凭据。

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
| `view_kind` | `image / map / series / table / text / structure / document`，表示用户场景 |
| `adapter` / `reader` | 批准的前端适配器键 / 非空读取器键；不是 URL 或可执行入口 |
| `capabilities.operations` | 非空、无重复、符合批准组合的操作列表，见下表 |
| `capabilities.input_mode` | `whole / page / prefix`，明确输入预算针对整文件、每页还是前缀 |
| `capabilities.shared` | 是否允许该插件用于已有共享预览；不是文件访问授权或数据公开开关 |
| `default_enabled` / `priority` | 未保存个人偏好时的初始值 / -1000～1000 的排序整数 |
| `permissions` | 严格 `["file:read"]`，不授予写入、任意网络或凭据 |
| `limits` | 严格正整数预算，受读取器及存储更严格硬限制；不能通过增大清单预算绕过它们 |

Node、Python、前端都从同一个批准规范校验完整组合，不能仅改 `operations` 或 `shared` 扩权。

| 操作 | 含义 | 当前插件 |
| --- | --- | --- |
| `bytes` | 读取授权文件的有界原始字节，浏览器本地解码 | 普通图片/TIFF/OBJ/HTML/Markdown/Shapefile、分子结构及 vtk/Mol*/地图/医学影像/PDF 等 |
| `page` | 文本/CSV 分页，不以整个源文件大小禁止预览 | `text`、`csv` |
| `preview` | 宿主调用批准读取器，返回有界结构化结果 | NetCDF/FITS/FASTQ 采样、Plotly/H5Web/JSROOT/NMR/RDKit/MetPy/Word/Excel/PPT |
| `prepare` | 返回只读分子结构元信息，不创建分析任务或写入数据 | `molecular`，随后用 `bytes` 读取 |
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
      | 'tree' | 'media' | 'report' | 'molecule' | 'resources';
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
| GET `/visualizations` | 返回完整 36 个当前统一描述符及 enabled，不再协商两个目录 |
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

`max_output_bytes` 校验派生的统一 JSON 结果封装（payload、metadata、warnings 等），不把原始字节流误计为派生结果。外层应用 APIResponse 只有固定包装开销。QC 读取还校验其 Artifact 的真实大小与摘要，并对归一化结果执行当前插件和 8 MiB 硬限制。原始字节流按 `max_input_bytes` 限制。

字节存储读取按最多 8 MiB 分块，HTTP 输出每块最多 1 MiB；源文件大于 1 MiB 时提前使用临时磁盘缓冲，避免整文件驻留。较大的存储读取块减少数据集 helper/存储的反复启动和往返，但不改变整文件输入预算。每后端进程字节读取最多两个并发槽，槽保持到响应关闭；取消时等待仍在运行的底层范围读取结束再释放。存储提供方可能有内部缓冲，因此不把“两个槽 × 单块大小”宣称为整个服务的精确内存用量。输入取回和科学解析也各有原有并发限制，不宣称整个系统仅两个任务。存储不支持有界范围读取时拒绝，不降级成整文件无限制读取。

隔离容器保留两种私有资源配置，而不是两种公有协议：

- NetCDF/FITS/FASTQ：512 MiB 内存、1 CPU、32 PID、96 MiB 临时空间、30 秒期限。
- 扩展科学/Office/QC：1 GiB 内存、1 CPU、96 PID、512 MiB 临时空间、50 秒外层期限；容器另有 55 秒独立硬时限与自动删除。

两者均无网络、无端口、无数据集或宿主目录挂载、无凭据、非 root、只读根文件系统。取消/超时强制回收；Docker 不可用时不降级到 FastAPI 进程执行复杂解析。普通受限文本分页与原始字节交付不因此新增解析容器。

此协议没有新增通用 tile/range/chunk 对外协议、大图零拷贝、目录型 Zarr、任意多对象科学资源集或分布式分析。后端内部有界 range 读取是安全的文件交付实现，不等于已实现浏览器按需大图瓦片服务。

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
