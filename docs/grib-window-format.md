# GRIB2 消息窗口可视化：明确受限的首批方言

`viz-grib-window` 是独立 Cordis 可视化插件，reader/adapter 为 `grib-window`，复用现有统一 v2 请求和受控 range worker。首次按消息分页读取元信息；选中一条消息及 ROI 后才展开气象场。它不是 GRIB 全格式浏览器，不替换已有 NetCDF/HDF5 插件，也不合并不同变量、层次和时刻。

## 解码依据与依赖

数值由 ECMWF **ecCodes C 核心**解码，不手写 GRIB 位流解码、不调用模型、不启动分析任务。自有只读 CFFI 薄层调用官方 `grib_handle_new_from_partial_message_copy` 读取已经预检的原始消息头前缀；只取名称、短名和单位，不请求 values、bitmap、latitudes 等数组。该前缀止于 section 7 的五字节节头，**目录不读取 section 7 正文，也不展开数值**。完整受限消息通过预检后，才使用 `codes_handle_new_from_message_copy`、`codes_get_double_array` 得到数值和真实经纬度。[官方 C API](https://github.com/ecmwf/eccodes/blob/develop/src/eccodes/eccodes.h)、[partial-message API](https://github.com/ecmwf/eccodes/blob/develop/src/eccodes/grib_api.h)。

固定验证版本：Python `eccodes==2.48.0`、`eccodeslib==2.48.2.27`，实际 C 库 `2.48.2`；传递二进制依赖 `eckitlib==2.2.0.27`。Linux ARM 测试确认定义和样本来自内置 `/MEMFS/definitions`、`/MEMFS/samples`，不要求用户主机安装解码库。Python 和核心库均为 Apache-2.0；完整依赖锁由统一 sandbox 构建管理。[官方 Python 仓库与许可证](https://github.com/ecmwf/eccodes-python)、[ecCodes 核心库](https://github.com/ecmwf/eccodes)、[PyPI 发布](https://pypi.org/project/eccodes/2.48.0/)（2026-09-10 核对）。

### 与已有原生科学库共存

本版本上游 Python loader 经 findlibs 会将整组 eckit 依赖以 `RTLD_GLOBAL` 预载；在当前 Linux ARM 环境与现有 libCZI 的 ZSTD 操作同进程共载时已实证发生段错误。因此生产不直接 import 上游 `eccodes` Python SDK，而是惰性初始化自有只读 C ABI 适配器：按安装 wheel 记录定位固定版本，仅本地作用域加载 ecCodes 必需的五个 eckit 库，再 `RTLD_LOCAL` 加载 ecCodes。它不是另一个解码实现，也没有修改旧 CZI、系统环境、ctypes、findlibs 或任何第三方模块。字符串与数组均先限长，所有句柄在 finally 释放，不暴露文件访问、样本创建或写入 API。

官方 Python SDK 仅在**独立测试生成进程**中创建合成 GRIB 字节，构成跨接口 oracle；真正 reader 通过上述 C 核心薄层解码。兼容测试在同一进程内分别执行 GRIB→CZI→GRIB 与 CZI→GRIB→CZI，包含真实 ZSTD CZI 写读及 GRIB 数值/经纬度校验，确认安装版本与环境变量不改变。每次正式可视化依然运行于原有一次性隔离 worker。

## 本批支持与明确不支持

| 项目 | 首批实现 |
| --- | --- |
| 候选后缀 | `grib`、`grb`、`grib2`、`grb2`；后缀不替代真实内容检查 |
| 容器 | 连续独立 GRIB2 消息；每消息一个字段、规范 section 序列与 `7777` 结束标识 |
| 网格 | GDT 3.0 `regular_ll`，标准微度坐标，Ni×Nj 与声明点数一致 |
| 产品 | PDT 4.0 瞬时分析/预报，单固定面；基准时间、预报时间单位编码和固定面原始编码独立展示 |
| 压缩 | DRT 5.0 `grid_simple`，位宽 0–32；有限参考值，二进制/十进制缩放指数绝对值不超过 100 |
| 扫描 | i 正/负方向、j 正/负方向、i/j 哪一维相邻的 8 种组合；不是按显示习惯重新排序 |
| 缺失 | section 6 内联位图（0）或明确无位图（255）；按照每个点的位图位保留空值 |
| 元信息 | 标准 WMO 表，名称/单位经 ecCodes 解释；标签未识别时可显示 `unknown`，不猜测单位 |

不支持 GRIB1、局部 section/局部参数表、PDT 的时间累计/统计/集合及双面层、简化/旋转/投影/高斯网格、交替行或错位扫描、复杂/差分/JPEG/PNG 压缩、历史位图复用（254）、外部预定义位图及一条消息包含多个字段。

未知或超预算的合法消息不会进入原生解码器；目录显示本页跳过计数，可继续下一页。畸形长度/节序列、截断或错误结束标识直接拒绝，不能假装已完整浏览文件。仅提供通用“不支持”原因列表，不把无法预览解释成文件损坏。

规范字段依据：[GDT 3.0](https://codes.ecmwf.int/grib/format/grib2/templates/3/0/)、[PDT 4.0](https://codes.ecmwf.int/grib/format/grib2/templates/4/0/)、[DRT 5.0](https://codes.ecmwf.int/grib/format/grib2/templates/5/0/)、[section 6 位图](https://codes.ecmwf.int/grib/format/grib2/sections/6/)。本表是实现子集，不代表这些规范仅支持以上能力。

## 科学呈现语义

- 输出是 ecCodes 完成 GRIB 打包解码后的数值，单位保留参数表声明。例如测试温度保持 K，**不默认转成摄氏度**；不额外标定、插值、重投影或抽样。
- 行列选区遵循网格 i/j 方向。j 相邻编码转为明确的二维行优先结构后，与原生库逐点经纬度交叉核对；不把转存矩阵误认为转置数据语义。
- 地理坐标轴使用纬度和明确的 **[0,360) 经度约定**。ecCodes 可以返回连续的 360° 以上数值，比较和输出采用等价经度约定，但不排序点。选区若跨 0/360 断点则拒绝，提示选择单侧；不能把 `[359,0,1]` 接成跨全球的连续像元。此图不声明 EPSG:4326 或统一地球椭球参数，不作距离/面积计算。
- 缺失仅由文件位图控制，不以任意数值（如 9999）猜测缺失。相同数值在位图声明存在时照常显示；非有限的存在值拒绝，不悄悄扩大缺失范围。
- 每条消息单独显示基准时间和编码的 forecast time × time-unit。固定面类型、scale/value 原始编码明确标注，不把未知垂直面解释成高度/压强；不擅自合并多消息时间序列。
- 前端仅用本地 Plotly 热图及数值经纬轴，缺失像元留空。没有在线地图瓦片、远程脚本或外部数据请求。

## 请求、分页与预算

```python
grib_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None)
validate_grib_window_options(kind, options)
validate_grib_window_payload(result, kind=kind, options=options, fmt=fmt,
    source_bytes=size, read_bytes=actual_bytes, read_requests=actual_requests)
```

`tree` options 为 `{}` 或 `{offset: 0}`，统一规范化为 `{offset: 0}`；下一页使用响应的 `metadata.next_offset`，**非零 offset 必须带外层文件 version**。offset 是该授权文件内的消息字节边界，不是主机路径。每页最多扫描 8 条消息，metadata 含 scanned/skipped/page_offset/next_offset；不在 API 主机上解析内容。

`image` options 为 `{message: "g-0000000000000000", roi: [列起点, 行起点, 宽度, 高度]}`，必须带 version。消息标识仅是稳定文件内定位符，**不是身份或授权凭据**；所有实际读取仍受 owner、file ID、插件状态、版本和 range 能力约束。错误或被猜中的非消息边界会被内容检查拒绝。

单请求预算：源 ≤8 GiB，累计取回 ≤8 MiB/128 次，单次 range ≤1 MiB；可解码的单消息 ≤1 MiB、**整个消息网格** ≤16,384 点，ROI 展示 ≤16,384 值；私有 JSON <2 MiB（保留包络余量）。元信息非数据节每段最多读取 4 KiB，目录只读最多 8 条消息的头和内联位图；没有整文件/读前预取降级。

即使 ROI 很小，也必须对该受限消息完整解码，因此大网格文件不会仅因选区很小就被接受。native 构造句柄前校验 Ni×Nj、编码值计数、位宽、位图长度/填充、数值节长度及消息边界；不能用容器内存上限代替预分配检查。取消或短读不会重试、更不会改成任意本地文件访问。

公有结果 `kind=tree/array`，`payload.view_kind=tree/image`；image 提供 `array.shape=[height,width]`、latitude/longitude 数值轴以及 null 缺失像元。宿主、worker 和前端校验明确字段，拒绝未知键、错类型、越界选择、虚假计数或不一致坐标。

## 测试与验收样本

`sandbox/tests/grib_window_fixtures.py:message()` 在独立子进程内调用 ecCodes 官方 Python SDK 的 `regular_ll_sfc_grib2` 内置样本编码，通过标准输出返回 228 字节，无用户数据/网络/文件写入。默认 6×4，2 m temperature、K、2026-09-10 00:00 UTC、6 小时预报，编码第 4 点缺失。

选区 `[1,0,4,3]` 应得到 `[281,282,283,null,287,288,289,290,293,294,295,296]`，纬度 `[50,49,48]`、经度 `[11,12,13,14]`。目录读取 182 字节/13 次、不读取数值正文；image 读取该消息 228 字节/14 次，完整解码 24 点，仅展示 12 点，其中 1 点缺失。

测试文件分别为 `sandbox/tests/test_grib_window_reader.py`、`backend/tests/test_grib_window_visualization.py` 和 `frontend/tests/gribWindow.test.mjs`。真实 ecCodes 响应保存在 `frontend/tests/browser/grib-window-data.json` 与 `grib-window-pages.json`，不会手造科学真值。真实 Chrome case 验证热图/单位/缺失点、8→2→8 消息分页、非零分页版本绑定、未决请求卸载取消、Plotly 释放和无外联。

```bash
cd frontend
VISUALIZATION_BROWSER_CASES=domain-expansion-grib-image,domain-expansion-grib-pagination node tests/browser/domains-smoke.mjs
```
