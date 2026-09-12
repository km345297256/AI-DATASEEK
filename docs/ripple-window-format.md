# Ripple 电子显微谱像：受限窗口规范

插件 `viz-ripple-window`，适配器/读取器 `ripple-window`，公有契约版本 2。首版覆盖 NIST Ripple 交换格式的三维 vector 谱像，不代表完整 HyperSpy、RosettaSciIO 或仪器专有格式兼容。

## 打开和选择

将 `cube.rpl` 和 `cube.raw` 放在同一已登记、只读的数据集目录，从 `.rpl` 打开。仅匹配同目录同名且唯一的 `.raw`；不跟随头文件中的路径，也不支持孤立头文件上传。浏览器只持有不透明数据集文件引用。

- `tree`：参数 `{}`，仅读取头文件。
- `image`：显式 `channel / x / y / width / height`，单通道二维区域，最大 128×128。
- `series`：显式 `x / y / channel_start / channel_count`，一个像元的连续通道，最大 16,384 通道。
- 数值请求必须携带首次结构请求返回的双文件版本。头文件、原始数据或数据集声明变化都会使旧版本失效；即使只请求结构，也检查未读数据成员的最终状态。

## 文件方言

头文件 ≤64 KiB、Latin-1 文本、制表符键值，有两列标题行及唯一键。支持分号注释，不输出 title、源文件名或任意附加元数据。拒绝 NUL、控制字符、重复键、路径型资源指针。必须明确声明 width、height、depth、offset、data-length、data-type、byte-order、record-by。

只支持 `record-by=vector`、depth 2～1,048,576 的 `[height,width,depth]` 存储。数据类型支持 int8/uint8、int16/uint16、int32/uint32、float32/float64。多字节类型必须明确 little-endian 或 big-endian；`dont-care` 只接受单字节，不猜写入机器的字节序。不支持 image/dont-care 布局、二维图、复数、64 位整数或压缩。

raw 长度必须精确等于 offset + height×width×depth×itemsize；offset 最大 1 MiB。非有限浮点数返回空值，整数不擅自定义缺失值；不平滑、不重采样、不截断数值以伪装合法文件。

## 科学语义

按头文件明确声明的 `height/width/depth-origin`、`-scale`、`-units` 计算 `origin + index × scale`，不转换单位。声明标定却省略 origin 时使用格式约定 0，并在界面明确提示默认值；完全没有标定时使用索引，不伪造空间或能量单位。

仅有 `ev-per-chan` 时可声明 eV 步长；若与 depth-scale 或单位冲突则拒绝。零尺度、非有限尺度、数值下溢以及相邻坐标精度坍缩拒绝。负空间步长保持真实物理坐标；只有未标定行索引采用图像向下递增显示。

仅展示明确的 EELS、EDS_SEM、EDS_TEM 信号声明，缺省标为未指定。纵轴始终是原始存储强度，不自动认为是 counts，不实施增益校准、背景扣除、元素识别、能量拟合或定量分析。

## 资源和边界

配对源合计 ≤8 GiB；每次累计取回 ≤8 MiB、≤256 次、单次范围 ≤1 MiB；结果 ≤2 MiB、≤16,384 数值。窗口先计算全部实际读取跨度并预检预算，附近样本合并时跨过的字节也计数。大立方体不整文件下载、不 mmap、不传给浏览器；初次目录不读 raw。

复用现有 ENVI 双资源代理的所有权、allowlist/nofollow、stat、快照和撤权检查，但独立限定 Ripple 配对规则。worker 只有两个连续虚拟资源 `header/data`，没有文件名、磁盘挂载或网络；跨资源读取和额外字段在主机拒绝。

测试以独立 NumPy 数组核对大小端/行列/通道，并使用测试环境的 RosettaSciIO 0.14.0 官方 writer/reader 交叉验证。其 native `=` 多字节 dtype 会写出含糊的 `dont-care`，官方参照样本使用显式大端存储；生产并未放宽这一边界。RosettaSciIO 不作为生产依赖，也未复制其解析实现。

格式依据：[RosettaSciIO Ripple 支持说明](https://hyperspy.org/rosettasciio/supported_formats/ripple.html)、[NIST Ripple 格式规范](https://hyperspy.org/rosettasciio/file_specification/ripple-specs.html)、[官方读写实现](https://github.com/hyperspy/rosettasciio/blob/main/rsciio/ripple/_api.py)。
