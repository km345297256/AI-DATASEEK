# FCS 流式细胞术原值窗口

`viz-fcs-window` 使用现有 Cordis v2 描述符、`fcs-window` reader/adapter 和私有 `dataseek-window-v1` 范围管线。生产解析仅用 Python 标准库，FastAPI 仅做纯结构验证；不改变 AgentLoop、SSE、权限与只读隔离。

## 首版格式范围

- `.fcs`，FCS 3.0/3.1 单数据集、`$MODE=L`。FCS3.1 严格 UTF-8，FCS3.0 仅 ASCII，不猜 Latin-1 或旧 `$UNICODE`。
- `F` 为 IEEE float32、`D` 为 float64，所有 PnB 必须分别是 32/64，PnE 必须 0,0；明确允许两种 `$BYTEORD`：1,2,3,4 和 4,3,2,1。
- `I` 仅支持通道等宽 8/16/32 位无符号整数，而且每个 PnR 必须等于 2^PnB。**常见的 PnB=16、PnR=1024 等需要位掩码的文件不在首版子集内**；不会将这些合法但未支持的布局称为损坏，不会悄悄 mask、改变参数范围或丢弃高位。混合位宽、非整字节紧凑打包不支持。
- 58 字节 HEADER、主 TEXT（≤256 KiB）、单 DATA。TEXT 起点≤4096且头部间隙为空格；TEXT/DATA 间隙≤4096且为纯空格或纯 NUL。无 supplemental TEXT、ANALYSIS、多数据集或尾部 CRC/厂商段；这些情况明确拒绝，不全文件降级。

所有 offset 为包含端点的精确字节位置。HEADER 与 TEXT 的 DATA offset 必须一致；DATA 末端超过 99,999,999 时 HEADER 两个 DATA 字段必须为零，仅采用 TEXT 中的明确大偏移。DATA 长度严格等于 TOT×PAR×每值字节，末端必须对应源文件长度；不猜差一字节，也不任选 HEADER/TEXT 的一套冲突数字。

TEXT 分隔符遵守双写转义，关键字大小写不敏感、重复项拒绝；已识别参数编号必须连续且在 PAR 内。只回传通道名、染料名和批准的科学字段；SRC、FIL、患者/样本名称、文件路径等原始 TEXT 不回传。界面字段有长度、Unicode 控制符、危险标记及路径规则，不将文件文字解释成 HTML 或脚本。

## 原值与图形语义

初次目录只读 HEADER/TEXT，不读取 events。之后用户明确选择通道、起始事件、事件数和视图：

- `scatter`：两个不同通道，逐事件原值点，不连线、不随机抽样。任一通道为 NaN/Inf 的事件在散点中留空，事件索引和不可绘制计数保留。
- `histogram`：一个通道和明确箱数，仅统计本次返回窗口。等宽箱左闭右开，最后一箱含最大值；常量窗口只显示该值的计数，不虚构区间；空值不计入柱高并单独报告。

PnE、PnG、TIMESTEP、PnCALIBRATION 和 PnD 均仅展示、不应用。**标定单位（例如 MESF）属于变换后数值，不是本图原值轴的单位**。标准 `$SPILLOVER` 以严格尺寸和精确通道匹配解析为只读声明；`$COMP`、`SPILL`、`SPILLOVER` 仅报告存在，不解释其厂商语义，不反演矩阵或读取外部资源。未发现声明不代表文件此前未被其他软件补偿。

浮点允许负值及超 PnR 值，不裁剪；NaN/Inf 返回 `null`。uint32 最大值 4,294,967,295 在 JSON/浏览器中可精确表达。当前线性图遇到绝对值超过 1e100 的有限原值会明确拒绝绘图，不截断、缩放或修改已验证载荷；直方图浮点精度不足以构成指定数量的严格递增边界时要求减少箱数。`sampled=true` 仅表示返回的是明确事件子窗，不表示隐式随机抽样。

## 请求、预算与生命周期

```text
fcs_window_preview(read_range, size, fmt, kind='tree', options=None, limits=None)
tree: {}
series scatter: {view:'scatter', channels:[x,y], event_offset, event_count}
series histogram: {view:'histogram', channels:[x], event_offset, event_count, bins}
```

通道/事件从 0 开始，series 必须携带初次目录的外层文件 `version`。最多 128 个通道目录、每窗 8,192 事件/16,384 数值、直方图 1～128 箱；超末尾不会暗中截断。

源≤8 GiB，每次累计读取≤8 MiB、单次≤1 MiB、≤128次，派生封装≤2 MiB。FCS DATA 按事件交错：**读的是选定事件行的完整字节，不是磁盘列投影**；只解码选定通道。事件行总字节在读取前预算，按完整行分块，选定值解码≤128 KiB。元信息解析、缓冲和图形对象仍有额外有界开销，这不是进程 RSS 精确值；沿用无网络、无数据集/宿主挂载、只读、非 root worker 与硬时限/内存边界。

后端将结果与 kind、全部选项、实际格式、源大小、读取字节和次数绑定；前端再绑定目录元信息、通道顺序、增益/标定/补偿声明及版本。换文件、停用、改通道或事件窗时同步取消旧任务并清空图形；卸载清除 Plotly 数据和元素。共享预览不批准此插件。

## 验证入口

- `sandbox/tests/test_fcs_window_reader.py`：真实范围读取、格式/偏移/UTF-8/预算，及独立 FlowIO 1.4.0 对照；`AI_DATASEEK_REQUIRE_FCS_REFERENCE=1` 强制参照测试不因缺包跳过。
- `backend/tests/test_fcs_window_visualization.py`：无需科学库的同源纯校验副本与实际请求绑定。
- `frontend/tests/fcsWindow.test.mjs`：严格载荷、直方图边界、实际组件交互和迟到响应取消。
- `sandbox/tests/fcs_window_browser_payloads.py` 产生合成实际结果，`domain-expansion-fcs-fixtures.mjs` 用真实 Vue/Plotly 验证原值散点、直方图、精度与清理。

参照库只装在一次性测试容器；FlowIO 1.4.0 构建固定 setuptools 75.8.0、wheel 0.45.1 并使用 `--no-build-isolation`，不修改生产依赖锁。合成 200,000,000 事件、约1.6 GiB 的逻辑文件，读取末两个事件只取目录前缀加16字节；不创建该体积实体文件。HTTP 可调用仅用标准库的 `sandbox/tests/fcs_window_fixtures.py:fcs_bytes()`，无需生产安装 FlowIO。

主要来源：ISAC 作者的 [FCS 3.1 规范论文](https://pmc.ncbi.nlm.nih.gov/articles/PMC2892967/)、[ISAC 规范原文（GWU 保存副本）](https://flowcytometry.gwu.edu/sites/g/files/zaskib311/files/2021-11/Data%20File%20Standard%20for%20Flow%20Cytometry%281%29.pdf)，官方 [FlowIO 读取实现](https://github.com/whitews/FlowIO/blob/master/src/flowio/flowdata.py)、[写入实现](https://github.com/whitews/FlowIO/blob/master/src/flowio/create_fcs.py)及[原始事件数据说明](https://flowio.readthedocs.io/en/latest/notebooks/flowio_tutorial.html)。本插件是明确受限的可视化子集，不表示完整 FCS 标准兼容，也不替代门控/补偿分析工具。
