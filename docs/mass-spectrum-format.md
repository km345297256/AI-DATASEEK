# 质谱预览：MGF 峰表与受限 mzML 1.1

`viz-mass-spectrum` 是独立 Cordis 可视化插件；reader / adapter 为 `mass-spectrum`，仅有只读 `preview`，`input_mode=whole`、`shared=false`。复用统一 v2、文件身份与版本、无网络隔离 worker，不替换 Agent、SSE、MCA 能谱或其他科学查看器，也不发起 AnalysisJob。

## 支持范围

| 项目 | 当前明确支持 |
| --- | --- |
| MGF | ASCII `BEGIN IONS` / `END IONS` MS/MS 块；每行恰好两列 m/z 与强度；多个谱独立列入目录 |
| MGF 科学语义 | 峰列表按 centroid 棒谱显示，原顺序与重复 m/z 保留；强度单位未知，不声称绝对计数或额外标定 |
| mzML | 1.1.0 / 1.1.1 命名空间内单 run / spectrumList，可带 indexedmzML 包装；索引按源序号，索引区不当作授权能力 |
| mzML 数组 | 恰好内联 m/z、intensity 两数组；明确 32/64 位 IEEE 浮点、小端；无压缩或标准 zlib，再用 Base64 编码 |
| mzML 表示 | 恰好一个 centroid/profile CV；未声明、冲突或受限方言不支持时在目录标明不可选，不擅自判断峰型 |
| 元信息 | MS level、单 scan 的明确保留时间（秒或分钟）、单前体单 selected ion 的 m/z；不默选多个 scan / 前体 |
| 单位 | m/z 的明确 `MS:1000040`；强度只保留声明 CV accession，已知计数/百分比/计数率显示可读标签，未知 CV 显示原编码，不换算 |

首版不支持单独 PMF/混合序列查询、MGF 三列峰电荷/注释、多个 PEPMASS、时间区间、mzXML、mzMLb、imzML、Numpress、外部二进制数组、额外数组、引用式参数组、自动总离子流/提取离子流色谱或跨谱叠加。普通源文件、软件与词汇表的描述性 URI 不执行、不取回、不传给浏览器；实际数组外部引用不受支持。MGF 的 TITLE、RAWFILE、用户字段以及 mzML 原生 ID/路径元信息全部隐藏，目录名称只用 `Spectrum N`。

目录中不可选不等于文件损坏，只表示该谱不在本批受控范围。用户可切换既有原文/其他查看器继续检查，不自动回退到未知解析器。

## 显示与精度

- centroid 使用一峰一条基线棒线，峰与峰之间插入断点，不连接、不拟合。profile 连接原始点，但要求 m/z 严格递增；遇到乱序或重复坐标拒绝，不排序、插值或合并重复点。
- IEEE 32 位值按文件真实精度展开成双精度；64 位值保持 IEEE 双精度。MGF 十进制文本以双精度显示，不宣称保留任意十进制有效数字。非有限值、数值溢出和文本非零下溢拒绝。
- 原始负强度保留，零不是缺失值，不强制抬基线、截负值、归一化或扣背景。原始强度单位来自文件声明；文件未声明时清楚标记未知。
- retention time 保持声明的 `s` 或 `min`，不自动换算。前体 m/z 不是完整化合物鉴定，不运行谱库检索、肽段搜索、峰提取或定量。
- 前端只加载项目已有本地 Plotly，使用双精度 scatter 的线段/曲线；关闭简化、跨空值连接与在线资源请求。关闭、切换、插件停用或版本变化时取消请求并 purge 图形。

## 输入、目录与解码预算

源文件最大 16 MiB，最多 1,024 谱；每页最多 64 个目录项；显式单谱最多 16,384 对 m/z/intensity，即 32,768 个返回标量，统一输出总量不超过 2 MiB。这只针对新质谱 reader，不扩大旧插件的通用数组预算。

整文件读取是明确的能力，不宣称支持大型 mzML 的真正范围读取。每次请求均取回完整的有界源；mzML 目录只检查 XML 元信息和数组声明，**不 Base64 解码、不 zlib 展开、不读取数值**。MGF 数值以原文形式存在，目录会遍历并验证行，但不返回峰数组。不会截取前 16,384 点冒充完整单谱；超出点预算则明确不可选。

XML 禁止 DTD、实体与处理指令；最多 100,000 元素、24 层、每元素 20 属性，非数组元信息累计不超过 1 MiB。MGF 最多 500,000 行，行长 4,096，元信息不超过 1 MiB。XML 严格检查谱 count/index/ID 唯一性、数组 count、dtype、压缩与声明长度；未知数组编码不进入解码器。

Base64 在分配前检查长度，使用严格字母表与规范补位；每个浮点数组最多 128 KiB 原始数据。zlib 单次最多产生声明长度加 1 个字节，随后核验 exact length / EOF / unused_data / unconsumed_tail，不使用无界 `decompress` 或 `flush`，不接受尾随流、截断流或声明炸弹。双数组合计最大 256 KiB 原始数值，不需要引入新 native 解码库。

## 请求与结果

```python
mass_spectrum_preview(data: bytes, fmt: str, kind="tree", options=None,
    output_limit=2 * 1024**2)
validate_mass_spectrum_options(kind, options)
validate_mass_spectrum_payload(result, *, kind=None, options=None,
    fmt=None, size=None, limit=2 * 1024**2)
```

`tree` 使用 `{}` 或 `{offset:0}`，统一规范化为 `{offset:0}`；后续 offset 必须是 64 的倍数并带外层文件 version。`series` 使用 `{spectrum:"s-000000"}`，要求显式选择及外层 version。谱 ID 是源内稳定序号，不承担身份或文件授权；宿主依然逐次核验 owner、当前文件、启停状态、目录代际和版本。

私有 `kind=tree/series` 归一化为公有 `tree/array`。series 返回 `array.shape=[N,2]`、`dimensions=["m/z","intensity"]` 和原始双列值；metadata 绑定 source_bytes、格式/方言、total_spectra、offset/next_offset、decoded_bytes/output_points。三层严格校验结构与选择，不接受未知字段、HTML、路径、模糊单位推断或伪造计数。

## 规范与验证来源

采用 [Matrix Science MGF 官方说明](https://www.matrixscience.com/help/data_file_help.html)、[HUPO-PSI mzML 规范仓库](https://github.com/HUPO-PSI/mzML)、[PSI-MS controlled vocabulary](https://github.com/HUPO-PSI/psi-ms-CV) 解释格式和表示术语（2026-09-10 核对）。术语表按其 CC-BY-4.0 许可引用；未打包下载完整词汇表到生产 reader。

生产使用标准库与已有 defusedxml，无新增依赖。测试容器临时安装 [Pyteomics 5.0.1](https://pyteomics.readthedocs.io/en/latest/api/mzml.html) 和 [psims 1.4.0](https://github.com/mobiusklein/psims)（均 Apache-2.0）：使用包内离线词汇表做独立数值 oracle，以及官方 writer 生成合成 mzML。不会下载科学数据或使用用户数据；不把可选 hdf5plugin 缺失警告当作质谱功能缺失，本批不启用 mzMLb 或 Blosc。

入口：`sandbox/tests/test_mass_spectrum_reader.py`、`backend/tests/test_mass_spectrum_visualization.py`、`frontend/tests/massSpectrum.test.mjs`；真实读取器返回保存在 `frontend/tests/browser/mass-spectrum-data.json`，浏览器 case 覆盖 MGF centroid、mzML profile、目录分页、版本绑定和未决请求卸载取消。HTTP 验收可使用 `sandbox/tests/mass_spectrum_fixtures.py` 的 `mgf_bytes()` / `mzml_bytes()`，均仅内存构造合成样例。
