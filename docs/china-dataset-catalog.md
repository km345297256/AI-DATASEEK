# 国内科学数据目录与访问边界

记录日期：2026-09-07。面向本机 DataSeek 数据分析；使用入口、管理和导入方法见[数据集管理说明](dataset-management.md)。

## 本批实际准备范围

ScienceDB、国家青藏高原科学数据中心（TPDC）、国家基因组科学数据中心（NGDC/GWH）各完成 **10 个**，化学化工科学数据中心（ChemDC）已完成 **2 个，另 8 个待获取**；当前共 **32 个不同数据集**，真实实体文件保存在：

```text
/Users/luchangfa/Documents/Codex/Data/open-catalog/<source>/<dataset_id>/
```

源标识分别为 `scidb`、`tpdc`、`ngdc`、`chemdc`。这是一批适合本机分析的代表性数据，不是下载四家机构的全部数据库，不是机构官方推荐榜单，也不是把同一个数据集的多个文件拆开充数。部分是单项研究全包，部分是明确标注的轻量子集。

| 来源 | 不同数据集数 | 清单文件数 | 实体目录总字节数 |
| --- | ---: | ---: | ---: |
| ScienceDB | 10 | 236 | 134,869,869 |
| TPDC | 10 | 127 | 262,622,240 |
| NGDC/GWH | 10 | 30 | 32,494,204 |
| ChemDC（已完成部分） | 2 | 11 | 2,279,814 |
| 合计 | **32** | **404** | **432,266,127** |

约 412.24 MiB。统计来自已完成实体清单聚合；**包含保留的压缩原包、无损解压文件、配套文件和 `SOURCE.md`**，不代表 404 份独立观测，也不是仅网络传输量。不要把原包与其解压副本重复计入分析。此数字不包含原有 18 个国际开放样例，也不包含用户本地长期登记。

每个目录的 `SOURCE.md` 保留作者、来源页、DOI/accession、许可证及范围。仓库保存 [external-datasets 清单](../backend/app/resources/external-datasets)、[已审阅的公开下载描述](../backend/app/resources/external-descriptors)和应用代码，**本批实体不放进 Git，也不发布到公网**。文件大小与 SHA-256 构成本次完整性基线，并非发布机构数字签名。

## ScienceDB：10 个

[ScienceDB 官方门户](https://www.scidb.cn/)。保留真实公开附件；不同研究的取样范围如下。

| # | 数据集 / 官方来源 | 系统领域 | 主要实体格式 | 许可 | 本批范围 |
| --- | --- | --- | --- | --- | --- |
| 1 | [三角龙湾 SJLW-2 湖泊岩芯代用指标](https://www.scidb.cn/en/detail?dataSetId=e83385d5bb244101a06840f35ffcb97e) | `geoscience` | XLSX | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 完整 V1：1 个 XLSX；保留列名与单位。 |
| 2 | [帕米尔高原极端升温过程（1961—2017）](https://www.scidb.cn/en/detail?dataSetId=633694461058088963) | `geoscience` | CSV；ZIP/RAR 原包 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 完整 V1 的 3 个历史命名原包；ZIP 分目录展开为 CSV，RAR 保留；分析时先核对重复版本。 |
| 3 | [马铃薯施肥与产量田间试验](https://www.scidb.cn/en/detail?dataSetId=8f2879f50cf34d4cba2cffea657f3183) | `tabular` | XLSX | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 完整 V1：1 个田间试验 XLSX，非合成示例。 |
| 4 | [二氧化碳等离激元传感器计算数据](https://www.scidb.cn/en/detail?dataSetId=9a1b3383cfa54d90b067a26828b3fa0d) | `spectroscopy` | XLSX,M | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 完整 V1：17 个 XLSX/MATLAB 文件；`.m` 仅保留，不执行。 |
| 5 | [医用同位素核反应截面实验数据](https://www.scidb.cn/en/detail?dataSetId=79656e6850d442daa7dae2e984f8d47f) | `tabular` | CSV | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 完整 V1：13 个实验 CSV；仅数据分析，不涉及生产或设施操作。 |
| 6 | [ANUVAD/ANUVUD 分子结构计算样例](https://www.scidb.cn/en/detail?dataSetId=3df4a7fb82a64f879a6bd84749253bbe) | `chemistry` | MOL,CML,JPG | [CC0](https://creativecommons.org/publicdomain/zero/1.0/) | 精选 MOL/CML/JPG 3 文件；不是完整计算档案，受限 `.log` 未获取。 |
| 7 | [中巴经济走廊冰川冰湖灾害清单](https://www.scidb.cn/en/detail?dataSetId=776016954459684864) | `geoscience` | SHP；ZIP 原包 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 完整 V1 ZIP；含 22 组 Shapefile 及配套属性/投影文件，保留原包。 |
| 8 | [FAST FRB 121102 爆发事件及动态谱样例](https://www.scidb.cn/en/detail?dataSetId=f172ff40142c4100855724b80a085deb) | `space` | CSV,NPY,PNG,PDF | [CC0](https://creativecommons.org/publicdomain/zero/1.0/) | V4 子集：完整 1652 事件 CSV、Burst 433 NPY/PNG、补充表 PDF；不是原始 FITS 全库。 |
| 9 | [银河宇宙线理论通量比较数据](https://www.scidb.cn/en/detail?dataSetId=1de994ad3c4140dba319b1522d7113b9) | `space` | TXT | [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) | 完整 V1：7 个理论通量 TXT；不是实测粒子事件。 |
| 10 | [一串红克罗烷生物合成关联数据](https://www.scidb.cn/en/detail?dataSetId=8e5c50c6eb244689967ac6047ea47d97) | `spectroscopy` | XLSX,PPTX | [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) | 完整 V1：2 个 XLSX 与 1 个 PPTX；光谱展示不等于原始仪器谱。 |

## TPDC：10 个

[国家青藏高原科学数据中心](https://data.tpdc.ac.cn/home)。本批十条均从官方详情确认 A 类开放获取、在线、已发布；采用官网正常免登录下载流程。未申请 B/C 类数据、未创建 FTP 用户或提交下载申请。

| # | 数据集 / 官方来源 | 系统领域 | 主要实体格式 | 许可 | 本批范围 |
| --- | --- | --- | --- | --- | --- |
| 1 | [青藏高原边界数据总集](https://data.tpdc.ac.cn/zh-hans/data/61701a2b-31e5-41bf-b0a3-607c2a9bd3b3) | `geoscience` | SHP | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方全包；5 类边界口径分别保留。 |
| 2 | [青藏高原大于1平方公里湖泊水量变化（1976-2020）v2.0](https://data.tpdc.ac.cn/zh-hans/data/5e537e53-9532-4acd-9b17-65ff43bb0eff) | `geoscience` | SHP / XLSX | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方全包；SHP 与 XLSX，两个历史 RAR 分目录展开；年份以文件为准。 |
| 3 | [亚洲黄土释光测年与古气候代用指标数据集](https://data.tpdc.ac.cn/zh-hans/data/d478815c-2192-4cc3-8036-62f03e9ec1ec) | `geoscience` | XLSX | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 官方全包；黄土年代与古气候代理指标工作簿。 |
| 4 | [西阿尔卑斯沉积物Zn同位素数据（2023）](https://data.tpdc.ac.cn/zh-hans/data/29c18c26-adef-451b-a4bc-2b1eb56ef6b5) | `geoscience` | XLSX | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 官方全包；地质样品 Zn 同位素表格。 |
| 5 | [青藏高原降水稳定氧18同位素数据集（1991–2008）](https://data.tpdc.ac.cn/zh-hans/data/8b5495b9-7d52-41e8-ba62-8c06e7131950) | `geoscience` | XLS | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方全包；月尺度站点 δ18O，不是全国格点降水。 |
| 6 | [中国植物功能型图（1 km）](https://data.tpdc.ac.cn/zh-hans/data/ab193a70-63a5-4df6-9bc1-d9b5ac5fb044) | `geoscience` | Arc/Info Binary Grid（ADF） | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方全包；ADF 目录分类栅格，保留投影/属性/说明，不当作逐年连续变量。 |
| 7 | [青藏高原多年冻土综合监测数据集（2002-2018）](https://data.tpdc.ac.cn/zh-hans/data/789e838e-16ac-4539-bb7e-906217305a1d) | `geoscience` | XLSX / DOCX | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方全包；4 个 XLSX 与站点说明 DOCX，各站时段不同。 |
| 8 | [青海湖水文气象数据（1956-2020）](https://data.tpdc.ac.cn/zh-hans/data/12cb9320-e6ba-4938-a9ac-5b0ebb656825) | `geoscience` | XLSX | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方全包；水位、径流、湖温、冰情、气温/降水，变量时段不同。 |
| 9 | [青藏高原与周边地区冰川变化及其与大气环流关系（1970s-2000s）](https://data.tpdc.ac.cn/zh-hans/data/439b01bd-1799-4171-b9ed-16e82ccc43df) | `geoscience` | XLSX | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方完整补充表工作簿；不是逐条冰川高精矢量编目。 |
| 10 | [北半球多年冻土气候-生态系统敏感性分区图（2000-2016）](https://data.tpdc.ac.cn/zh-hans/data/53b58111-e5a9-45c3-989f-af028b6866c1) | `geoscience` | SHP | [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) | 官方全包；实际为多年冻土分区矢量 SHP/DBF，附辅助文件，不是栅格。 |

TPDC 的 `metadata.fileSize` 是源文件总量，不一定等于网站动态打包 ZIP 的大小；本地清单使用实际收到的文件字节和 SHA-256。湖泊水量、植物功能型、北半球冻土三个包的内嵌 ZIP/RAR 已单独、无损展开并保留原包，减少每次分析时重复解包。展开限制为每集 10,000 文件、累计解压 256 MiB，拒绝越界路径、符号链接、加密条目和覆盖现有文件。

## NGDC/GWH：10 个

[国家基因组科学数据中心](https://ngdc.cncb.ac.cn/)。十条是不同物种（茶为变种）的独立组装 accession；全部保留官方已发布 FASTA 和原始 GZIP，不自行截取为演示序列。前五条是叶绿体，后五条是微生物/酵母组装；不含患者或个人人类基因组数据。

| # | 数据集 / 官方来源 | 系统领域 | 主要实体格式 | 许可 | 本批范围 |
| --- | --- | --- | --- | --- | --- |
| 1 | [小球藻完整叶绿体基因组](https://ngdc.cncb.ac.cn/gwh/Assembly/9671/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 完整发布的叶绿体 FASTA；不含核基因组。 |
| 2 | [阿萨姆茶完整叶绿体基因组](https://ngdc.cncb.ac.cn/gwh/Assembly/212/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 完整发布的叶绿体 FASTA；不含核基因组。 |
| 3 | [西双版纳柑橘DYC002叶绿体组装](https://ngdc.cncb.ac.cn/gwh/Assembly/66231/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 完整发布的叶绿体 FASTA；官方级别 Scaffold，不称无缺口完整染色体。 |
| 4 | [参薯完整叶绿体基因组](https://ngdc.cncb.ac.cn/gwh/Assembly/24173/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 完整发布的叶绿体 FASTA；不含核基因组。 |
| 5 | [小叶杨完整叶绿体基因组](https://ngdc.cncb.ac.cn/gwh/Assembly/8848/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 完整发布的叶绿体 FASTA；不含核基因组。 |
| 6 | [枯草芽孢杆菌TM114-134基因组组装](https://ngdc.cncb.ac.cn/gwh/Assembly/29089/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 该菌株完整发布的组装文件；Scaffold 草图。 |
| 7 | [植物乳植杆菌AF91-04IFC-1A基因组组装](https://ngdc.cncb.ac.cn/gwh/Assembly/27930/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 该菌株完整发布的组装文件；Scaffold 草图。 |
| 8 | [动物双歧杆菌Nourse12001完整基因组](https://ngdc.cncb.ac.cn/gwh/Assembly/111608/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 该菌株完整发布的组装文件；官方级别 Complete。 |
| 9 | [粟酒裂殖酵母SP-LT18基因组组装](https://ngdc.cncb.ac.cn/gwh/Assembly/29521/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 该菌株完整发布的组装文件；Scaffold 草图。 |
| 10 | [乳酸乳球菌AF03-11基因组组装](https://ngdc.cncb.ac.cn/gwh/Assembly/27511/show) | `sequence` | FASTA + FASTA.GZ | [GWH 学术使用条款](https://ngdc.cncb.ac.cn/gwh/statistics/genome) | 该菌株完整发布的组装文件；Scaffold 草图。 |

许可不是 CC0/CC BY：GWH 页面说明学术研究可免费使用，商业使用需要联系取得许可。使用时应引用 NGDC、GWH 及 accession；无 DOI 的记录不编造 DOI。三个菌株虽同属一个较大的 BioProject，仍是三个物种的独立组装；不是将一个 accession 的多个文件当成多个数据集。

## ChemDC：10 个候选，已完成 2 个

[用户指定的 ChemDC HTTP 门户](http://chemdc.casdc.cn/home)可访问；核验时该域 HTTPS 连接不可用。用户已通过 Chrome 自行登录。下表第 3、8 条已取得完整原文件并登记，**其余 8 条仍未取得完整实体、不登记为可探查数据集，也不计入已完成数量**。

官方前端下载流程除了 `privacyPolicy.type=open`，还检查下载权限；此前匿名身份无法下载。本次使用用户已登录的 Chrome 正常页面下载，未提取 Cookie、账号口令或认证下载地址，未提交新的申请、未替用户同意条款。Chrome 对实际 HTTP 文件下载提示“不安全的下载”，由用户自行处理；未关闭保护、未代点“保留”，也未改用其他网络工具绕过拦截。

第 8 条催化剂数据的 PDF（2,042,630 字节）和 DOCX（25,095 字节）已下载齐全，逐项大小与官方目录一致；原件保留在下载目录，复制时保留安全属性。另附 2,638 字节的 `SOURCE.md`。通过现有沙箱镜像只读验证：PDF 17 页均可提取文本，DOCX 39 段、0 表、CRC/XML 有效且无嵌入宏。**这是可读文档，不是已提取好的数值表**；图表数字和实验条件仍需在探查时核对。正式清单采用人工获取证据及哈希，不伪造 ChemDC 匿名自动下载描述。

第 3 条的 6 份 XLSX 和 1 份 DOCX 已全部经用户在 Chrome 中自行保留并完成下载，原件共 204,197 字节。7/7 官方文件名、逐文件大小、Office ZIP CRC 和内容解析全部通过；本地 SHA-256 作为后续完整性基线。按官网目录层级保留扩展名前空格，另附 5,254 字节的 `SOURCE.md`，共 8 个库存文件、209,451 字节。6 个 XLSX 共 14 个工作表、12,395 个数值单元格，0 个公式；说明 DOCX 为 27 段、无表格。均未执行宏、求值公式或跟随外链。

该套表格含 1,070 个文本 `--` 占位符和 13 个真实数值零，两者不能混同；有布局空列/空行及 V/mV、A/pA 等单位差异。重复 X/Y 列对须保留材料和实验条件，Fig3/Fig.3e 的 `erro bar` 不能擅自解释为标准差或标准误。来源说明保留这些注意事项，未清洗或修改原件。

文件夹列表没有整包下载按钮；官网另有“FTP下载”入口，但尚未确认服务可用性或是否支持加密传输，不能将其视为绕过浏览器提示的办法。剩余 8 套的批量获取需用户继续处理提示，或由中心提供安全的官方批量传输方式。

| # | 候选数据集 | DOI / 官方入口 | 文件目录提示 |
| --- | --- | --- | --- |
| 1 | 基于碳纳米环对称破缺性实现室温三态单分子开关论文数据集 | [10.57841/casdc.0007020](https://doi.org/10.57841/casdc.0007020) | JPG/PNG/PXP/DOCX；Igor 专有格式 |
| 2 | 分子半导体中自旋输运的结构异构效应论文数据集 | [10.57841/casdc.0007018](https://doi.org/10.57841/casdc.0007018) | TXT/XLS(X)/图像及仪器文件 |
| 3 | 室温有机自旋电子器件通过电光补偿策略实现宽范围磁电流调控和多功能性论文数据集 | [10.57841/casdc.0006995](https://doi.org/10.57841/casdc.0006995) | **已登记**：完整 6 XLSX + DOCX；电光—磁电流实验 |
| 4 | 有机微晶阵列在超低磁场下产生巨磁光致发光用于片上光学磁强计论文数据集 | [10.57841/casdc.0006994](https://doi.org/10.57841/casdc.0006994) | XLS/PNG/DOCX；摘要材料名称待核对 |
| 5 | 电场驱动曲率诱导选择性C–C键断裂论文数据集 | [10.57841/casdc.0006873](https://doi.org/10.57841/casdc.0006873) | JPG/PXP/DOCX；STM/电学资料 |
| 6 | 苯并二吡咯类A-DA′D-A型小分子受体的构效关系研究 | [10.57841/casdc.0006858](https://doi.org/10.57841/casdc.0006858) | TXT/JPG/DOCX；结构—性能 |
| 7 | 锂离子电池跨条件荷电状态估计的无监督领域适应框架论文数据集 | [10.57841/casdc.0006778](https://doi.org/10.57841/casdc.0006778) | TXT/PDF/IPYNB/DOCX；SOC/SOH 元数据不一致 |
| 8 | In2O3/HZSM-5二氧化碳加氢制汽油催化剂的性能数据集 | [10.57841/casdc.0002039](https://doi.org/10.57841/casdc.0002039) | **已登记**：完整 PDF/DOCX；不是现成 CSV 表 |
| 9 | 汽爆秸秆高固酶解发酵过程强化工程技术数据集 | [10.57841/casdc.0002439](https://doi.org/10.57841/casdc.0002439) | DOCX；不能声称已有原始 Excel |
| 10 | 水电解用高稳定性PEEK增强聚芳基哌啶阴离子交换膜论文数据集 | [10.57841/casdc.0005902](https://doi.org/10.57841/casdc.0005902) | TIFF/XLSX/DTA/PLR/OPJU 等仪器格式 |

十条详情均标注 CC BY 4.0，但 **“允许怎样使用数据的许可证”和“能否匿名取得文件的访问授权”是两件事**。公开元数据/目录以及 CC 许可都不是跳过登录、验证码、申请或受保护下载流程的授权。用户按官网正常流程获取文件后，再放入本机 Data 下独立目录登记；不得用占位 CSV、说明文档或网页截图冒充尚未下载的实验数据。

## 元数据、格式与分析注意

- **题名不一致 1：ANUVAD/ANUVUD。** ScienceDB 官方题名为 ANUVAD，正文和文件名为 ANUVUD。保留映射，不擅自将二者合并为已验证的同名化合物；本批只取得结构相关子集，403 的日志未获取。
- **题名不一致 2：电池 SOC/SOH。** ChemDC 电池条目题名是 SOC/无监督领域适应，摘要描述 SOH/联邦迁移。尚未下载，必须在正常取得实体后核对说明，不声称取得完整 MIT 原始全集。
- ChemDC 有机微晶条目的摘要材料名称另有不一致，秸秆条目描述 Excel/图像而公开目录只有 DOCX；仍属待核对项。NGDC 柑橘名称含 complete，但官方级别为 Scaffold，以组装级别和实际文件为准。
- **专有格式不改后缀伪装。** ADF 必须保留目录及配套信息，由支持 AIG 的 GDAL/raster 工具读取；不能只拿 PNG 预览当原分类数据。ChemDC 的 PXP、DTA、PLR、OPJU、GData 等保留真实格式，可能需要相应软件或经授权转换，不承诺当前工具全部能解析。
- Shapefile 的 SHP/SHX/DBF/PRJ 等成组保留。北半球冻土分区是矢量，不是栅格；各边界版本和湖泊数据历史包分别保留，不能将其重复相加。
- **源数据几何质量警告：** 实际读取发现，中巴经济走廊部分图层含无效几何，最多的图层为 21,800 个要素中 1,776 个；北半球冻土为 160,328 个要素中 3,775 个；湖泊两套历史图层各为 1,132 个要素中 39 个。这不妨碍读取，但空间叠加、面积统计前应检查几何及坐标系。此次没有修写原件；如需修复，应把派生产物写到会话输出目录。
- FAST 的 NPY 应以 `allow_pickle=False` 读取。MATLAB 源文件、notebook、宏仅作为资料，不因下载而执行。PPTX 光谱展示不等于原始仪器谱。
- CC BY 保留署名；CC BY-NC 和 CC BY-NC-SA 的非商业限制仍然存在，不能统一称为“无限制开源”。数据页中附加的引用要求和机构条款也应保留。
- 数据实存本机后，常规文件读取不再依赖源网站；DeepSeek/其他远程模型推理仍取决于用户配置和网络，并不因此变成离线模型。

## 导入与状态说明

经过审阅的元数据清单位于 `backend/app/resources/external-datasets/<source>/<dataset_id>.json`；下载准备与管理员登记是分离步骤，浏览器不具备任意 URL 下载或将私人数据标为公开精选的能力。新目录先核验真实格式、全量文件、大小和哈希，再通过精确只读挂载的管理员命令校验/登记，操作示例见[数据集管理说明](dataset-management.md#管理员导入已准备的公开实体)。

## 首批 30 集本机验收（2026-09-07）

- 30 个新数据集已完成管理员预校验和实际登记，全部启用；列表共 49 条，即原有 18 个样例 + 本批 30 个 + 1 个本地长期登记。
- 实际内容验收使用现有沙箱镜像、`ubuntu`（UID 1000）、只读数据挂载，没有为验收额外安装分析库。30/30 通过，393 个文件大小和 SHA-256 全部一致；133 次格式读取检查覆盖 XLS/XLSX、CSV、数值 TXT、NPY、MOL、SHP、ADF 和 FASTA。
- 10 个 NGDC 组装共 161 条序列、24,710,848 个碱基，全部通过 IUPAC 字母检查；压缩和解压副本统计一致。来源附带的 2 个 MATLAB 文件未执行。
- 桌面 1440 px、手机 390 px 均逐个点击全部 49 张卡片进入对应探查页；通过来源/领域交叉筛选、搜索、列表刷新、本地长期记录保留和模拟登记跳转。审计 1,126 个 API 响应及 104 次浏览器存储检查，未发现宿主路径泄露；没有新增真实会话、真实登记测试数据或付费模型调用。
- 部署仍为单一 Compose，前端仅 `127.0.0.1:7001`，宿主直接访问 `/datasets` 返回 HTTP 200。更新前后 7 个已完成会话、422 条事件、62 个文件记录、114 条 Token 使用记录及运行时指纹一致；Cordis 保持 healthy、15 插件、280 工具。
- 回归检查：后端全量 **1,268 通过、29 跳过**（跳过项为需要独立在线栈的旧集成用例及已被 Agent 工具循环取代的旧快速路径用例）；沙箱严格地学依赖模式 **190 通过**；前端 **81 通过**，类型检查、生产构建、Compose 配置及 `git diff --check` 均通过。后端镜像站曾超时，改用官方 PyPI 后完整重跑成功；没有将安装失败算作测试通过。

上述为首次 30 集导入时的验收记录，ChemDC 的增量验收另列，不将旧计数当作当前会话基线。这些检查证明已验收文件可读取、数据集入口与持久化链路正常，不等同于对每种科学分析方法、所有专有格式或模型回答正确性的保证。

## ChemDC 增量验收（2026-09-07）

- 新增 `cn-chemdc-0002039` 一套；精确只读挂载下 dry-run 和 apply 均通过，3 文件（2 原文件 + 来源说明）全部与清单大小、SHA-256 一致。
- 完成后再次对全部 **31 套 / 396 文件**执行管理员只读 dry-run，全部通过，旧三来源清单与文件内容仍匹配；此轮没有再次写入登记。
- 目录测试 **4 通过**，保留旧 30 集及其 3 组公开下载描述，单独验证新人工获取清单、来源证据与文档解析边界。
- 桌面 1440 px、手机 390 px 实际读取均为 **50 条**（49 精选 + 1 本地），100 次卡片进入探查、154 次筛选、358 次无横向溢出检查通过。1,148 份 API 响应及 106 次 URL/storage 检查未发现宿主路径泄露；102 次推荐问题和 2 次登记均 mock，未发起真实模型调用或测试登记。ChemDC 两屏均显示完整 3 文件和说明。
- 完整回归重跑：后端 **1,271 通过、29 跳过**，沙箱严格地学模式 **190 通过、0 跳过**，前端 **81 通过**，类型检查、生产构建、Compose 配置和补丁格式检查通过。原有依赖警告仍存在，包括测试 JWT 短密钥、Jupyter OpenAPI 重复 operation ID、科学库警告，以及前端 3Dmol eval、嵌套 CSS 和大 chunk 提示；本次没有将其声称为已修复。
- 本次只新增文件和受控登记，不重建/重启应用、不更改 AgentLoop、SSE、模型或插件。最终运行时 healthy / Cordis / 15 插件 / 280 工具；记录数仍为 7 个 completed 会话、476 条事件、64 个文件记录、128 条 Token 使用记录。含更新时间的会话摘要指纹前后存在差异，因此不声称所有会话字段逐字节不变；浏览器测试的非 GET/HEAD/OPTIONS 真实请求均被禁止，目录前后核对未变。
- 对摘要差异进行有界只读核查：浏览器回归窗口（01:57:19–01:58:01 UTC）附近的后端日志中，1,039 个可识别访问请求没有 POST/PUT/PATCH/DELETE，会话级请求仅 `GET /api/v1/sessions`；该接口、数据集会话列表和数据产品列表的查询链均为只读查询。现有日志不记录每次数据库写操作，尚不能解释摘要变化，亦不能排除其他后台或并行操作；没有据此回写或重置任何用户状态。

## ChemDC 第二套增量验收（2026-09-07）

- `cn-chemdc-0006995` 的剩余 4 个文件通过官网登录页面请求下载，用户自行处理 Chrome 提示。全部 7 原件独立只读验收通过，重算哈希/Office ZIP CRC 后才生成完整清单，并按官方目录保留文件名及空格；安全属性与下载原件保留。
- 新清单 8 文件（含 `SOURCE.md`）完整匹配；ChemDC 两套 dry-run/apply 成功，首次催化剂登记幂等保留。之后对全部 **32 套 / 404 文件**再次只读 dry-run，全通过。
- 目录、宿主文件校验、导入 CLI、数据集管理四组定向测试 **42 通过**，无跳过/警告；Compose 配置与补丁格式检查通过。本次没有应用代码/依赖改动，也没有重建或重启服务；前一轮全量回归结果见上节，不冒充本轮重新执行的结果。
- 桌面 1440 px、手机 390 px 均显示 **51 条**（50 精选 + 1 本地）。102 次卡片进入、158 次筛选、102 次完整文件树核对及 468 次无横向溢出检查通过；0006995 的长标题、8 文件、单官方子目录及缩进均正确。1,170 份 API 响应和 108 次 URL/storage 检查未见宿主路径；104 次推荐问题和 2 次登记均 mock，没有发起真实模型调用或测试登记。新报告保存在 `browser-artifacts-chemdc6995`，前一轮 50 条目录的结果另存保留。
- 本轮重新建立导入前基线，分别记录状态、更新时间、数据集绑定、会话文件的摘要；导入及页面验收后上述各字段摘要均一致，7 个 completed 会话、476 条事件、64 个文件记录、128 条 Token 使用记录保持一致。该结果只适用于本次第二套导入窗口，不用于抹去上一节记录的历史摘要差异。
