# 仪器图像受控窗口：ESRF EDF 与 Princeton SPE 2.x

核验日期：2026-09-10。本文记录读取器的实际受限方言，不表示支持所有同后缀文件。

## 支持范围

| 文件 | 接受的布局 | 明确拒绝的布局 |
| --- | --- | --- |
| ESRF EDF | 512 字节对齐的 ASCII 花括号头；未压缩内联二维像素；单帧或最多 256 个同形状、同类型、同字节序的帧；头内明确尺寸、类型、字节序、二进制长度 | 生理 EDF/BDF；外置二进制文件；压缩数据；通用 EDFU/header inheritance；非块对齐头；三维及更高维；64 位整数；异构帧；不完整或长度矛盾的数据 |
| Princeton SPE 2.x | 明确的 `[2, 3)` 头版本；4100 字节固定头；little-endian；datatype 0/1/2/3 对应 float32/int32/int16/uint16；按行连续、同尺寸帧；最多 1,000,000 帧 | SPE 3/XML footer；缺失版本；未知 datatype；多传感器 ROI 拼接；overscan；非零 geometric 标志；帧数量与源大小不符 |

ESRF 支持 int8、uint8、int16、uint16、int32、uint32、float32、float64 和明确的大小端声明。SPE 如声明一个传感器 ROI，分组后的尺寸必须严格吻合存储尺寸；输出中的像素 ROI 仍相对于存储帧，而不是传感器坐标。

SPE 的部分 uint32 代码在不同上游读器中有不同映射。本轮只采用双方一致的 0–3 子集，不能根据扩展名或像素大小猜测其他代码。

## 只读边界与显示含义

- 读取器仅持有 `read_range(offset, length)` 字节能力，不知道文件路径、用户、凭据或外部 URL；不读 sidecar、不落盘、不安装仪器厂商 SDK。
- 源文件上限 8 GiB，单次范围上限 1 MiB，累计读取上限 32 MiB，最多 2048 次读取。仪器预算不改变原生理 EDF/BDF 的 128 次限制。
- 结构请求只读取头部。ESRF 会跳过二进制像素，验证全部允许帧的头；单头不超过 64 KiB，累计头不超过 1 MiB。SPE 只读固定的 4100 字节。
- 图像请求必须显式提供帧和 `[x, y, width, height]`；宽高各不超过 1024，且需外层版本 pin。相邻连续行合并读取，窄 ROI 不暗中下载整行或整帧。大 ROI 仍可能因读取次数、总量或宿主时限被拒绝。
- 图像是所选 ROI 有限原值的 min–max 8 位灰度显示，非有限值显示为黑色并报告数量。原始数据不改变，不宣称强度校准、能量/波长校准、衍射积分、科学拟合或诊断。常数窗口及全非有限窗口显示为黑色并明确范围。
- 不返回自由文本实验头、评论、路径、患者/设备身份。前端只接受受限 PNG，使用可撤销 Blob URL，不把源路径或数据放入浏览器存储。

## 独立模块及验证

- `sandbox/app/services/instrument_window_reader.py`：独立标准库布局解析、范围计划、取消检查及显示 PNG 编码。
- `backend/app/application/services/instrument_visualization.py`：纯标准库协议校验；精确绑定源大小、实际读取计数、格式、视图类型及选择；校验 PNG CRC、压缩流终止、像素字节长度和预算。
- `frontend/src/visualizations/extended/InstrumentImagePreview.vue` 与 `instrumentImageData.ts`：先结构后显式选择，版本/布局绑定，完整文件及插件配置指纹，取消、切换及停用后丢弃旧结果并释放 Blob。
- `sandbox/tests/instrument_window_fixtures.py`：完全合成的规范头和数据，含只分配小片段的 5 GB 逻辑稀疏源；不包含真实用户数据。
- `sandbox/tests/instrument_window_browser_payloads.py` 生成 `frontend/tests/browser/instrument-image-data.json`：四个真实 reader 输出，可跨后端、前端与浏览器回归使用。图像夹具选择为帧 1、ROI `[1, 1, 2, 2]`。

共享窗口编排、插件目录和界面路由由主线集成；此文对应独立模块，不单独改变 AgentLoop、SSE、部署栈或宿主数据隔离边界。

## 一手依据与许可证

[FabIO 的官方 EDF 实现](https://github.com/silx-kit/fabio/blob/main/src/fabio/edfimage.py) 明确记录 EDF 数据类型、字节序、维度以及内联/外置二进制块；当前实现文件声明 MIT。也可查阅 [FabIO 官方 API 文档](https://www.silx.org/doc/fabio/latest/api/modules.html)。本轮未复制或嵌入 FabIO 源码，也没有安装 FabIO 依赖。

[ImageIO 官方 SPE reader](https://github.com/imageio/imageio/blob/master/imageio/plugins/spe.py) 的 `Spec.basic` 列出读取所需头偏移、版本、尺寸、帧数和固定数据起点；[ImageIO LICENSE](https://github.com/imageio/imageio/blob/master/LICENSE) 为 BSD-2-Clause。本輪依据这些字段独立实现范围读取，没有引入 ImageIO parser。

[OME Bio-Formats 的 SPE reader](https://github.com/ome/bioformats/blob/develop/components/formats-gpl/src/loci/formats/in/SPEReader.java) 可用于交叉核对固定头及帧布局，其 GPL-2.0-or-later 源码没有复制或集成。其扩展 datatype 与 ImageIO 的不同也是本轮拒绝额外代码的原因。

## SPC / WDF 后续所需资料

本轮不注册 SPC 或 WDF 的空实现。继续接入前至少需要：

1. 厂商/上游明确允许使用的版本规范，特别是 SPC 多子谱/X 轴和 WDF block、数据类型、偏移及校准/单位规则。
2. 可再分发的脱敏小样本：单谱、多谱/映射、多轴和不同版本，各有已知正确的数值、坐标与单位；不能只有后缀统计。
3. 对压缩、外部数据、文本字段、文件损坏及错误偏移的处理边界；缺少这些信息时不推断波数轴或单位。
4. 读器及其依赖许可证核验，以及通过本项目 range、输出 schema、权限、版本与取消回归的证据。
