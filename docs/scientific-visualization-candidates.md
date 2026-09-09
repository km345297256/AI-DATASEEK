# 科学数据与办公文档可视化插件候选清单（选型记录）

调研日期：2026-09-09。以下保留选型时的建议与范围，不作为当前可用功能说明。用户随后已批准全部 21 组并确认非商业用途；实际锁定版本、适配方式和已支持边界见[集成说明](./scientific-visualization-integration.md)。

依据：[当前可视化插件协议](./visualization-plugin-contract.md)、前端 trusted adapters、Node/Python 契约白名单及现有科学读取器。上游能力、维护状态与主许可证以各条官方 GitHub/文档链接为据；建议优先级、适配方式和复杂度属于本项目的工程判断，并非已经完成的兼容性测试。主线许可证不能替代最终锁定版本及其依赖的许可核验。

## 1. 结论

建议把用户可选项设计成“场景插件”，把 GitHub 项目作为实现依赖，而不是一个第三方仓库对应一个不可拆分的大插件。

- 同一 NetCDF：地图叠加、数值曲线、矩阵热图可独立启停，共享授权读取与有界切片。
- 同一 TIFF：普通图像、带地理坐标的栅格地图、OME 多通道显微图像应是不同插件。
- 同一 CIF：晶体材料与生物大分子的内容识别、显示方式和资源预算不同，不能只凭扩展名决定。
- 多个插件可以共享一个引擎，例如 Plotly 负责数值图表，OpenLayers 负责地理视图；关闭其中一个不应阻止另一个读取同一文件。

本清单有 **21 组主要候选**：17 组科学数据能力，以及本轮补充的 **18–21 PDF、Word、Excel、PPT 办公预览**。科学方向优先考虑 **01、02、03、05、06、09、12、14、15**；办公方向建议作为基础能力优先确认，但不自动安装。现有 3Dmol、NetCDF/FITS、文本/CSV 等能力先保留，再逐场景增强。

## 2. 适配层级

| 标记 | 意义 | 对现有协议的影响 |
| --- | --- | --- |
| S1 | 受信视图适配 | 复用现有授权文件或有界数值数据，增加懒加载适配器和独立场景描述；仍需同步 Node/Python/前端白名单及生命周期测试，不是只添加 JSON。 |
| S2 | 读取与数据模型扩展 | 新增隔离读取器、明确结果 schema、内容探测和科学元数据；层级树、多曲线、误差数组、网格等不能冒充现有 `x/y/values`。 |
| S3 | 多资源／大数据协议扩展 | 索引、参考序列、目录数据、瓦片、体素块、二进制资源需要授权引用及预算；新增版本化接口/协商，保留 v1，不直接往严格拒绝未知字段的 v1 填新字段。 |
| Job | 分析任务而非即时预览 | 全量 QC、复杂重采样/转换走现有 AnalysisJob 与 Artifact Store；不由打开文件隐式启动。 |

当前科学预览的 64 MiB 输入、512 KiB JSON 输出、512 MiB 隔离容器及 30 秒解析限制仍作为基线。文件适配器各有自己的预算，不能把上述限制混同为所有插件的统一文件大小。大数据方案应采用有界窗口/分块，不以全局放大预算替代设计。

## 3. 主要候选清单

### 通用、数学统计、物理

| 编号 | GitHub 项目／主许可证 | 适合场景与首期范围 | 建议集成方式与边界 | 建议 |
| --- | --- | --- | --- | --- |
| 01 | [Plotly.js](https://github.com/plotly/plotly.js) · MIT | 多曲线、散点、误差棒、直方图、热图、等高线；显示 CSV/表格、NetCDF/FITS 切片产生的数值。 | **S1/S2，中。**它是绘图器，不是所有文件格式的解析器。拆成数值曲线、统计分布、矩阵热图；先使用受检数据，不接受任意 Plotly 配置或远程数据 URL。按图型打包，关闭时销毁图表/GPU资源。 | 优先新增，作为共享科学图表引擎。 |
| 02 | [H5Web](https://github.com/silx-kit/h5web) · MIT | HDF5、HDF5 型 NeXus 的分组树、属性、数组切片、曲线和热图。 | **S2，中高；大文件 S3。**Vue 外壳管理 React 子树与自定义 provider；通过不透明文件 ID 访问隔离 HDF5 读取器，不直接用上游任意路径/URL接口。NetCDF4 是 HDF5 型容器，NetCDF3 不是；后者继续用现有 NetCDF reader。 | 优先选，补齐通用层级科学数据。 |
| 03 | [vtk.js](https://github.com/Kitware/vtk-js) · BSD-3-Clause | 物理仿真网格、标量场、截面与体绘制；首期限定 VTP、VTI、STL、OBJ 的已验收子集。 | **S2/S3，高。**需要 mesh/volume 的点、拓扑、标量归属、单位、包围盒协议及 GPU 预算。它只是 VTK 的子集，不承诺直接支持所有 VTK/VTU/OpenFOAM。小网格有界预览，大网格降采样或分块。 | 推荐第二批；与现有 OBJ 插件并存。 |
| 04 | [JSROOT](https://github.com/root-project/jsroot) · MIT | ROOT 文件／ROOT JSON；实验物理中的对象树、TH1/TH2 直方图、TGraph。 | **S2，中高；大表 S3/Job。**首期只允许明确对象类别与数值绘图，拒绝任意自定义函数/绘制代码、远程 THttpServer。TTree 全表分析不进入点击预览。 | 有 ROOT 数据时选，非全体用户必装。 |

### 化学、材料、结构生物学

| 编号 | GitHub 项目／主许可证 | 适合场景与首期范围 | 建议集成方式与边界 | 建议 |
| --- | --- | --- | --- | --- |
| 05 | [3Dmol.js](https://github.com/3dmol/3Dmol.js) · BSD-3-Clause | 现有 PDB/CIF/MOL/SDF/XYZ/MOL2 等分子与晶体展示；可逐项增强晶胞、表面与 CUBE 等密度视图。 | **S1/S2，中。**复用系统已有依赖，按“分子结构／晶体材料／密度等值面”拆分。新增原子数、晶胞复制、体素、表面计算预算。当前已装版本与上游支持列表不能画等号。 | 优先增强，不重复安装。 |
| 06 | [RDKit](https://github.com/rdkit/rdkit) · BSD-3-Clause | SMILES、MOL、SDF 的二维结构、结构表和子结构高亮；与 3Dmol 的三维展示互补。 | **S2，中。**优先在隔离 worker 中读取有界记录，生成有界 PNG／受检 SVG 和属性表；不是把用户 SVG/HTML 原样注入。浏览器 [RDKit.js](https://github.com/rdkit/rdkit-js) 可作为实现备选，但当前有维护交接风险。 | 推荐新增二维化学能力；优先服务端路线。 |
| 07 | [Mol*](https://github.com/molstar/molstar) · MIT | 大型蛋白、核酸和配体，PDB/mmCIF/BinaryCIF；CCP4/MRC 等密度图。 | **S2/S3，高。**只在大型结构、冷冻电镜/密度需求明确时增加；关闭自动联网取数据库、任意状态导入和 URL 加载。大密度数据需要有界体数据/切片资源协议。 | 专业补充；与基础 3Dmol 重叠场景不默认双装。 |
| 08 | [NMRium](https://github.com/cheminfo/nmrium) · MIT（该仓库） | 核磁 1D/2D 谱，先接已处理 JCAMP-DX；后续再考虑 Bruker、JEOL、Varian 和 NMRium archive。 | **S2，中高；目录/压缩包 S3；处理 Job。**React 组件需受信封装。先做只读光谱，禁默认自动处理 FID、外部导入/发布；原始实验目录要关联文件与有界解压。GitHub 代码许可与商业托管服务条款分开核验。 | 有化学光谱需求时选。 |

NMRium 的格式和自动处理行为见[官方数据加载文档](https://docs.nmrium.org/20_1d-spectra/loading_a_spectrum/)。本项目首期只读范围不等于开放其全部处理功能。

### 地学、遥感、气象、天文

| 编号 | GitHub 项目／主许可证 | 适合场景与首期范围 | 建议集成方式与边界 | 建议 |
| --- | --- | --- | --- | --- |
| 09 | [OpenLayers](https://github.com/openlayers/openlayers) ＋ [geotiff.js](https://github.com/geotiffjs/geotiff.js) · BSD-2-Clause／MIT | GeoTIFF/COG、GeoJSON、授权读取后的 Shapefile/NetCDF 栅格；投影、图层叠加、波段和色标。 | **S1/S2，中；大 COG S3。**独立于 TIFF 普通图像和 NetCDF 数值曲线。CRS/NoData/经纬度语义必须验证；无坐标数据不强行当地图。COG 需要范围读取，不整幅解码。底图优先离线数据，不默认引入外部瓦片服务。 | 地理方向首选。 |
| 10 | [MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js) ＋ [deck.gl](https://github.com/visgl/deck.gl) · BSD-3-Clause／MIT | 大量地理点、轨迹、聚合和 GPU 图层；GeoJSON/矢量瓦片，列式数据需另配读取层。 | **S2/S3，高。**偏大规模交互而非 NetCDF/GeoTIFF 通用解析；要求受控图层、同源数据服务、GPU预算及字体/样式/瓦片离线化。不要允许用户粘贴任意完整 style JSON 外联。 | 在 09 不满足大量点/轨迹需求时选。 |
| 11 | [CesiumJS](https://github.com/CesiumGS/cesium) · Apache-2.0 | 三维地球、地形、3D Tiles、glTF、CZML 轨迹等。 | **S3，高。**不是直接打开原始 NetCDF/DEM 的通用 reader；常需转换/切片产物。仅使用本机可授权资源，关闭默认外部服务，不能把 Cesium ion 当作已有必要环境。 | 明确有三维地球需求再做。 |
| 12 | [Aladin Lite](https://github.com/cds-astro/aladin-lite) · 当前主线 LGPL-3.0-or-later，选定版本另核验 | 天球坐标下的 FITS 图像、WCS、HiPS 与天文覆盖层；补齐现有 FITS 普通像素图的天文语义。 | **S2，中高；HiPS/星表集合 S3。**沿用文件授权和 FITS 校验，先本机 FITS/WCS；将星空地图与 FITS 像素/光谱视图分开。禁默认联网查询星表、巡天或上传转换；离线 HiPS 需要本机数据。许可及标识保留要求须在锁版时确认。 | 天文首选候选，先小范围验收。 |
| 13 | [MetPy](https://github.com/Unidata/MetPy) · BSD-3-Clause | 探空温湿廓线、Skew-T、风羽和风随高度变化图；从 CSV/NetCDF 映射气压、温度、露点和风分量。 | **S2，中；衍生计算 Job。**是 Python 气象计算/绘图库，不是直接嵌入的 Web 查看器。用隔离 worker 做单位校验与有界绘图，返回受控图像；明确变量映射和公式，不能把任意 CSV 自动识别成探空。 | 有气象垂直剖面需求时优先考虑。 |

Aladin 必须参考[新版官方 API](https://cds-astro.github.io/aladin-lite/)，不要沿用旧文档中“FITS 必须上传转换”的路线。其新版本具备浏览器 FITS/WCS 能力，但这不免除本系统格式、资源和离线限制。

### 生物、医学、测序质量

| 编号 | GitHub 项目／主许可证 | 适合场景与首期范围 | 建议集成方式与边界 | 建议 |
| --- | --- | --- | --- | --- |
| 14 | [IGV.js](https://github.com/igvteam/igv.js) · MIT | 基因组轨道；BED/GFF/GTF、VCF、bigWig/bigBed；BAM＋BAI、CRAM＋CRAI＋参考序列。 | **S2/S3，高。**将主文件、索引、参考基因组绑定为授权资源集合；按区间读取、逐请求鉴权，明确参考版本。禁默认下载远程 hg38/轨道/基因搜索，不顺带开启外部通信功能。 | 生命科学优先，前提是先完成关联文件协议。 |
| 15 | [Viv](https://github.com/hms-dbmi/viv) · MIT | OME-TIFF、Indexed OME-TIFF、OME-NGFF/Zarr；多通道叠加、Z/T 切换和多尺度显微图像。 | **S2/S3，高。**与普通 TIFF 图像插件分开；需要 OME 元数据、轴/单位、tile/chunk 和目录集合鉴权。不能把大图整文件送浏览器，也不能信任元数据里的外链。共享 deck.gl/luma.gl 依赖时统一锁版。 | 生命科学优先，尤其适合显微图像数据集。 |
| 16 | [NiiVue 新仓库](https://github.com/niivue/mono) · BSD-2-Clause（核心） | NIfTI/NRRD 科研影像，正交切片、体渲染与分割叠加；其他格式逐项验收。 | **S2/S3，高。**上游正从[旧仓库](https://github.com/niivue/niivue)迁移；先试点并测试 affine、左右方向、qform/sform、体素内存和覆盖图配准。DICOM 是额外转换扩展及多文件流程，不属于本轮核心默认支持。 | 医学/神经影像试点，非临床诊断工具。 |
| 17 | [FastQC](https://github.com/s-andrews/FastQC) ＋ [MultiQC](https://github.com/MultiQC/MultiQC) · GPL-3.0-or-later | FASTQ/压缩 FASTQ 等完整质量控制，以及多个样本 QC 结果汇总。 | **Job，中高。**FastQC 计算质量指标，MultiQC 汇总其日志/结果，不直接解析 FASTQ 生成全部报告。保留现在 FASTQ 前缀预览，完整 QC 显式启动 AnalysisJob；图表读受检结果，HTML 报告若保留需严格隔离。 | 有完整批量 QC 需求再选；不阻塞文件预览。 |

IGV 的索引与参考要求见[官方格式清单](https://igvteam.github.io/igv-webapp/fileFormats.html)。FastQC/MultiQC 的分析权限与可视化 `file:read` 不能混为一谈；启用报告查看不应暗中授予分析执行或写原始文件的权限。

### 办公文档：PDF、Word、Excel、PPT（本轮新增候选）

本轮仅增加选型清单，不扩展到在线编辑、协同办公或对源文件保存。办公文件分为“版式阅读”和“数据浏览”，同一文件仍可以拥有独立启停的不同视图。

| 编号 | 候选实现／主许可证 | 拟支持范围与能力 | Cordis 适配及边界 | 建议 |
| --- | --- | --- | --- | --- |
| 18 | [PDF.js](https://github.com/mozilla/pdf.js) · Apache-2.0 | PDF 分页、缩放、缩略图、目录、文本选择与搜索。扫描 PDF 可看页面，搜索取决于是否有文字层。 | **S1/S2，中。**受信 `document` 适配器，PDF/Worker/字体/CMap 本地打包，授权文件读取及页面渲染预算。禁 PDF 内脚本、表单提交、自动外链及嵌入文件执行；关闭时取消渲染并销毁 Worker。OCR 是另行选择的分析能力，不默默启动。 | 办公基础首选，也作为共享页面渲染依赖。 |
| 19 | [docx-preview / docxjs](https://github.com/VolodymyrBaydalka/docxjs) · Apache-2.0；版式备选 [LibreOffice Writer](https://github.com/LibreOffice/core) | DOCX 快速阅读正文、表格、图片、页眉页脚；DOC 旧格式或分页要求更高的文件走隔离转换为 PDF。 | **S1/S2，中；转换需有界调度。**可拆为 `word-reading` 与 `word-pages` 两个场景。docx-preview 不解析旧 DOC，也不保证 Word 完全同版；只使用稳定公开接口，禁 altChunk HTML、外部关系/字体加载及不安全链接，隔离文档样式。转换结果保留原文件不变。 | 推荐；若首先重视固定版式，可先只做转换路线。 |
| 20 | 现有 openpyxl/xlrd ＋ 受控分页表格；前端读取备选 [SheetJS CE](https://github.com/SheetJS/sheetjs) · Apache-2.0 | XLSX/XLS 工作表切换、单元格值、行列分页、合并区域信息、公式文本/已有缓存值；选列后可交给 01 作图。 | **S2，中。**优先复用已有隔离 Python 读取栈，新增工作簿元数据与有界单元格窗口。SheetJS 是解析器，不是完整 Excel UI；不承诺原图表、复杂格式或完整公式计算。禁宏、DDE、外部链接和自动重算；没有公式缓存时明确标识缺失，不显示为 0。 | 推荐先做数据浏览；打印版式视图可后续独立添加。 |
| 21 | [LibreOffice Impress](https://github.com/LibreOffice/core) ＋ [PDF.js](https://github.com/mozilla/pdf.js) | PPT/PPTX 静态幻灯片分页、缩略图、缩放及只读全屏浏览。 | **S2，中。**在无网络隔离容器中转 PDF，再用受信页面渲染器展示。不承诺动画、转场、音视频或可编辑形状；复杂版式、字体、SmartArt 需样本验收。默认不导出隐藏幻灯片/备注，不能擅自展开嵌入对象。 | 推荐先做静态预览，避免引入整套在线 Office 服务。 |

LibreOffice 的许可以 [MPL-2.0 及相关组件/贡献许可说明](https://www.libreoffice.org/licenses/)为准，不能把完整发行包简单标为 MIT；其 GitHub 是官方只读镜像，不代表项目停止维护。本机转换能力依据[官方命令行说明](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)及[PDF 导出参数](https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html)，版式与性能仍需本项目实际样本验证。

#### 现有工程可以复用什么

- `sandbox/Dockerfile` 已声明 `libreoffice-writer` 和 `libreoffice-impress`，可作为 19/21 的转换依赖基础；这是构建声明，不是已完成办公预览接入或本轮运行验证。
- 当前未在该文件显式安装 `libreoffice-calc`。如果需要“Excel 打印版式 → PDF”，应另行核验/补齐 Calc 与字体，不能假定已经具备。
- 沙箱已有 openpyxl/xlrd/pandas 依赖；20 优先复用它们，避免仅为 XLSX 数据表又增加一套浏览器解析栈。
- 当前 trusted adapters 没有 PDF/Word/Excel/PPT 项；四类能力都必须新增插件声明、适配代码、鉴权/限额及测试后才能开放。

#### 办公预览的专门规范

1. **开关按场景独立。** PDF、Word 阅读、Word 页面、Excel 数据表、PPT 页面分别有插件 ID；PDF.js/LibreOffice 是可共享实现依赖。关闭“PDF 文件预览”不应自动关闭 PPT 页面渲染；PPT 的派生 PDF 仍受 PPT 场景授权约束，不能借通用 PDF 入口绕过禁用状态。
2. **转换不是简单执行命令。** 非 root、无网络、只读输入、有界临时空间、独立用户配置目录，固定参数与文件 ID 输入；禁止宏、外部数据更新和动态链接，超时/取消回收整个进程组。`--headless` 或 `--safe-mode` 本身不是安全沙箱。
3. **结果与原文件分离。** 派生 PDF/缩略图经校验后使用授权产物引用，不返回临时宿主路径、不覆盖源文件、不进入模型消息。缓存按用户、源文件版本、转换器版本、字体包及参数区分，原权限或插件状态失效后不可继续使用。
4. **明确读取预算。** 控制压缩包成员/解压后大小、PDF 页面像素/并发页数、DOCX DOM/图片数量、Excel 工作表/单元格数量、PPT 页数及转换输出大小；超预算明确拒绝或由用户显式选择异步任务，不绕过当前短预览边界。
5. **数据不失真。** Excel 区分文本、数值、日期系统、空值、错误值、公式文本与缓存结果；不执行宏、不更新外部数据。PDF 搜索不是 OCR；Word/PPT 字体替代或格式差异需要提示，不承诺与 Microsoft Office 像素级一致。
6. **离线与回归。** 不使用 Google Docs/Microsoft 在线预览地址，不要求公网可访问文件。测试中文字体、公式/表格/图表、分页、加密/损坏文件、压缩炸弹、外链、停用、取消与旧功能回归；加密文件不自动破解，密码不得记录到 URL 或日志。

#### 办公方向备选，不作为默认依赖

- [Mammoth](https://github.com/mwilliamson/mammoth.js) · BSD-2-Clause：适合 DOCX 的语义阅读/正文提取，不适合保留原分页版式；官方明确不净化输出，因此需清洗 HTML/链接，并保持外部文件访问关闭。若选用，可作为 `word-reading` 的另一种实现，而不是额外安装后没有独立价值的插件。
- [Univer](https://github.com/dream-num/univer)：开源核心为 Apache-2.0，可用于更完整的电子表格界面，但不是只读文件解析器。官方[Office 文件交换方案](https://docs.univer.ai/guides/server-integration/import-export)涉及 Pro 模块及服务端转换；不能把开源核心等同于免费完整 XLSX/DOCX/PPTX 兼容。当前先不引入在线编辑/协同需求。
- SheetJS 的[GitHub 公告](https://github.com/SheetJS/sheetjs)明确主源码已迁移，若选择前端解析应从当前官方渠道锁定并验证版本，不照旧 GitHub/npm 示例安装。其[公式文档](https://docs.sheetjs.com/docs/csf/features/formulae/)明确不自动计算结果；不要当作 Excel 计算引擎。

## 4. 需要复用或补充的读取层，不额外包装成空壳查看器

| 读取基础 | 服务的格式／角色 | 本项目建议 |
| --- | --- | --- |
| 现有 pandas/openpyxl/xlrd；[Apache Arrow](https://github.com/apache/arrow) 按需补充 | CSV/TSV、XLSX/XLS 工作表、Parquet/Arrow 表数据 | 共用分页表格和 Plotly。表格库依赖已在沙箱声明，Parquet reader 需单独确认/适配；Excel 宏、外部链接和公式不执行，不能宣传“Plotly 自动读 Excel/Parquet”。 |
| 现有 NumPy/SciPy | NPY/NPZ 数组、MATLAB MAT 数值变量 | 补充矩阵/曲线 reader，复用 01/02 的视图。强制禁用 pickle、限制头部和解压量；MAT v7.3 需走 HDF5 分支，不交给 SciPy `loadmat`。复杂对象和稀疏矩阵另定边界。依据：[NumPy 读取说明](https://numpy.org/doc/stable/reference/generated/numpy.load.html)、[SciPy MAT 读取说明](https://docs.scipy.org/doc/scipy/reference/generated/scipy.io.loadmat.html)。 |
| [xarray](https://github.com/pydata/xarray)、[h5py](https://github.com/h5py/h5py)、现有 netCDF4/Zarr | 带维度数组、HDF5 层级、NetCDF 和 Zarr 切片 | 优先复用现有隔离环境，分别给曲线、地图、H5Web 供数。Zarr 的目录/分块支持仍需资源集合协议；HDF5 外链/VDS 等越界依赖不能默认跟随。 |
| [Astropy](https://github.com/astropy/astropy) | FITS/HDU、坐标及 WCS 数据处理 | 继续作为天文读取基础，先增强现有 FITS 像素/曲线能力，再给 Aladin 提供受检资源。它是科学库，不是独立 WebUI 插件。 |
| [h5wasm](https://github.com/usnistgov/h5wasm) | 浏览器 HDF5 读取备选 | 不是默认必装；当前服务端隔离路线更易统一预算。其[许可](https://github.com/usnistgov/h5wasm/blob/main/LICENSE.txt)为 NIST 自定义条款及捆绑 HDF5 条款，不应误标 MIT。 |

“装有某个 Python 库”不等于“已经开放对应预览格式”。只有实现授权 reader、输出 schema、限额与回归后，才应登记为可用能力。

## 5. 备选与暂缓项

| 项目 | 结论 |
| --- | --- |
| [Vega-Lite](https://github.com/vega/vega-lite) · BSD-3-Clause | 若希望重点做声明式、分面和刷选联动，可作为 01 Plotly 的替代。先选一个基础图表体系；禁止任意 `data.url`、链接与无限制表达式/transform，不能把 Vega 规范原样当本系统安全插件协议。 |
| [JS9](https://github.com/ericmandel/js9) | 不建议新增。官方仓库于 2024-12-31 归档，作者明确提醒已经没有维护者；不能因历史上常用于 FITS 就作为新系统长期依赖。 |
| [CARTA](https://github.com/CARTAvis/carta-frontend)／[后端](https://github.com/CARTAvis/carta-backend) · GPL-3.0 | 可留作专业天文大数据工作站远期方案；是前后端应用，不是轻量嵌入组件。需要额外运行时、数据协议、鉴权/资源审查，不能以 iframe 或 manifest 绕开主机边界。 |
| [silx](https://github.com/silx-kit/silx) · MIT | 科学实验/HDF5/SPEC 很有价值，但其主查看器是 Qt 桌面界面。优先借鉴读取层或使用 H5Web，不将整套桌面应用塞入当前 Vue 文件面板。 |

## 6. 维护与兼容性核实中发现的重点

- Plotly 官方[版本记录](https://github.com/plotly/plotly.js/blob/master/CITATION.cff)列有稳定 3.7.0（2026-07-03）；不因为存在新的 RC 就直接选 RC。vtk.js 的[发布页](https://github.com/Kitware/vtk-js/releases)可见 2026-09-04 的 v36.11.2。最终仍须锁定实际验收版本。
- H5Web [17 版发布记录](https://github.com/silx-kit/h5web/releases/tag/v17.0.0)有 provider/schema 变更及 ESM-only 变化；需要适配，不照抄旧版示例。
- [RDKit.js 公告](https://github.com/rdkit/rdkit-js)记录 2026-04-07 起 npm/仓库维护交接。推荐 RDKit 能力不等于推荐无条件使用最新 npm 包。
- [NiiVue 旧仓库公告](https://github.com/niivue/niivue)指向 mono，后者有 v1 RC 路线及额外 dcm2niix 扩展；选定版本必须验证，不照旧仓库支持列表承诺新版已全部实现。
- Aladin 当前主线声明 LGPL-3.0-or-later，不能由此推定所有旧发行版许可相同。FastQC/MultiQC、CARTA 等也需核对锁定版本和依赖；容器隔离本身不替代许可证核验。
- NMRium、H5Web 引入 React；Viv 引入 deck.gl/luma.gl；现有系统已有 Three.js/3Dmol。这些属于需要测量的依赖和 GPU 开销，不宜让所有查看器进入首页主包。

本轮没有安装构建或对上游跑本机基准，故未给出未经测量的包大小、加载秒数和“性能提升百分比”。

## 7. 确认后采用的统一适配原则

1. **授权仍在宿主。** 主文件、索引、参考序列、瓦片和压缩包成员都通过受控引用访问；归属、启停、版本和预算逐请求验证。浏览器和上游库不得得到宿主真实路径。
2. **能力先探测再展示。** 扩展名仅作候选匹配；检验文件内容及元数据，确定能否地图化、是否 OME、是否目标 HDU/变量。无法识别时明确“不适用”，不伪造科学语义。
3. **独立启停。** 用户开关控制场景使用及当前实例卸载；不销毁影响他人的共享引擎。停止应取消请求、释放 Worker/GPU/Blob 资源，迟到结果不能重新挂载。
4. **科学语义不丢。** 保存维度、单位、dtype、缺失值、CRS/WCS、坐标/参考版本及抽样信息；不隐式做空间平均、自动拟合、自动相位校正。计算型操作单独授权并记录。
5. **真正按需读取。** 曲线/网格/图像都有明确采样或分辨率；有界 slice/tile/chunk，并限制解压后字节数、点数、原子数、体素数、并发与缓存。缓存以用户/文件版本/插件版本/参数隔离。
6. **运行时默认离线。** JS/WASM/字体/样式本机打包，禁止继承上游示例中的云端上传、CDN、远程目录和自动下载参考数据。当前 Shapefile 代码含 OSM 外部瓦片入口，离线 GIS 改造时也应明确处理，而非假设现有地图已经完全离线。
7. **协议兼容。** 保留当前 v1 和 14 个现有插件；新资源模型通过明确版本与能力协商引入。扩展数据读取端点不改变 AgentLoop/PlanActFlow/SSE，禁止借可视化引入任意代码执行。
8. **逐插件验收。** 每项准备公开或合成样本，测试正确值/坐标、无效文件、权限/关联资源越界、停用/取消、版本变化、离线运行、内存/首屏指标和旧预览回归。许可、版本与样本来源形成依赖清单。

## 8. 建议确认方式与实施顺序

先选场景再定范围，以下只是建议，不是自动执行计划：

- **办公基础：18 PDF、19 Word、20 Excel、21 PPT。** 建议与基础预览增强一起优先确认；实现上先做 PDF/Excel，再复用 PDF 渲染依赖完成 Word/PPT。本轮仍仅更新候选清单。
- **A：先增强现有使用体验：01 Plotly、05 3Dmol 增强、06 RDKit 二维、09 OpenLayers＋GeoTIFF。** 同时复用现有 NetCDF/FITS 读取层；避免重复引入同类引擎。
- **B：覆盖专业科学数据：02 H5Web、03 vtk.js、12 Aladin Lite、14 IGV.js、15 Viv。** 按所选项目先落实对应 S2/S3 数据协议，再做可视化插件。
- **C：由实际数据决定：04 JSROOT、07 Mol*、08 NMRium、10 MapLibre＋deck.gl、11 Cesium、13 MetPy、16 NiiVue、17 FastQC＋MultiQC。** 这些不是“不好”，而是专业深度、依赖或数据面成本更高，不应无需求默认启用。

可直接回复编号，例如：“选择 01、02、03、05、06、09、12、14、15；暂不接入其余项”。也可指定某项的首期格式，例如“14 先支持 BED/VCF，BAM/CRAM 后续再做”。确认选型后再提交具体协议差异、插件 ID、数据样例与验收范围。
