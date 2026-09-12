# 一维衍射／散射扫描插件

`viz-diffraction` 使用 `diffraction` 读取器和适配器。仍走 Cordis v2、现有整文件隔离 worker 与不透明文件授权；不进入 Agent/SSE，不改变旧 XY 或 XML 结构树优先级。

## 首版格式

- **XRDML `.xrdml`**：明确命名空间 `http://www.xrdml.com/XRDMeasurement/1.0`～`1.7` 或 `2.0`～`2.4` 的 Completed 一维扫描。扫描轴支持 `2Theta`、`Omega`，以及 `2Theta-Omega`、`Omega-2Theta`、`Gonio`。明确双轴的前两种耦合扫描必须同时提供两轴，单位相同，且坐标满足固定偏移关系。`Gonio` 用文件明确提供的 `2Theta` 绘制一维曲线，允许省略 `Omega`，不补造缺失坐标；若文件提供 `Omega`，仍须满足相同单位及 `Omega = 2Theta / 2`。其他变化轴或互相矛盾的轴拒绝。角度必须显式 `deg`，不自行接受弧度或转换 q。
- `positions` 可以是明确 `startPosition/endPosition`，按强度点数展开；也可以是逐点 `listPositions`，保留非等间距和递减扫描，不排序或重采样。其他轴只能为 `commonPosition`。至少两个点，坐标严格单调。
- XRDML 1.x 使用 `intensities unit="counts"`；2.x 使用 `counts unit="counts"`。二者均保持**文件存储值**。1.x intensities 按厂商定义可能已包含衰减修正，不能冒称未经处理的原始探测器计数。计数时间、衰减和发散因子只验证长度／正有限值，不应用；不自动转为 cps，也不以平方根臆造误差。
- **canSAS1d `.xml`**：严格根元素与命名空间 `cansas1d/1.0`（version 1.0）或 `urn:cansas1d:1.1`（version 1.1）。每个 `SASentry/SASdata` 是单独可选扫描；每个 `Idata` 必须含一个明确单位的 `Q` 与 `I`。Q 允许 `1/A`、`1/nm`、`1/cm`、`1/m`；I 允许 `1/cm`、`1/mm`、`1/m`、`a.u.`、`none`、`counts`、`cps`、`counts/s`、`counts/sec`。原单位原值显示，不换算。负 I 可以显示，Q 不得为负。
- 可选 `Qdev/Idev` 必须在该扫描每点都有，有限非负、单位与对应 Q/I 一致。按**文件声明的不确定度**画误差线，不把它必然解释为 1σ。`dQw/dQl` 是分辨率宽度而不是通用误差，本期明确拒绝；其他数据列、SESANS 或多维数据同样拒绝，不选择首两列冒充。

以上是安全读取子集，**不是完整 XSD 验证器**。无单位 ASCII XY、通用 CHI、任意 XML 不视为自动兼容；NXcanSAS HDF5 不由本插件接管。无结构求解、峰识别、Rietveld 精修、背景扣除、方位积分或单位归一化功能。GUI 只画线性坐标，不悄悄丢弃零／负强度来生成对数图。

## 交互、预算与隔离

首次请求 `kind: tree, options: {}`，只返回安全序号扫描目录，不返回科学数组。用户选择后显式请求 `kind: series, options: {scan: 0}`，同时携带初次文件 `version`；没有隐式选择多扫描中的第一条。整个 XML 仍需有界读取和解析，不能把 tree 的小响应称为文件范围读取。

输入 UTF-8 XML ≤16 MiB；最多32扫描，每扫描2～16,384点，全文件合计≤65,536点；输出统一封装≤2 MiB。XML深度≤32、节点≤350,000，属性数和文本长度有界；数字词法≤64字符、有限值、拒绝浮点下溢和超出 binary64 安全整数范围的整数字面值。超限直接拒绝，不抽样、不截断、不补零。

DTD、实体声明、处理指令／XSLT、XInclude 和外部 href/src/base 明确拒绝。命名空间和 schemaLocation 不会触发网络请求或加载 XSD。样本标题、文件路径、仪器注释均不返回；公开扫描标签固定为 `Scan N`。API、沙箱和前端各自校验扫描、单位、类型、预算；前端再绑定文件大小、后缀、version 和初次完整目录。切换、停用或关闭时取消请求，清理捕获到的实际 Plotly 实例；迟到响应不得重新挂载。

## 上游依据与实现方式

本项目自行实现受限标准库 XML 解析；没有复制 GPL 读取器、安装桌面软件或增加生产依赖。绘图复用项目已有 Plotly。

- [Malvern Panalytical 官方 Data Collector / XRDML 规范下载页](https://www.malvernpanalytical.com/en/products/category/software/x-ray-diffraction-software/data-collector)：核验厂商发布的1.x与2.x XSD、counts/intensities差异、位置展开公式和耦合轴定义；不分发厂商 XSD。
- [xrayutilities 官方读取器源码](https://github.com/dkriegner/xrayutilities/blob/main/lib/xrayutilities/io/panalytical_xml.py)，[维护者生成的代码文档](https://xrayutilities.sourceforge.io/_modules/xrayutilities/io/panalytical_xml.html)：作为位置读取交叉参考。其默认计数时间归一化与本插件不同，不照搬其归一化结果；GPL-2.0-or-later，不复制实现。
- [canSAS1d 官方 Idata 字段规范](https://www.cansas.org/formats/canSAS1d/1.1/doc/element_SASdata.html)：Q/I 与 Idev/Qdev 的声明、全点一致性及单位规则。
- [SasView/sasdata 官方项目](https://github.com/SasView/sasdata)用于格式生态参考；未作为生产依赖。

回归入口：`sandbox/tests/test_diffraction_reader.py`、`frontend/tests/diffractionVisualizations.test.mjs`、`frontend/tests/browser/domain-expansion-diffraction-fixtures.mjs`。所有夹具均为原创合成数据，浏览器测试走已有本机路由拦截 harness，不调用生产 API 或创建分析会话。

独立上游对照入口：`sandbox/tests/diffraction_reference_check.py`。只在已有科学环境的一次性容器以 `--no-deps` 安装固定 `xrayutilities==1.8.0`、`sasdata==0.11.0` 后，以 `PYTHONPATH=/workspace/sandbox:/workspace/sandbox/tests` 显式运行该脚本；缺少依赖时失败而不是跳过。脚本保留官方包资源路径，直接执行未改动的 config/helper/IO 模块，不执行会导入无关 Fortran 拟合器的顶层初始化，也不模拟科学函数或解析结果；不加入生产依赖。它比较 XRDML 的位置与存储强度（明确还原上游默认除以计数时间的处理），并用 SasView 官方 writer 生成原创 canSAS XML，再由双方 reader 对照 Q/I/Qdev/Idev；这不是对整个上游分析工具的功能验收。
