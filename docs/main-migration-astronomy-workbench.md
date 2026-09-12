# main 科学影像工作台迁移到 Cordis

来源固定为 `origin/main` 的 `6f6b32e`，不是合并整个 main。源实现为 `backend/app/application/services/astronomy_preview.py`、`backend/app/interfaces/api/file_routes.py` 的 astronomy-preview 路由，以及 `frontend/src/components/filePreviews/AstronomyImagePreview.vue`。

## 保留的业务交互

| main 功能 | Cordis 工作台实现 |
| --- | --- |
| FITS HDU 目录、图像立方切片 | `tree` 返回目录，`image` 要求显式 HDU / 每个前置轴索引 / 文件版本 |
| FITS `.fit/.fits/.fts/.fz`、gzip 包裹 | 受控解压后检查 FITS 块布局；支持标准图像及 Astropy 的受限瓦片压缩图像 |
| TIFF 多页、通道、RGB | 页目录，按 TIFF 的 planar configuration 选择通道，不猜 shape 中哪个维度是颜色；前 3 通道 RGB |
| 显示范围与颜色 | ZScale、1–99%、手动上下界；线性 / log / sqrt / asinh；灰度 / 近似 Viridis / 热力 / 冷色，反色 |
| 直方图与统计 | 96 桶、明确直方图抽样数量；全平面有限标量的最小 / 最大 / 均值 / 中位数 / 标准差 / 总和 / 有效与无效数 |
| 缩放、平移、像素查询 | 前端局部交互；点击对应原始像素索引，再携带原始文件版本查询数值；无效值透明 |
| 框选统计 | 拖动选区或填写四边界；严格零起始、右/下边界不包含，越界拒绝，不偷偷裁剪 |
| 天球 WCS | 可分离 celestial WCS 四角、单像素以及峰候选世界坐标，保留轴类型与单位；无法构造的坐标不猜测 |
| 源检测叠加 | 原 main 的 MAD 噪声估计、Gaussian 平滑与局部极大值法；最多 2,000 个候选，显示背景 / 噪声 / 原始峰值 / SNR；不是测光或分类 |
| FITS 表格 | ASCII 表、二进制固定列、固定向量；每页 50 行 ×32 列，列单位；整数超过 JS 安全范围时转成精确十进制文本 |
| FITS 一维数据 | 最多 2,000 点的显式步长抽样，显示原始采样索引；缺失值断开曲线，禁止跨缺口连线 |
| GeoTIFF 空间元数据 | CRS、原生与 WGS84 范围、分辨率、波段数、Nodata；仅显示经过筛选的字段 |

天球/地图的**公网底图与外部调查服务不迁入**。原有 FITS、TIFF、Aladin 等插件保持独立，可由用户按场景启停。本插件不是替代 AgentLoop、SSE 或已有读取器。

## Cordis 协议与隔离

- 插件 / adapter / reader：`viz-astronomy-workbench` / `astronomy-workbench` / `astronomy-workbench`，contract v2。
- 只使用统一可视化 API；不新增 prepare/render/release 路由，不返回 `preview_id`、临时路径或源主机路径。
- Worker 接口：`astronomy_workbench_preview(data, fmt, kind='tree', options=None)`；只能接收宿主授权的字节、格式枚举和严格选择参数，无法接收 URL、路径、SQL 或表达式。
- 纯契约验证：`validate_options(kind, options)`；`validate_payload(payload, *, kind=None, options=None, format=None, source_bytes=None)`。沙箱与 API 端文件保持逐字一致。
- 所有响应均有 `choices / selected / metadata / workbench`；目录另有 `tree`。`image` 对外归一化为 `raster`，嵌套结果由专用 schema 校验，不放宽旧 array/graph/readers 的上限。
- 首次 `tree {}`，之后请求必须携带树响应的文件版本；主机再次检查用户、插件启停、目录修订、源版本和大小。前端再次校验精确选择与初始目录一致性。
- 文件更换、插件禁用/升级/预算变更、切片/显示参数更换、取消与卸载均取消正在读取的任务；迟到结果不能恢复已关闭的内容。只在内存保存当前结果，不写浏览器持久存储。

### 请求

```text
tree:   {}
table:  {dataset, row_offset, column_offset}
series: {dataset}
image:  {dataset, slices: [...], band, action, ...}
  render:  stretch, interval, low, high, colour_map, invert
  pixel:   x, y
  region:  bounds: [x0, y0, x1, y1]
  sources: threshold_sigma
```

FITS `band=1`；TIFF `slices=[]`，`band=0` 为前 3 通道 RGB，其他波段从 1 起。自动范围的 `low/high` 必须为 null，手动范围必须为有限数且 high > low。

## 与 main 的明确差异和预算

main 使用 API 进程中的临时文件缓存，允许单源 2 GiB、总 64 条/单用户 4 条、30 分钟 TTL。这里没有搬入这一架构：所有科学解析运行在网络隔离的一次性 worker，采用 **32 MiB 整文件预算**。这不是 2 GiB 或大型文件按块读取能力；大型 FITS/GeoTIFF 的 range 版需要后续独立实现。

| 边界 | 上限 |
| --- | --- |
| 输入 / gzip 解压后源 | 各 32 MiB |
| 所有目录声明数据的保守解码预算 | 128 MiB；FITS 按每元素至少 8 字节估算，TIFF 按最大实际/float64 宽度估算 |
| 单个所选图像平面 | 2,097,152 标量，RGB 计算 3 通道总和 |
| HDU / TIFF 页 | 64 |
| 维数 / 颜色通道 | 8 / 16 |
| 单 FITS 头 | 1 MiB |
| FITS 目录列描述总数 | 256 |
| PNG 显示最大边 / 输出 JSON | 1,024 px / 4 MiB |
| 表格页 / 单固定向量 | 50×32 /100 项；文本 128 字符 |
| 曲线 / 峰候选 | 各 2,000 |

这是格式限制而不是静默退化：随机组、非标准 FITS 块布局、FITS 变长堆列与复数列、超预算页/图像、palette/YCbCr 等 TIFF 色彩编码均明确拒绝。现有 worker 已有科学依赖 Astropy 6.1.7，新增 TIFF 依赖 tifffile 2025.5.10；图像、数值、投影依赖复用已安装栈。不会安装 main 的 API 进程缓存依赖或修改生产 NumPy 主版本。

显示和科学语义也有明确界限：坐标按数组行列，不自动旋转到患者/地理方向；RGB 平面统计按各通道标量汇总；源检测 RGB 使用各通道均值；范围统计不等于滤波或拟合。显示 PNG 可以缩小，但像素与选区查询始终针对原始所选平面。TIFF 空间元数据来自主图像的 GeoTIFF 声明，不自动继承给不同的页。

## 可重复验证

- `sandbox/tests/test_astronomy_workbench.py`：64 项真实格式与安全回归；包含由 Astropy / tifffile / Rasterio 独立生成的图像立方、压缩 FITS、unsigned scaling、ASCII/二进制表、谱线、多页 TIFF、RGB contig/separate、GeoTIFF Nodata、WCS 数值参照、全部 32 组色图/拉伸/反色组合、恶意元数据和越界请求。
- `backend/tests/test_astronomy_workbench_visualization.py`：7 个生产 reader 响应及 42 个篡改用例，另验证 API / sandbox 契约文件一致。
- `frontend/tests/astronomyWorkbench.test.mjs`：48 项，包含严格 schema 与实际 Vue 的 tree→version→操作链路、错误切片、gzip 文件名仍走不透明文件 ID、迟到结果、停止/禁用/卸载/源变更回归。
- `frontend/tests/browser/main-astronomy-fixtures.mjs`：3 个真实浏览器场景，检查 PNG 实际像素/透明度、像素 WCS、选区、峰候选、表格、缺口曲线、错切片及私密字段拒绝。已经由项目统一 browser runner 执行通过，没有外部请求、控制台错误、未回收 worker 或 blob。
- 无 pytest 依赖的实际 API fixture producer：`sandbox/tests/astronomy_workbench_fixtures.py`，导出 `cube_bytes()`、`tiff_bytes()`、`selection(action='render', **changes)` 和 `fixture_payloads()`，只生成合成数据。

数据格式依据：[Astropy FITS](https://docs.astropy.org/en/stable/io/fits/index.html)、[Astropy WCS](https://docs.astropy.org/en/stable/wcs/wcsapi.html)、[tifffile](https://github.com/cgohlke/tifffile)、[Rasterio MemoryFile](https://rasterio.readthedocs.io/en/stable/topics/memory-files.html)。文档描述的是本地冻结依赖下实际验证的子集，不把上游最新版本能力自动算作已集成。
