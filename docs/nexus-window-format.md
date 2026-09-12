# NeXus NXdata 语义窗口预览（首批受限方言）

本插件用于把 HDF5 内已声明的 NXdata 默认信号按文件坐标绘为曲线或二维图，而不是替代通用 HDF5 数组查看器。`nxs`、`nx`、`h5`、`hdf5`、`hdf` 只是候选后缀；必须验证实际 HDF5 内容。HDF4、普通 HDF5 无 NXdata 等情况不能因后缀获得语义支持。

## 支持范围与科学语义

解释依据为 [NeXus NXdata 官方定义](https://manual.nexusformat.org/classes/base_classes/NXdata.html)（2026-09-10 核对）。首批采用现代组属性 `NX_class=NXdata`、`signal`、`axes`、可选 `AXISNAME_indices` 和 `FIELDNAME_errors`；官方标准还有更广能力，下述限制属于本插件的安全和呈现边界，不是对标准的完整实现。

- 每个 NXdata 只选择 `signal` 指定的一个默认信号，不枚举辅助信号或自动执行 `default_slice`。
- 信号限 1D/2D；整数 8/16/32/64 位、float32/float64，保留文件字节序和原始值。排除布尔、float16、复合、对象、字符串、枚举与变长数据；整数超过 JavaScript 安全范围时拒绝窗口，不能悄悄舍入。
- 坐标限数值型 1D 中心坐标，长度必须等于对应信号维度，选择的坐标须有限且严格单调（升序或降序均可）。`AXISNAME_indices` 若存在，必须与该维度一致。边界坐标、类别坐标、多维坐标和冲突声明不猜测映射。
- 缺少 `axes` 或某维明确写 `.` 时，该维显示原始索引并明确标注；声明过的错误坐标不会被悄悄替换为索引。
- 只显示默认信号对应的 `FIELDNAME_errors`，尺寸必须与信号一致，作为标准差；曲线显示误差条，二维图在悬停中显示。**坐标误差和辅助信号的误差本批不读取/展示**。负标准差拒绝；误差单位若声明必须与信号原始单位声明一致，不自动换算。
- 单位只作为文件声明的标签展示，不证明已经标定。**不应用任何缩放/偏移、单位转换或有限填充值掩膜**，包括 NeXus 的 scaling/offset 字段；需要标定的文件应使用明确的分析步骤。非有限信号或误差显示为空，非有限坐标拒绝；不连接信号缺口、不自动重采样、不转置数组。
- 仅访问同文件硬链接，遇到被遍历的软链接/外链立即拒绝，不访问目标文件。VDS、外部存储和动态 HDF5 解码插件均不支持。

## 必须注意的固定长度字符串边界

本批识别用元信息只读取有界 **HDF5 固定长度字节字符串**（UTF-8 内容、每项不超过 128 字节），不能读取 HDF5 变长字符串。变长字段的表面大小只是堆引用，不能在读取之前可靠限制其解码分配；普通 h5py `attrs[name] = 'text'` 经常写出这种变长类型，因此某些完全有效的 NeXus 文件也会被本试点跳过。这不是文件损坏。

无可选择信号时，页面显示“未发现本试点支持的 NXdata”及具体受限能力说明，并提示用户自行选择 HDF5 原始数组查看器；**不会自动切换插件**。部分组不受支持时显示跳过数量；`skipped_nxdata` 是实现字段名，计数还包含无法安全辨认 `NX_class` 的组，不能当成实验数。目录达到预算时标明截断，不声称全文件搜索完成。

可参考仓库 `sandbox/tests/nexus_window_fixtures.py` 创建固定长度字符串的纯内存合成样本；不要求或自动修改用户原文件。后续扩大变长元信息支持应先设计独立的有界堆解析/隔离分配策略。

## Cordis / window 协议接入

reader 与 adapter 均为 `nexus-window`，插件 `viz-nexus-window`。先请求 `tree` 获取不透明的信号 ID 与形状；用户选择一个信号及每维 `{start, stop, step}`（终点不含）后，显式请求 `series` 或 `image`。宿主要求外层 `version`，不允许请求携带内部 HDF5 路径。

```python
nexus_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None)
validate_nexus_window_options(kind, options)
validate_nexus_window_payload(result, kind=kind, options=options, fmt=fmt,
    source_bytes=size, read_bytes=actual_bytes, read_requests=actual_requests)
```

读器复用 `array_window_reader.RangeFile` 的受控 HDF5 file-object VFD、数据类型/存储检查、选择规划及压缩块预检，不改变旧数组读器。API 主机不加载 h5py；宿主与沙箱保持同一份纯 Python 白名单校验规则，严格绑定选择、源大小、格式和实际 broker 读数。浏览器再次校验公开结果并绑定版本和信号描述。

`tree` 私有结果提供 `choices.signals`、只含清洗标签的树及预算元信息；`series/image` 增加行优先 `array.values`、每维原始索引与真实坐标、可选信号标准差。前端使用已有本地 Plotly，不向外部服务发送数据；语义相同的文件/插件轮询不重载，选择变化、停用和卸载会中止请求并释放绘图。

## 固定预算

| 项目 | 上限 |
| --- | --- |
| 源大小 | 8 GiB |
| 单次 / 累计 range 读取 | 1 MiB / 8 MiB |
| 单请求 range 次数 | 128 |
| 读取的声明属性累计 | 64 KiB |
| 目录对象 / 深度 / 可选信号 | 128 / 8 / 32 |
| 单压缩块 / 累计块解码 | 4 MiB / 16 MiB |
| 累计涉及块数 | 128 |
| 输出信号 + 坐标 + 误差标量总数 | 16,384 |
| 私有 JSON 输出（含预留包络余量） | 小于 2 MiB |

信号、坐标、误差合并预检解码预算，不分别放大。HDF5 元数据读取/预读也计入 range 总量；源大小不等于单次下载大小。回调短读或取消不重试、不改为宿主本地文件访问。由于元数据布局、块形状、预算或宿主期限，一个符合像素上限的选择仍可能被明确拒绝。

## 验证与复现

所有夹具来自合成内存 h5py HDF5，不读取用户数据。实际私有响应保存在 `frontend/tests/browser/nexus-window-data.json`，由沙箱生成器产生，供纯后端校验和前端交叉验证。

- `sandbox/tests/test_nexus_window_reader.py`：实际坐标/误差/端序，索引回退、非法元信息、链接/VDS/外部存储、压缩炸弹、累计预算、稀疏 2.5 GB 源及取消。
- `backend/tests/test_nexus_window_visualization.py`：恶意/未知字段、精度/维度、输出核算和宿主请求绑定。
- `frontend/tests/nexusWindow.test.mjs`：同一实际响应的严格验证，以及显式选择、版本绑定、稳定轮询、停用/卸载/迟到响应清理。
- 浏览器 case `domain-expansion-nexus-series`、`domain-expansion-nexus-image`：真实 Chrome/Plotly 验证曲线误差条、二维真实坐标、版本、无外联与卸载后的 Plotly 清理；图像 case 额外在真实待完成 HTTP 窗口请求中卸载，验证 AbortSignal 已取消。

浏览器复现（在 `frontend` 目录执行）：

```bash
VISUALIZATION_BROWSER_CASES=domain-expansion-nexus-series,domain-expansion-nexus-image node tests/browser/domains-smoke.mjs
```
