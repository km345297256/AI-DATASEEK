# MiniSEED / SAC 只读波形窗口

读取器与适配器为 `seismic-window`，沿用现有 Cordis 公有协议 v2、文件授权与私有 `dataseek-window-v1` 范围管线，不进入 Agent 工具或 SSE。生产读取器只使用 Python 标准库；后端使用独立纯载荷验证，不导入科学库。

## 明确支持的子集

- MiniSEED v2 数据记录，后缀 `.mseed`、`.miniseed`。数据质量标识 D/R/Q/M，每条记录必须有 Blockette 1000；可有 100（覆盖名义采样率）和 1001（微秒偏移、时间质量、帧数）。首条记录声明固定的 2^8～2^20 字节记录长度，文件大小必须整除，当前页内每条记录重新校验长度。大小端必须明确且头部与数据字节序一致。
- 编码：INT16、INT32、FLOAT32、FLOAT64、STEIM1、STEIM2。压缩记录按其声明样本数预限，再有界整数积分；核验帧布局、编码控制位与终值 Xn。第一差分属于前记录关系，不加入本记录首样本；int32 积分采用格式规定的有符号 32 位回绕。
- SAC 二进制 NVHDR 6/7、IFTYPE=ITIME、LEVEN=true，单段 float32 样本，大小端明确。v7 从末尾双精度 footer 读取 DELTA/B；不把 v6 的浮点时间伪装成双精度来源。文件长度严格匹配 632 字节头、NPTS×4 字节样本及 v7 的 176 字节 footer。

发现首条记录长度前，Blockette 链只能在首 256 字节内读取；取得并校验 Blockette 1000 后才按其记录大小继续。已知记录槽的所有指针在读取前受槽边界限制。首条 B1000 置于 256 字节之外的布局不在首版支持范围内，不能先跨记录读取再事后报错。

不支持 MiniSEED v3、全 SEED 控制卷、可变记录长度整卷、未知 blockette/编码、发生闰秒的记录、SAC ASCII、频谱、不等间隔、复数或多分量 SAC。`.ms` 未注册，避免抢占过泛后缀。未实现的能力明确拒绝，不猜格式或回退全文件解码。

## 交互与科学语义

初次 `tree` 仅检查记录头，不读取样本数组。MiniSEED 目录展示的是由首条记录长度计算的固定长度槽；每页最多 16 条，只保证本页头部已检查，不能据此声称整个文件已验证。目录可以直接定位远端槽，不线性扫描跳过的全部记录。

`series` 必须明确选择一条记录、起始样本和样本数，并携带首次目录取得的同一文件 version。目录翻页也沿用该 version。横坐标始终是相对此记录首样本的秒数，不是跨记录压缩后的伪连续时间；UTC 起始时间另外展示到微秒精度。

MiniSEED 按 activity flag 判断是否需要施加 FSDH 时间修正，再加入 Blockette 1001 微秒偏移，避免重复修正。名义采样率按 factor/multiplier 的正负规则计算；存在 Blockette 100 则使用其实际采样率。当前页同通道按文件顺序提示时间缺口、重叠/乱序或采样率变化；标识缺失或被安全替换的记录不作同通道连续性判断。不同记录永不自动拼接，不填补或重采样。质量标志只展示，不自动掩膜最大整数值或其他可能的标志样本。

MiniSEED 不携带完整仪器响应/单位信息，单位为未知，不擅自称为 counts、位移或速度。SAC 只显示明确 IDEP 单位枚举，SCALE 单独展示但不应用。所有样本均为原始存储值，不滤波、不去响应、不物理校正。浮点 NaN/Inf 显示为空且不跨空值连线；有限值与 int32 整数保持原精度。

## 私有参数与结果

函数：`seismic_window_preview(read_range, size, fmt, kind='tree', options=None, limits=None)`。

- `tree`：`{}`，或严格 `{record_offset, record_limit}`，页大小 1～16。
- `series`：严格 `{record, start_sample, sample_count}`，起点从 0 开始、样本数 1～16,384，不能超出所选记录。没有隐式截断或抽样。
- 元信息、记录目录和科学语义使用固定字段；`series` 返回单一 trace 的 `x/y` 与 record/label/unit。后端绑定 kind、完整 options、文件格式、源大小和实际范围读取计数；前端再核验目录与波形一致，文件/插件/选区变化同步取消旧请求。

源上限 8 GiB；一次调用累计取回 ≤8 MiB、每次 ≤1 MiB、≤128 次范围请求；输出封装 ≤2 MiB。STEIM 单条记录解码 ≤65,535 样本，之后才取至多 16,384 点窗口；未压缩格式和 SAC 直接读取所选样本字节。预览预算不是进程 RSS，仍保留现有无网络、只读、非 root、无宿主数据挂载的一次性 worker 及硬内存/时限。

## 独立格式核验

测试以合成内容为限。额外在一次性测试容器安装 ObsPy 1.4.2，使用其 libmseed 写出并独立读回各编码/大小端/跨帧记录，与本读取器逐样本比较；这不是生产依赖，也不修改锁文件。设置 `AI_DATASEEK_REQUIRE_SEISMIC_REFERENCE=1` 可使验收因缺少参照库而失败，不能把跳过参照测试算作格式通过。

异常输入检查有意比部分旧版本严格：ObsPy 1.4.2 自带的旧 libmseed 会忽略末使用帧中样本数之外的保留 STEIM2 dnib；本插件仍检查该帧所有控制字段并拒绝。回归如实记录参考库接受该合成异常而本插件拒绝，不把它宣称为参考库一致拒绝，也不将本子集等同于完整 libmseed 兼容性。

独立模块测试为 `sandbox/tests/test_seismic_window_reader.py`、`backend/tests/test_seismic_window_visualization.py` 与 `frontend/tests/seismicWindow.test.mjs`。实际浏览器夹具由 `sandbox/tests/seismic_window_browser_payloads.py` 生成，覆盖 STEIM2、SAC7、跨目录页后选择单记录；检查真实 Plotly 的坐标/原始值、显式请求数量和卸载清理。另有源长 1,200,000,632 字节的合成逻辑 SAC，只取 632 字节头和末尾 16 字节样本，不创建大实体文件，也不借用真实用户文件证明范围读取。

规范来源：FDSN [SEED 2.4 手册](https://www.fdsn.org/pdf/SEEDManual_V2.4.pdf)，EarthScope [libmseed 解码器](https://github.com/EarthScope/libmseed/blob/main/unpackdata.c)，IRIS [SAC 时间与 v7 精度说明](https://ds.iris.edu/files/sac-manual/manual/tutorial.html)，ObsPy 官方 [SAC 字段定义](https://github.com/obspy/obspy/blob/master/obspy/io/sac/header.py)。实现边界以上述受限子集及回归为准，不表示完整替代 ObsPy、SAC 或 libmseed。
