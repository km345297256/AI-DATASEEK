# AnnData 空间组学受控窗口（首版）

插件 `viz-spatial-window`，读取器/适配器 `spatial-window`，统一公有契约版本 2。
面向单个 `.h5ad` 的空间坐标与一个表达特征，不代表完整 AnnData、SpatialData 或 10x 产品兼容。

## 支持的结构与科学语义

- AnnData 根编码 `anndata / 0.1.0`；明确 `obsm/spatial` 数值二维数组，形状必须为 `n_obs × 2`，编码 `array / 0.2.0`。
- 表达源只取 `X`：二维 dense，或编码 `csr_matrix / csc_matrix / 0.1.0` 的稀疏结构。形状必须与观测、特征数一致。
- `obs` 与 `var` 是 `dataframe / 0.2.0`，检查其索引数据集的类型与长度；兼容官方 writer 的 UTF-8 可变长索引。**不读取索引字符串、其他 obs/var 列、患者/样本身份或基因名。** 用户使用从 0 开始的特征序号，并自行按数据说明映射。
- 仅原样展示 `X` 存储值。**X 不必是原始计数，上游可能已经归一化/对数变换，当前插件无法判断。** 不做归一化、配准、背景扣除、滤波、校正或统计聚合。
- 空间两列按文件原序、同一观测序号关联；不倒 Y，不推断像素/微米、CRS、尺度或轴方向。坐标轴标注“单位未知”；没有底图、影像叠加或组织配准。图中相同数值尺度不宣称具有物理标定。
- 稀疏缺失项按 CSR/CSC 定义为 0；选中段内必须索引严格递增且无重复。重复或未排序会拒绝，不自动累加或修复。只验证读取段，不声称全稀疏矩阵已校验。
- 浮点 NaN/Inf 在对应位置保留为 null，不代换为 0；仅完整 x/y/value 三元组绘点，显示空值计数。超过 JavaScript 安全整数范围的数值拒绝而非舍入。
- 数值完整但超过当前 Plotly 线性显示的可靠范围时明确提示，不暗中重缩放。散点不连线；颜色表示所选 X 特征的存储原值。

不支持：多维/稀疏 spatial、DataFrame spatial、layers/raw 表达切换、命名基因查询、obs 分组、图像/标签对象、SpatialData Zarr、多文件 10x 输出直接配对，以及自动识别某仪器单位。

## 协议

`spatial_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None)` 只在现有无网络隔离 worker 中运行；授权仍在主机先完成。

首次：

```json
{"plugin_id":"viz-spatial-window","operation":"preview","kind":"tree","options":{}}
```

显式数值窗口必须携带首次返回的文件版本：

```json
{
  "plugin_id":"viz-spatial-window",
  "operation":"preview",
  "kind":"geometry",
  "version":"<此前返回的文件版本>",
  "options":{"feature":1,"observation_start":0,"observation_count":1024,"decode":"raw"}
}
```

`tree` 返回固定结构节点及观测/特征数量，不自动读样本。
`geometry` 的专用 `payload.spatial` 含等长 `observations / x / y / values`。
返回选择与请求必须一致；dtype、源大小、读取计数、目录结构和版本均绑定。数据或插件身份改变后同步取消；晚到响应和图形不能重新挂载。只读预览不会创建会话、调用模型或修改数据集。

## 实际读取与预算

| 项目 | 首版边界 |
| --- | --- |
| 源文件 | ≤8 GiB |
| 授权范围取回 | 每次累计 ≤8 MiB、≤128 次，每范围 ≤1 MiB |
| 返回点 | ≤8,192；科学数值 ≤24,576（x/y/value），另有对应观测序号 |
| 返回 JSON | ≤2 MiB，包含公共包装预算 |
| HDF5 数值块 | 单块声明解码 ≤4 MiB；本次累计 ≤16 MiB／128 块 |
| 主动数值数组 | 累计 ≤4 MiB，单读 ≤65,536 元素 |
| 稀疏扫描 | 每窗 ≤65,536 稀疏条目 |
| 白名单标量属性 | 固定 128 字节目标内存类型，累计目标缓冲 ≤4 KiB |

复用已有 HDF5 `RangeFile`、数值类型/布局检查、切片预算和压缩块实际解码预检，不修改旧数组插件限制。拒绝涉及目标路径的软链接、外链、VDS、外部数据存储、未知/不批准的过滤器、复合/vlen 数值类型及超限块。

Dense 选择观测行和特征列；所涉及 HDF5 块可能还含其他列。CSR 读取所选观测行的有界完整稀疏段，再选择特征；CSC 读取所选特征列的有界完整稀疏段，再筛观测窗。**CSC 并不是仅取回所选观测；压缩块解码也不等于输出点数。** 跨度过大时缩小窗口或使用分析任务，不退化为整文件下载。

目录不执行数值数组读取，但 16 KiB 的有界 HDF5 元信息页缓存可能预读同页相邻字节；这仍计入实际输入预算，不能描述成物理上没有接触任何邻近样本字节。

官方 AnnData 使用 UTF-8 vlen 属性保存编码标签。仅 `encoding-type / encoding-version / _index` 允许标量字符串通过低层 H5A 固定 128 字节目标类型转换；满长、非法字符/编码或非标量拒绝，数字 shape 只接受固定两个整数。不会向 Python 生成任意长度字符串，**但 128 字节不是 HDF5 内部 heap 或进程 RSS 上限**；内部元信息解析仍受范围预算、无网络、只读、进程内存与超时隔离约束。不读取用户字符串数组，不将固定目标缓冲冒充完整原生分配预算。

## 验证与复测

- 独立参考：测试环境临时安装 `anndata==0.11.4`，用官方 `AnnData.write_h5ad` 和 `read_h5ad` 验证 dense/CSR/CSC、gzip/未压缩、真实 UTF-8 索引；生产不新增 AnnData、Scanpy 或 SpatialData 依赖。
- `sandbox/tests/test_spatial_window_reader.py`：设置 `AI_DATASEEK_REQUIRE_SPATIAL_REFERENCE=1` 强制 oracle，不允许以依赖缺失跳过。含超过 1 GiB 的实际稀疏文件、只取回不足 128 KiB 的窗口；压缩炸弹/超短流、重复索引、形状、累计解码、标签不读取、取消等边界。
- `backend/tests/test_spatial_window_visualization.py`：纯 Python 严格字段、源/计数/选择绑定与科学语义；不依赖科学库。
- `frontend/tests/spatialWindow.test.mjs`：真实 Vue setup 的同步取消、版本、手动窗口、空值/范围和颜色原值。
- 浏览器 `frontend/tests/browser/domain-expansion-spatial-fixtures.mjs` 导出 `domainExpansionSpatialCases`，使用真实 AnnData + reader 生成的 JSON，覆盖三布局、无自动窗口读、不反转 Y、原值颜色和 Plotly 清理。
- HTTP 小型纯合成入口：在沙箱测试目录 import `spatial_window_fixtures.spatial_bytes("dense"|"csr"|"csc")`（不需要 AnnData）。默认 4 观测/3 特征，`feature=1,start=1,count=2` 返回坐标 `(2,20),(3,30)`，表达值 `[0,5]`。官方兼容性另由 oracle 证明。

所有测试使用合成数据和一次性测试容器，不读取真实样本或模型资源。

## 上游依据

结构与编码遵循 [AnnData on-disk format](https://anndata.readthedocs.io/en/stable/fileformat-prose.html)；空间数组约定参考 [Scanpy read_visium](https://scanpy.readthedocs.io/en/stable/generated/scanpy.read_visium.html)。
[10x Space Ranger spatial outputs](https://www.10xgenomics.com/support/software/space-ranger/latest/analysis/outputs/spatial-outputs)区分组织位置、像素坐标和缩放元信息，不能从通用 spatial 两列自动推断单位或配准。
字符串边界参考 [h5py Strings](https://docs.h5py.org/en/stable/strings.html)、[低层 H5A.read](https://api.h5py.org/h5a.html)；固定目标转换的限制按上文说明，不宣称完整 vlen 内容安全子系统。
