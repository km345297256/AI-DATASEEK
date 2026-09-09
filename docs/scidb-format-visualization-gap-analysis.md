# SciDB 文件格式统计驱动的可视化插件增量建议

分析日期：2026-09-09。状态：**选型分析，待用户确认；本轮未集成、安装项目依赖、启动转换任务或修改系统功能。**

依据：用户提供的 `sdb文件后缀统计.xls`（`Sheet1!A1:C6064`）、分析时的 DataSeek 工作区代码，以及各工具的官方仓库／文档。代码审计不是部署验收，上游支持列表也不等于本系统已经支持。

## 1. 结论

最值得投入的方向是 **“补强高频格式的读取与多视图能力，再增加有数据依据的专业读取器”**，不是继续堆叠绘图库。

- **先增强已有能力**：HDF5、NetCDF、TIFF 的表内数量合计 **1,051,373，占 52.95%**。优先补内容识别、真实窗口／分块读取、变量和坐标语义，以及同文件的地图／曲线／热图／显微视图选择。
- **通用新增**：压缩包目录与成员预览、JSON/XML/YAML 结构树、视频播放／时间定位、音频波形／频谱。这些目前没有对应的完整适配器，且有明确数量依据。
- **专业新增或读取器增强**：EDF/BDF 生理信号、CZI/SCN 显微数据、MCA 仪器谱、Origin 工程静态数据、ProcSpec 光学谱、Bruker 已处理核磁谱。
- **先确认样本再决定**：`.ar/.zap/.zFTp` 的脉冲星候选非常值得调查，但目前不能仅凭后缀确认；IMA、SCN、EDF、MCA、SPC 同样有判型要求。

已有 Plotly、H5Web、Viv、NiiVue、NMRium、IGV、OpenLayers 等应复用，不能再次列成“尚未集成的新工具”。用户管理的是独立场景插件，而不是安装多少个第三方库。

## 2. 统计口径与限制

### 2.1 读表结果

| 指标 | 结果 |
| --- | ---: |
| 原始标签记录，不含表头 | 6,063 |
| 去首尾空白、去前导点、统一大小写后的标签数 | 5,989 |
| `count` 列合计 | **1,985,705** |
| 数量为 1 的规范化标签 | 4,832 |
| 前 10 个规范化标签的数量占比 | 82.63% |
| 前 20 个规范化标签的数量占比 | 90.22% |

数量列有 6,062 个文本值和 1 个数值，已统一按非负整数解析；无无效数量。采用两套独立聚合核对总量和主要分组，结果一致。原工作簿未修改。

**分母是这张表的 `count` 合计，不是本轮重新盘点的 SciDB 当前全站文件数。** 表中没有明确的统计日期、去重规则、文件大小、所属数据集数、压缩成员或实际文件头。不能据此计算实际可预览成功率、存储字节占比或受益用户数。

5,989 是标签数，不是 5,989 种真实格式：其中有完整文件名、哈希式名称、参数文件名和空白标签；空白标签对应的 55 个文件仍计入总量。长尾不适合机械地“一后缀一插件”。

### 2.2 与当前功能最相关的数量

各行按列明后缀求和；比例统一除以 1,985,705。下面是部分分组，不是完整学科分类。

| 后缀集合 | 数量 | 占比 | 对选型的含义 |
| --- | ---: | ---: | --- |
| `h5/hdf5` | 414,469 | 20.87% | 增强 H5Web／科学读取器，不另装同类查看器 |
| `nc/nc4` | 360,485 | 18.15% | 保留地图和数值视图分离，判别 NetCDF3／4 |
| `tif/tiff` | 276,419 | 13.92% | 区分普通 TIFF、GeoTIFF、OME-TIFF，而非单一图像插件 |
| `png/jpg/jpeg/bmp/gif/webp/svg` | 450,014 | 22.66% | 多数已有基础图片入口；不能据此声称全部编码均验收 |
| `gz/zip/rar/7z/tar/tgz/bz2/xz` | 60,399 | 3.04% | 增加安全容器入口；不知道内部实际格式 |
| `json/xml/yaml/yml` | 52,372 | 2.64% | 增加只读结构树／表格投影，区别于当前纯文本 |
| `mp4/avi/mov/mkv/wmv/mpg/mts` 及 `wav/mp3/ogg` | 24,753 | 1.25% | 新增科研音视频场景；不含待判型的 `movie/oggu` |
| `csv/tsv/xls/xlsx/pdf/doc/docx/ppt/pptx` | 52,795 | 2.66% | 对应工具已有，应做互通与回归而非重复引入 |
| `mat/npy/npz` | 14,702 | 0.74% | 已有数值入口，但需补 MAT v7.3 路由等边界 |

前三组合计 52.95%；加常见图片后为 75.61%。**这些是格式需求量的集中度，不是当前或未来必然达到的覆盖率。** 同一科学实验可能产生很多帧／切片／配套文件，数量不能代替独立实验数。

## 3. 当前规范下，哪些属于增强而不是新增工具

分析时工作区有 **34 个可视化插件清单，全部声明 Contract v2**，批准能力统一定义于 [visualization-adapters.json](../contracts/visualization-adapters.json)。前端、Cordis 主机和后端使用由它生成的批准组合。当前实际适配范围明显小于上游软件的全部能力。

| 已有能力 | 当前实际限制／空缺 | 本轮推荐增量 |
| --- | --- | --- |
| H5Web + NetCDF 地图／曲线 | H5Web 适合 HDF5 型容器，不是经典 NetCDF3；输入仍有有界全文件阶段 | 容器内容探测、维度／单位／缺失值保真、窗口读取；无地理坐标时不强行提供地图 |
| Plotly + MAT/NPY/NPZ | 普通 MAT 数值读取；MAT v7.3 失败后尚无完整自动路由到 HDF5 的入口 | 普通 MAT → 当前 reader；v7.3 → HDF5 reader。拒绝对象执行／pickle，复用曲线和热图 |
| TIFF / OpenLayers / Viv | 地图子集限制投影和波段；Viv 只接单文件 `.ome.tif/.ome.tiff` 等明确子集 | 普通 `.tif` 内的 Geo/OME 内容探测、多波段／通道、可选地图与显微视图；大图做 ROI／瓦片 |
| OpenLayers 地理图 | 不是 GDAL 全格式浏览器 | 增强 GDAL 读取：`asc` 9,985、`grd` 6,256、`kml` 2,860；识别方言，确认 CRS，不猜地理含义 |
| NMRium | 已处理 1D JCAMP AFFN/PAC 子集 | 接 Bruker 已处理谱读取器及受限配套资源，不再引入第二个 NMR 工作台 |
| IGV / FastQC | IGV 为 BED/VCF 子集；FASTQ QC 不支持任意 gzip／大型多样本 | 受控压缩、参考序列／索引集合与窗口能力就绪后扩展；不能把全部 `.gz` 算作 FASTQ |
| NiiVue / vtk.js / Mol* | 体数据、网格、结构各有严格格式和输入限制 | NIfTI gzip、DICOM、二进制网格、密度或轨迹按实际样本增加 reader／资源模型 |

地理读取建议复用 [GDAL](https://github.com/OSGeo/gdal)（MIT）与既有地图引擎。例如 [AAIGrid](https://gdal.org/en/stable/drivers/raster/aaigrid.html) 可对应一类 ASC，[Surfer Binary Grid](https://gdal.org/en/stable/drivers/raster/gsbg.html) 是一类 GRD；**后缀相同不表示一定属于这些方言**。HDR 还可能是 ENVI 或医学配对体数据，必须和已授权主文件关联后判型。

## 4. 建议优先新增的能力清单

编号仅用于本报告确认，不沿用上一轮工具清单编号。优先级和成本属于工程判断；“数量依据”表示潜在需求，不是兼容承诺。工具库可能只是读取器，浏览器显示优先复用当前 trusted adapter。

### 4.1 通用入口：可优先推进

| 编号 | 数据依据 | 工具／主许可证 | 用户获得的能力 | Cordis 接入与成本 |
| --- | --- | --- | --- | --- |
| A1 | 压缩容器 **60,399**，其中 GZ 42,344、ZIP 15,107 | [libarchive](https://github.com/libarchive/libarchive)，以 BSD-2 类许可为主、部分文件另行许可；也可复用现有解包实现的安全规则 | 包目录、成员类型／大小、点击成员再选择可视化；GZ 是压缩流，不伪造多个目录成员 | **新容器入口，中高。**输出分页树和 opaque member ID；不得默认全量解包，扫描字节数、成员数、嵌套层数、膨胀量都有限额 |
| A2 | JSON 31,189、XML 19,813、YAML/YML 1,370 | [vanilla-jsoneditor / svelte-jsoneditor](https://github.com/josdejong/svelte-jsoneditor)，[ISC](https://github.com/josdejong/svelte-jsoneditor/blob/develop/LICENSE.md) | 只读树、类型／路径、检索、规则化记录的表格投影；后续数值字段送已有 Plotly | **新结构化视图，中。**Vue 使用 vanilla 入口并懒加载；XML/YAML 需自己的安全 parser 和类型转换，不是该库原生通吃。关闭编辑、远程 schema、外部实体与危险 YAML 构造 |
| A3 | 视频 **23,545**，其中 MP4 23,078；音频 **1,208** | [Video.js](https://github.com/videojs/video.js)，Apache-2.0；[WaveSurfer.js](https://github.com/katspaugh/wavesurfer.js)，BSD-3-Clause | 实验视频播放与时间定位，音频波形／频谱；后续同步事件轨道 | **新媒体场景，基础中、大文件中高。**拆开视频、波形、频谱插件。播放器不能解决所有编码；长音频需预计算 peaks，带权限 seek/range。精确逐帧解码、转码或整段频谱另做受控作业 |
| A4 | EPS 5,260、PS 713，合计 **5,973** | [Ghostscript](https://github.com/ArtifexSoftware/ghostpdl)，[AGPL-3.0-or-later](https://github.com/ArtifexSoftware/ghostpdl/blob/master/LICENSE) | 已有科研图版静态预览 | **读取转换增强，中且安全／许可成本较高。**隔离、有界转 PNG/PDF 后复用现有 image/PDF.js，不嵌入 PostScript 执行环境。适合 A1–A3 后补充 |

A1 是数据入口基础设施，不是新的科学图型，但能让包内已有查看器真正被发现和使用。ZIP/NPZ/Office 现有内部安全检查，不等于已经有用户级压缩包浏览器。

A3 可选用 [FFmpeg / ffprobe](https://github.com/FFmpeg/FFmpeg) 读取媒体信息、生成有界预览。其主体 LGPL、部分可选组件 GPL，需按实际构建核验，不能标成统一 MIT。浏览器播放器只承诺经过编解码样本验收的子集。不得自动播放带音轨内容或连接外部字幕、编码服务。

A4 的解释器处理不可信程序语言，需无网络、非 root、只读原文件、CPU／内存／页数／像素／时间限制，不能只靠一个命令行“安全模式”。[libarchive 的细分许可说明](https://github.com/libarchive/libarchive/blob/master/COPYING)也应在锁版本时纳入依赖清单。

### 4.2 专业增量：建议试点，先锁定可验收方言

| 编号 | 数据依据 | 工具／主许可证 | 推荐范围 | 接入方式／成本 |
| --- | --- | --- | --- | --- |
| B1 | EDF **3,687**、BDF **446** | [PyEDFlib](https://github.com/holgern/pyedflib) + 按需 [MNE-Python](https://github.com/mne-tools/mne-python)，均 BSD-3-Clause | EEG/ECG/睡眠等多通道波形，通道单位、时间窗、注释、频谱 | **新领域插件，中。**轻量 reader 读局部通道＋时间窗，Plotly 复用；不要默认载入整个 MNE 分析流程；保留每通道采样率和任何重采样说明 |
| B2 | CZI **3,467**、SCN **2,045** | [BioIO](https://github.com/bioio-devs/bioio) 核心 BSD-3；[bioio-czi](https://github.com/bioio-devs/bioio-czi) GPL-3；可选 [Bio-Formats](https://github.com/ome/bioformats) 主项目 GPL-2、[OpenSlide](https://github.com/openslide/openslide) LGPL-2.1 | CZI 多通道／Z/T／场景；经识别的 SCN 显微或病理图像 | **现有 Viv 的读取扩展，高。**先有界切片，后金字塔／ROI；大型转换须显式作业。不要同时默认装多套重型 reader，更不能按 BioIO 核心许可推断全部依赖许可 |
| B3 | MCA **4,885** | [PyMca](https://github.com/silx-kit/pymca)，核心 MIT；[GUI 依赖许可另算](https://pymca.sourceforge.net/license.html) | 经判型的多道谱、道数／能量—计数、多谱比较 | **新仪器 reader，中。**复用 Plotly，不嵌入 Qt GUI。没有标定系数时仅显示 channel/counts，不能杜撰能量轴；不默认做拟合或定量元素分析 |
| B4 | OPJ **3,439**、OPJU **1,040** | [liborigin](https://github.com/gerlachs/liborigin)，GPL 系列，主仓库 GPL-3 | Origin 工程树、工作表、矩阵和静态曲线重建 | **新工程 reader，中高，条件试点。**在隔离进程中提取数值；不执行 LabTalk/Python/公式／外链。旧 OPJ 优先验收，OPJU 及较新版本需逐项验证；不承诺复刻完整原图／工程 UI |
| B5 | ProcSpec **1,636**、SPC **729** | [lightr](https://github.com/ropensci/lightr)，[GPL ≥2](https://github.com/ropensci/lightr/blob/main/DESCRIPTION) | 经判型的光学反射／透射／吸收谱，波长和仪器元数据 | **新仪器 reader，中高。**复用曲线适配器；需要受控 R worker，有额外运行时成本。OceanOptics 与其他厂商 SPC 不是同一个方言，不按后缀统一套 reader |
| B6 | `procs` 703、`proc` 702、`acqus` 669、`1r` 642、`1i` 623、`fid` 615 | [nmrglue](https://github.com/jjhelmus/nmrglue)，BSD-3-Clause；复用已有 NMRium | Bruker 已处理核磁谱、参数和配对分量；后续原始 FID 时域 | **现有 NMRium 的输入扩展，中高，依赖资源集合。**`1r/procs/acqus` 按角色授权；处理缩放和 ppm 来源要可追溯，不能把上列数量相加称为实验数 |

#### B1：EDF 生理信号有真实样例支持，但仍需内容探测

表中 EDF 示例 `dataSetId=778740145531650048` 对应的 [PSG-Audio 原作者数据论文](https://www.nature.com/articles/s41597-021-00977-w)说明数据使用 European Data Format，含多导睡眠和同步音频，伴随 RML 睡眠阶段／事件注释。因此 **B1 是有样例支持的新增领域**，以后可把 RML 作为批准伴随资源，以安全 XML 解析接事件轨道。

但 EDF 也可能是 ESRF 衍射探测器数据。此类应考虑 [FabIO](https://github.com/silx-kit/fabio) 读取后复用二维图像／热图，而不是送给 MNE。不能将全表 3,687 个 EDF 都称为 EEG 文件。生理预览只显示白名单元数据，隐藏身份字段，不作诊断工具。

#### B2：SCN 与显微生态需避免错误承诺

[Leica SCN](https://openslide.org/formats/leica/) 与 [Bio-Rad SCN](https://docs.openmicroscopy.org/bio-formats/6.3.0/formats/bio-rad-scn.html)不是同一种格式。OpenSlide 的 Leica reader 还有荧光和焦平面限制，不能保证全表 SCN 都可读取。CZI 拼接／场景／压缩的实际分布也需样本确定。

不建议新接 [AICSImageIO](https://github.com/AllenCellModeling/aicsimageio)：其仓库已于 2025-12-01 归档并指向 BioIO。新接 reader 应按文件内容与锁定能力选择，不能让“最后安装的读取器”隐式获得优先权。

#### B4／B6：工程或实验目录不只是单个文件

liborigin 的兼容说明不能外推为所有 OPJU 版本支持，也不是 LGPL 库；以[实际接口和源码许可](https://github.com/gerlachs/liborigin/blob/master/OriginAnyParser.h)复核。建议用用户代表样本验收工作表数、列数、矩阵形状、数值和缺失值，而非仅测试能打开文件。

nmrglue 的 [Bruker 读取](https://nmrglue.readthedocs.io/en/latest/reference/bruker.html)依赖配套参数；[read_pdata](https://nmrglue.readthedocs.io/en/latest/reference/generated/nmrglue.fileio.bruker.read_pdata.html)会依据 `procs` 做缩放，且已处理数据没有低内存 reader。必须限制总输入和解码数组，不能只限制返回 JSON。原始 FID 的傅里叶变换、相位／基线处理属于有参数和来源记录的 AnalysisJob，不在点击预览时静默完成。

## 5. 数量较小或证据待补、但值得保留的方向

| 编号 | 表内依据 | 建议与必要条件 |
| --- | --- | --- |
| C1 | `ar` 11,393、`zap` 7,672、`zFTp` 7,669，合计 **26,734** | **PSRCHIVE 脉冲星预览，先抽样判型。**三者同一示例短链；官方 `observation.ar`、RFI 标记与 `pam -FTp` 用法与其命名吻合，但本轮未获得样例文件头，仍是推断。确认后做轮廓／相位—时间／相位—频率／偏振视图，复用 Plotly。上游现为 GitLab，不把 GitHub 非官方镜像当官方来源；AFL-2.1。见[官方文档](https://psrchive.readthedocs.io/en/latest/quickstart.html)、[pam](https://psrchive.readthedocs.io/en/latest/manuals/pam.html)、[迁移与许可](https://psrchive.sourceforge.net/) |
| C2 | AB1 **681**、FASTA **764**、GB **746** | [Biopython](https://github.com/biopython/biopython) reader → Sanger 四通道峰图／质量、序列和 GenBank 特征轨道；不重复装 IGV。Biopython License／部分文件 BSD 双许可，不能整体标 BSD。ABI 染料映射读取元数据，FASTA 不伪造注释 |
| C3 | IMA **1,889**、NII **216** | [pydicom](https://github.com/pydicom/pydicom)（MIT）+ 既有 NiiVue；需要专门 DICOM 交互再评估 [Cornerstone3D](https://github.com/cornerstonejs/cornerstone3D)（MIT）。IMA 先判型，先单实例后授权 series；序列排序、像素方向／间距／强度换算、压缩编码与身份脱敏均需验收；NII 已有，不作为新覆盖重复计算 |
| C4 | FCS **394** | [FlowKit](https://github.com/whitews/FlowKit)（BSD-3）→ 流式细胞通道散点／密度／直方图，复用 Plotly；补偿和坐标变换需显式标注，门控和全量分析走 Job |
| C5 | SEGY **557**、SGY **288**，合计 **845** | [segyio](https://github.com/equinor/segyio)（LGPL-3）→ 地震道和剖面，复用曲线／热图；需要道头、采样间隔、大小端和几何验证。上游当前 2.0 是预发布且开发暂停，若选用应评估维护中的 1.x 和实际数据版本 |
| C6 | TLE **753** | [satellite.js](https://github.com/shashwatak/satellite-js) → 扩展现有 Cesium 的离线轨道展示；限制传播时间窗／采样点、标明历元与传播模型，不能把传播轨迹当作实测轨迹 |

C1 虽数量较高，却不因猜测被提前登记为“支持”；一旦真实样本确认，可提升到专业试点首批。`.ar` 没有计入 A1 压缩容器总量。

本表中的 `dat/raw/bin` 等泛型二进制、`sav/dta/fig` 等歧义标签，以及 `fca/btn/ktp/kgs/kta` 等尚未确认的仪器标签，先建立“待识别”队列，不编造格式解释。源表部分 URL 标注“保护中”，本轮没有尝试绕过权限访问。

## 6. 对 Cordis 规范的适应性要求

### 6.1 保留现有宿主边界

继续使用 Cordis 的注册／启停、可信适配器、统一授权入口和 `VisualizationResult`。不改 AgentLoop、PlanActFlow、SSE、FastAPI 主机及 Docker 数据隔离。新增可视化无需模型自动选择后执行任意代码。

实现时先修改批准能力源 [contracts/visualization-adapters.json](../contracts/visualization-adapters.json)，同步生成前端／Node／Python 定义，再添加 reader、adapter、清单和测试。当前解析器严格拒绝未知字段及不批准的组合，**不是往 JSON 写上新后缀或 `range` 就实现了新能力**。若改变公共字段语义，需显式版本迁移与兼容测试，不悄悄扩展已有 v2 响应。

当前 `view_kind` 没有独立的音视频类别；`VisualizationResult` 中存在 `media/resources` 类型，也不代表媒体播放器、通用关联文件或流式读取已实现。需分别落实数据模型与生命周期。

### 6.2 三个优先补齐的基础能力

| 能力 | 当前缺口 | 建议设计，尚未实施 |
| --- | --- | --- |
| **内容画像与多视图匹配** | 前端主要按文件名／后缀匹配 | 有界读取魔数／头部后返回容器、方言、版本、shape、dtype、坐标／单位与置信信息。让同一文件提供多个适用场景，不以优先级永久独占；不能让客户端指定任意 reader 或路径 |
| **受授权的资源集合** | 已有 `resource_id` 主要用于 Shapefile 等特定关联，不是通用目录授权 | 服务端生成不透明集合和成员 ID，批准 `main/index/reference/header/data/annotation/chunk` 角色及关系；同所有者／文件范围、版本、成员总数与总字节校验。Bruker、DICOM、RML、BAM、压缩成员均可复用 |
| **真正的范围／窗口／瓦片读取** | 批准输入模式为 `whole/page/prefix`；内部存储 range 不等于浏览器可按需读取大型文件 | 增加受控 bytes offset/length、通道时间窗、数组切片、ROI／瓦片等明确能力；每类有请求和解码预算。COG、SCN、长 EDF／媒体、Zarr 不能靠放大全文件上限解决 |

依据：[当前匹配与目录校验](../frontend/src/visualizations/contract.ts)、[统一授权入口](../backend/app/application/services/unified_visualization.py)、[数据集伴随文件边界](../backend/app/application/services/dataset_file_preview.py)。字段名仅为设计建议，未宣称当前协议已经接受。

### 6.3 场景可组合，读取器可共享

- 同一 NetCDF：地图、曲线、热图独立启停；没有地理坐标时仍可数值分析。
- 同一 TIFF：普通图像、地理栅格、多通道显微分别匹配，内容识别不替用户强制切换场景。
- 同一 EDF：生理波形与频谱共享 reader；ESRF 图像是另一个经判型的 reader／场景。
- 同一实验：只向启用的插件开放经过授权的成员；不能因为关闭一个显示插件就让其他插件失去共享读取能力。

停用、切换和卸载必须取消加载，拒绝迟到结果，停止音频／视频，清理 AudioContext、Worker、图表与 GPU 资源。插件状态和版本／目录 revision 的前后检查应继续生效。用户偏好不能变成绕过权限的 fallback。

### 6.4 性能与安全要求

1. 首屏先给元数据＋有界样本；明确 `sampled`、单位、维度、坐标、缺失值、裁剪和重采样，不以降采样后的图冒充全量精度。
2. 缓存按所有者／文件或资源集合、源版本、插件版本、参数建立隔离键；派生 Artifact 保持私有。禁缓存串数据集、停用后复用失效授权或以主机路径作浏览器键。
3. 解析器在现有隔离机制内运行；预览只读，不执行工程宏、表达式、pickle、自带脚本、外链。资源集合只能枚举批准成员，不能任意扫描邻接目录。
4. `DATASET_HOST_PATH_ALLOWLIST`、只读 mount 和 opaque ID 不变；真实路径不进入响应、URL、localStorage 或 sessionStorage。外部 HDF5 链接、XML 实体、归档绝对路径／穿越／符号链接不得越权。
5. 全量转码、复杂谱处理、门控、重建、全量 QC 复用 AnalysisJob／Artifact Store 并显式启动；SSE 保持现有结构，预览不暗中执行分析任务。
6. 新增库按需打包，依赖与 WASM／Worker 本地提供；不默认开启在线底图、外部参考库、云端解码或遥测。部署仍使用现有唯一栈，不添加旁路预览服务。

## 7. 建议实施批次与验收方式

### 第一批：高频基础与易收益入口

- 高频 HDF5／NetCDF／TIFF 内容匹配修正，先闭环 MAT v7.3、普通 `.tif` 中的 OME／Geo 类型路由。
- A2 结构树，A3 的小型、受支持编码音视频预览。
- 启动资源集合／窗口协议设计；A1 先有限成员目录，后有限成员预览。不把 gzip 流承诺为任意位置低成本随机访问。
- GDAL 的 ASC/GRD/KML 已确认方言读取，复用现有地图／数值视图。

### 第二批：数据明确的专业试点

- 优先 B1 生理信号、B3 MCA、B2 的单场景 CZI 有界切片；先用代表样本确认格式／仪器与许可。
- B4 Origin 静态数据、A4 EPS/PS 在解释器安全与许可评估通过后补充。
- B5 ProcSpec、C2 序列／Sanger、C4 FCS 按实际用户需求选配，避免为少量格式默认增加全部重型运行时。

### 第三批：多文件与大数据

- 资源集合与范围／瓦片能力验收后，接 B6 Bruker、SCN 大切片、DICOM series、大型媒体；再按需补 IGV 索引资源和 OME-Zarr。
- C1 脉冲星样本若确认，先做少量数组只读提取的试点，验证再扩展时频／偏振视图。

每个新增场景先提供真实或合法公开的最小样本、中等样本，以及损坏／歧义／超预算／缺配套文件样本。测量冷／热首屏耗时、读取字节数、峰值内存、取消清理时间、数值正确性；本轮没有基准测试，**不预报性能提升倍数**。

回归必须包括：现有 34 插件启停、刷新保持、同一文件多视图切换；数据集／会话双方授权；版本变更与跨所有者拒绝；原 NetCDF/FITS/FASTQ/Office 功能；坏文件不拖垮其他会话。实现时执行契约生成检查、前端 type-check/build、Cordis 测试、backend/sandbox pytest、Compose 校验及实际浏览器画面验收。只通过清单或 mock 不能称为兼容成功。

## 8. 数据证据索引

以下行号为原始 `Sheet1` 的 Excel 行号，A 列标签、B 列数量、C 列示例 URL。大小写合并需要同时读取所列行；未列出的小类别仍按第 2 节明确后缀集合统计。

| 证据 | 原始行 |
| --- | --- |
| H5／HDF5 | 2、61 |
| NC／NC4 | 5、6、763 |
| TIF／TIFF | 4、40、51、243 |
| GZ／ZIP／RAR | 9、17、42、995 |
| JSON／XML／YAML／YML | 10、15、54、816 |
| MP4 | 12、350 |
| MTS／空白标签 | 862（MTS 3）／290（空白 55） |
| EPS／PS | 29、76、409 |
| EDF／BDF | 34、116 |
| CZI／SCN | 36、45 |
| MCA | 30 |
| OPJ／OPJU | 37、59、1193 |
| ProcSpec／SPC | 48、75、2917 |
| AR／ZAP／ZFTP | 19、23、24；示例均为 `https://www.scidb.cn/s/111396` |
| AB1／FASTA／GB | 86、550／72、1789／74 |
| SEGY／SGY | 104、1372／141 |

本报告未读取真实科学数据内容，除已注明的 EDF 论文关联之外，不将示例 URL 当作已完成格式验证。工具能力与许可链接紧随各条建议；未来集成时仍需按锁定版本及传递依赖复核，非商业用途也不免除署名、再分发等义务。
