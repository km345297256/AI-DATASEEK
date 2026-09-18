# 逐插件可视化测试数据与验收指南

验收快照：2026-09-18；本机 http://localhost:7001。范围是数据集文件预览，不是重新测试 AgentLoop 或所有分析算法。

## 交付结果

- 当前 83 个具体可视化插件都有明确的测试数据集、入口文件、操作步骤、预期结果与 SHA-256。
- 本次新增 52 套 / 194 个文件，共 2,364,883,977 字节（约 2.36 GB / 2.20 GiB）：50 套正向数据，另保留 2 套已标记的边界测试。
- 正向对应表使用 64 套数据：50 套新增 + 14 套已有。兼容插件共享同一套真实数据，避免重复下载同一来源；不是声称新增了 83 个互不相同的数据集。
- 实体文件存放在用户指定的 `/Users/luchangfa/Documents/Codex/Data/open-catalog/plugin-tests/`。注册后总计 113 个数据集；原有 61 个的公开元数据指纹不变，83 个插件启停设置不变。
- 82 个原本启用的插件完成下表所述基础页面预览/交互；VTK 原本关闭，保持关闭，只有离线解析证据。CZI-window 小窗口通过、大窗口有已知性能问题。**入口覆盖不等于全部功能、全部格式和任意大小都保证正常。**
- 恢复后的最终API验收：83项中82通过、1项原有禁用、0项失败、0项未运行；包含对同一个已完成FastQC任务的只读结果核验，没有重复启动任务。

## 如何使用

1. 打开[数据集管理](http://localhost:7001/datasets)，搜索 **插件测试样例** 可找到本次新增数据；也可直接点击下面每行的数据集链接。
2. 进入探查页，点击表中指定的入口文件右侧预览按钮。在右侧“可视化”选择器中选择准确插件，不要只依赖默认格式匹配。
3. 有“读取窗口”“读取分页”“应用切片”等按钮时，需要明确选择参数后点击。下表写的是本轮测试路径；原目录 `SOURCE.md` 保留首次准备说明，发生兼容性调整时以下表为准。
4. `.shp/.shx/.dbf/.prj/.cpg`、ENVI、Ripple、OME-Zarr 等配套文件应保持完整，不能只移动入口文件。DBF 正向样例请打开 `countries.dbf`，再切换到 DBF 只读数据表。
5. FastQC 完整质控由“启动质控”创建 AnalysisJob，不是打开文件就执行。本轮只在 3 条读段的官方样例上运行过一次并成功；WARN/FAIL 质量模块不等于程序失败，也不能拿这个极小样例推断实验质量。

## 样本性质

- **科学原件**：公开发布的原始下载文件或完整研究附件，保留原字节。
- **科学派生**：由保留的开放原件转换、选列、截取完整记录或无损重排；明确记录过程，不补造科学数值。
- **官方样例**：上游软件维护者提供的格式/算法测试资料，不一概称为真实观测。
- **项目夹具**：本项目已有或明确生成的合成数据，只验证软件功能，不做科学/医学推断。

每份新增数据都有 `SOURCE.md`、完整文件清单、大小、SHA-256、来源、作者、许可、转换说明与样本性质；SHA-256 是下载快照完整性基线，不是发布机构签名。CSV/Excel及派生表保留缺失值、原数字精度和单位，未为方便演示而填补缺失值。完整许可见对应 manifest / descriptor，不将官方测试样本统一改标为“开放观测数据”。

## 83 个插件的测试入口

“大小”是入口文件大小，不是整套数据占用。各行预期只适用于本样例。除 VTK 和特别注明的 CZI-window 外，本轮基础页面路径已成功显示；详细实际动作见验收 JSON，不将手动建议操作全部算作自动测试已覆盖。

### 图像（21）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| RDKit 二维化学结构<br>`viz-rdkit` | [插件测试·RDKit 二维顺式结构官方样本](http://localhost:7001/dataset/seek/viz-plugin-rdkit-cis)<br>`cis.sdf`<br>[来源](https://github.com/rdkit/rdkit/blob/20331b5101183089580840759c3ddf11a73efe31/rdkit/Chem/test_data/cis.sdf) | 565 B<br>官方样例 | 打开 cis.sdf，选择 viz-rdkit。 检查结构或数值与说明一致，不启用外部网络请求。 预期：显示1个2D分子，4原子、3键，分子式C2H2BrF，分子量124.94。 |
| NeXus 科学坐标与误差<br>`viz-nexus-window` | [插件测试·RosettaSciIO NeXus 二维信号切片](http://localhost:7001/dataset/seek/viz-plugin-nexus-rosettasciio)<br>`nexus_fixed.nxs`<br>[来源](https://github.com/hyperspy/rosettasciio/blob/bc254db14cd7d4d23169b11aeb622a0a7eac1fbe/rsciio/tests/data/nexus/nexus_dls_example.nxs) | 5.2 KB<br>官方样例 | 打开 nexus_fixed.nxs，选择 viz-nexus-window。 选择 data 信号，设置两个轴 0..10、step=1，检查 10×10 二维数据。 预期：NXdata原始信号10×10，x/y坐标完整；读取显式窗口，不做缩放或缺值推断。 |
| 电子显微 Ripple 谱像<br>`viz-ripple-window` | [插件测试·RosettaSciIO Ripple 谱像官方样本](http://localhost:7001/dataset/seek/viz-plugin-ripple-rosettasciio)<br>`ripple_little_endian.rpl`<br>[来源](https://github.com/hyperspy/rosettasciio/blob/bc254db14cd7d4d23169b11aeb622a0a7eac1fbe/rsciio/tests/data/ripple/test_ripple_sdim-1_ndim-2_float32.rpl) | 398 B<br>官方样例 | 打开 ripple_little_endian.rpl，选择 viz-ripple-window。 选择通道 2 图像和像素 (1,1) 的 4 点谱线，核对原值。 预期：2×3×4原始float32数据；显示6像素图及4道谱，使用文件声明的轴标定。 |
| ESRF EDF / SPE 2.x 探测器图像<br>`viz-instrument-images` | [插件测试·ESRF EDF 合成三帧标定网格](http://localhost:7001/dataset/seek/viz-plugin-instrument-project)<br>`calibration-grid.edf`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 20.0 KB<br>项目夹具 | 打开 calibration-grid.edf，选择 viz-instrument-images。 选择第 2 帧、ROI [8,8,32,24]，检查范围 1520..3023。 预期：合成ESRF-EDF三帧64×48；frame=1的32×24 ROI可见，uint16原始值不变。 |
| 图片<br>`image` | [真实犬细胞核图像：七种影像格式与阅读器对照](http://localhost:7001/dataset/seek/viz-bio-lightmycells-formats)<br>`nucleus-uint16.png`<br>[来源](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047) | 1.92 MB<br>科学派生 | 打开PNG，确认细胞核影像可见；16位PNG仅作显示，不以显示亮度代替原始强度。 预期：1200×1200 PNG显微细胞核影像可见；显示强度不替代uint16测量值。 |
| TIFF 栅格<br>`tiff` | [真实犬细胞核图像：普通TIFF像素无损兼容样例](http://localhost:7001/dataset/seek/viz-bio-lightmycells-classic-tiff)<br>`nucleus-classic-uint16.tiff`<br>[来源](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047) | 2.88 MB<br>科学派生 | 选择TIFF阅读器，确认普通TIFF-6兼容副本1200×1200可解码；逐像素与随附原始OME-BigTIFF相同。 预期：普通TIFF-6绘制为1200×1200灰度画布，像素与随附OME原件完全相同。 限制：原始OME文件为BigTIFF；当前旧TIFF插件无法处理它，应使用此普通TIFF副本或选择Viv读原件。 |
| Viv 多通道显微图像<br>`viz-viv` | [真实犬细胞核图像：七种影像格式与阅读器对照](http://localhost:7001/dataset/seek/viz-bio-lightmycells-formats)<br>`image_399_Nucleus.ome.tiff`<br>[来源](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047) | 2.24 MB<br>科学原件 | 选择Viv，显示单通道原始OME-TIFF，Z=0、T=0，点击应用切片。 预期：原始1200×1200 OME-BigTIFF的单Nucleus通道可见；Z=0、T=0，应用切片后正常显示。 |
| CZI 显微切片<br>`viz-czi` | [真实犬细胞核图像：七种影像格式与阅读器对照](http://localhost:7001/dataset/seek/viz-bio-lightmycells-formats)<br>`nucleus-uncompressed.czi`<br>[来源](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047) | 2.88 MB<br>科学派生 | 打开CZI，选择C/Z/T均0和128×128 ROI；比较原图。 预期：显示128×128原始ROI对应的灰度PNG；C/Z/T均0，不凭空补齐或重采样。 |
| 未压缩 CZI 大文件区域<br>`viz-czi-window` | [真实犬细胞核图像：七种影像格式与阅读器对照](http://localhost:7001/dataset/seek/viz-bio-lightmycells-formats)<br>`nucleus-uncompressed.czi`<br>[来源](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047) | 2.88 MB<br>科学派生 | 选择CZI区域阅读器，C/Z/T均0，明确将宽高改为16，读取[0,0,16,16]。 应约8秒显示16×16灰度像素。128×128当前真实请求出现503超时，不能视为大窗口已通过。 预期：目录显示1200×1200、单C/Z/T；16×16 ROI实际灰度PNG可见，实测约7.9秒。 限制：128×128 ROI在真实浏览器范围读取中长时间等待后返回503；仅16×16已通过端到端验证，较大窗口性能仍需优化。；whole CZI读取器可处理本小样例；window通过不等于任意8GiB文件或所有窗口均已验收。 |
| OME-Zarr 本地分块图像<br>`viz-ome-zarr` | [真实犬细胞核图像：七种影像格式与阅读器对照](http://localhost:7001/dataset/seek/viz-bio-lightmycells-formats)<br>`nucleus.zarr/.zattrs`<br>[来源](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047) | 488 B<br>科学派生 | 打开同目录.zattrs，level=0、indices=[]、ROI=[0,0,128,128]，保持全部目录文件。 预期：单层级YX NGFF读取并绘制128×128区域；同目录4个Zarr对象必须完整保留。 |
| 高维矩阵工作台<br>`viz-matrix-workbench` | [真实犬细胞核图像：七种影像格式与阅读器对照](http://localhost:7001/dataset/seek/viz-bio-lightmycells-formats)<br>`nucleus-uint16.npy`<br>[来源](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047) | 2.88 MB<br>科学派生 | 选择矩阵工作台，确认二维uint16矩阵与原始像素相同；不启用pickle。 预期：二维uint16矩阵热力图非空，可切换有界数值表格再返回热力图；原数组全部1200×1200值保留。 |
| DICOM 灰度影像<br>`viz-dicom-window` | [原创DICOM数值体模（非临床）](http://localhost:7001/dataset/seek/viz-bio-dicom-phantom)<br>`synthetic-phantom.dcm`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 8.7 KB<br>项目夹具 | 打开DICOM元数据，确认单帧64×64。 仅对本明确合成文件勾选已去标识确认，再选择frame=0与ROI=[0,0,64,64]；不是诊断影像。 预期：本原创非患者体模显示64×64灰度区域；将窗位改为750时屏幕像素改变而源值不变。 限制：confirm_deidentified=true仅批准本明确合成体模，不可复用于未经脱敏审查的其他DICOM。 |
| NiiVue 医学体数据<br>`viz-niivue` | [原创三维球体体模：NiiVue正交切片](http://localhost:7001/dataset/seek/viz-bio-niivue-phantom)<br>`synthetic-radial-volume.nrrd`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 221.3 KB<br>项目夹具 | 打开内嵌raw NRRD，选择NiiVue。 切换正交切片与体渲染；中心强度高、外围为0，确认轴向一致；不要作医学解释。 预期：48³原创径向体模的正交切片与3D球体可见；切换影像布局正常，非临床影像。 |
| MetPy 大气探空图<br>`viz-metpy` | [插件样例｜MetPy 官方 May 4 探空剖面](http://localhost:7001/dataset/seek/viz-metpy-may4-sounding)<br>`may4_sounding.csv`<br>[来源](https://github.com/Unidata/MetPy/blob/07df928b0d47fce73696116d6988d0722bfaa57e/staticdata/may4_sounding.txt) | 551 B<br>官方样例 | 打开 may4_sounding.csv，选择 viz-metpy。 压力选择 pressure_hPa（hPa）；温度 temperature_degC 和露点 dewpoint_degC 均选 degC，点击绘制探空图。 应显示红色温度、绿色露点曲线，共 30 层；不应显示 CAPE/CIN 诊断。 预期：应显示红色温度、绿色露点曲线，共 30 层；不应显示 CAPE/CIN 诊断。 |
| ENVI 波段与像元光谱<br>`viz-envi-window` | [插件样例｜NOAA 月气温气候态 ENVI 栅格](http://localhost:7001/dataset/seek/viz-noaa-climatology-envi)<br>`monthly_air.hdr`<br>[来源](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html) | 131 B<br>科学派生 | 打开 monthly_air.hdr，选择 viz-envi-window（同目录 monthly_air.img 自动配对）。 图像选 band 0、x=0、y=0、width=128、height=73，读取窗口。 应显示 73×128 个一月气温原值（°C）；可选 x=72、y=36、band_start=0、band_count=12 查看月气候态序列，横轴 band 索引不是波长。 预期：应显示 73×128 个一月气温原值（°C）；可选 x=72、y=36、band_start=0、band_count=12 查看月气候态序列，横轴 band 索引不是波长。 |
| GRIB 气象场<br>`viz-grib-window` | [插件样例｜NOAA GFS 东亚 2 米气温 GRIB2](http://localhost:7001/dataset/seek/viz-noaa-gfs-grib)<br>`gfs-20260916-00-2mt-wmo.grib2`<br>[来源](https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_1p00.pl) | 300 B<br>科学派生 | 打开 gfs-20260916-00-2mt-wmo.grib2，选择 viz-grib-window 并读取目录。 选择第一条 2t 消息，ROI x=0、y=0、width=11、height=11。 应为 121 个 2 m 气温值，K，不应误标 °C；经度 100–110、纬度 30–40，时效 0。 预期：应为 121 个 2 m 气温值，K，不应误标 °C；经度 100–110、纬度 30–40，时效 0。 |
| HDF5 / NetCDF4 分块变量<br>`viz-array-window` | [插件样例｜NOAA 四年全球气压层气温与 GB 数组窗口](http://localhost:7001/dataset/seek/viz-noaa-pressure-air-2022-2025)<br>`air.2025.contiguous.h5`<br>[来源](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html) | 1.044 GB<br>科学派生 | 打开 air.2025.contiguous.h5，选择 viz-array-window，读取目录。 变量选 air；前两维固定 0（首时刻、首气压层），后两维分别切片 start=0 stop=73 step=1 与 start=0 stop=144 step=1。 读取图像窗口应显示 73×144=10512 值（K），与原数组完全一致；实测只读 46144B、2 次范围请求，而非下载完整 1.04GB；不选其它三个年度原 NC 冒充兼容。 预期：读取图像窗口应显示 73×144=10512 值（K），与原数组完全一致；实测只读 46144B、2 次范围请求，而非下载完整 1.04GB；不选其它三个年度原 NC 冒充兼容。 |
| ODIM 天气雷达窗口<br>`viz-radar-window` | [插件测试样例｜ODIM 2.4 合成雷达双仰角](http://localhost:7001/dataset/seek/viz-dataseek-odim24-fixture)<br>`synthetic-odim24.h5`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 32.2 KB<br>项目夹具 | 打开 synthetic-odim24.h5，选择 viz-radar-window。 选 sweep=1、quantity=DBZH，ray_start=1、ray_count=2、gate_start=1、gate_count=4，decode=raw。 窗口有 8 个原值、恰好 1 个 nodata 与 1 个 undetect；检查二者不同样式，不能解读为实际天气。 预期：窗口有 8 个原值、恰好 1 个 nodata 与 1 个 undetect；检查二者不同样式，不能解读为实际天气。 |
| FITS 图像<br>`fits-image` | [NASA 哈勃 WFPC2 天文成像样本](http://localhost:7001/dataset/seek/nasa-hst-wfpc2)<br>`WFPC2ASSNu5780205bx.fits`<br>[来源](https://fits.gsfc.nasa.gov/fits_samples.html) | 63.4 KB<br>官方样例 | 打开 WFPC2ASSNu5780205bx.fits，选择 fits-image。 选择 HDU 0，显示 100×100 原始像素；此插件不应用天空坐标方向。 预期：100×100 像素图像、10000 数值；像素坐标，不应声称 WCS 投影。 |
| FITS / TIFF 科学图像工作台<br>`viz-astronomy-workbench` | [NASA 哈勃 WFPC2 天文成像样本](http://localhost:7001/dataset/seek/nasa-hst-wfpc2)<br>`WFPC2ASSNu5780205bx.fits`<br>[来源](https://fits.gsfc.nasa.gov/fits_samples.html) | 63.4 KB<br>官方样例 | 打开 WFPC2ASSNu5780205bx.fits，选择 viz-astronomy-workbench。 选择 dataset 0、band 1、asinh、zscale、gray，点击渲染；可检查像素或区域统计。 预期：100×100 PNG、10000 有效像素、WCS四角存在；显示拉伸不改原值。 |
| H5Web 层级与切片<br>`viz-h5web` | [NOAA 近地面气温月气候态](http://localhost:7001/dataset/seek/open-noaa-air-climatology)<br>`air.sig995.mon.ltm.1991-2020.nc`<br>[来源](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html) | 657.4 KB<br>科学原件 | 打开 air.sig995.mon.ltm.1991-2020.nc，选择 viz-h5web。 选 /air，前导维度索引 0，切换二维热图。 预期：原12×73×144中的一月平面；H5Web默认坐标为索引，并做有界间隔显示，单位degC。 |

### 地图（11）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| 空间组学观测与表达<br>`viz-spatial-window` | [原创AnnData空间网格（非转录组观测）](http://localhost:7001/dataset/seek/viz-bio-spatial-phantom)<br>`synthetic-spatial.h5ad`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 30.4 KB<br>项目夹具 | 选择空间窗口，feature=2、observation_start=0、observation_count=256、decode=raw。 核对右上方向梯度以及每个点value=x+y；没有基因表达生物学含义。 预期：16×16网格全部256点可见，第三特征等于x+y，颜色梯度反映原创数值而非真实基因表达。 |
| SAM / BAM / CRAM 比对工作台<br>`viz-alignment-browser` | [原创基因组阅读器功能样例：SAM/BED/BLAST](http://localhost:7001/dataset/seek/viz-bio-alignments-and-tracks)<br>`synthetic-alignments.sam`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 479 B<br>项目夹具 | SAM选择reference=0,start=100,end=180，观察匹配、插入、缺失与成对关系。 预期：显示5条原创SAM read轨道及覆盖度，可点击read查看匹配/插入/缺失等详情。 |
| 基因组注释轨道<br>`viz-genome-tracks` | [原创基因组阅读器功能样例：SAM/BED/BLAST](http://localhost:7001/dataset/seek/viz-bio-alignments-and-tracks)<br>`synthetic-features.bed`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 99 B<br>项目夹具 | 染色体0，start=0，end=500；检查三条BED区间。 预期：chr_demo的三个BED区间可见；UI起点1对应API起点0，可选区间打开详情。 |
| ASC/GRD/KML 地理数据<br>`viz-geoformats` | [插件样例｜USGS 2024 强震 KML 点图层](http://localhost:7001/dataset/seek/viz-usgs-earthquakes-kml)<br>`usgs-2024-m6.kml`<br>[来源](https://earthquake.usgs.gov/fdsnws/event/1/) | 10.4 KB<br>科学派生 | 打开 usgs-2024-m6.kml，选择 viz-geoformats。 应为 WGS84 99 个点，可缩放/平移；无外部底图请求。 检查跨日期变更线的点，不将震源深度视作高度。 预期：检查跨日期变更线的点，不将震源深度视作高度。 |
| UGRID 海洋网格窗口<br>`viz-ugrid-window` | [插件样例｜Xugrid 荷兰高程三角网子集](http://localhost:7001/dataset/seek/viz-xugrid-netherlands)<br>`elevation_nl_512faces.nc`<br>[来源](https://github.com/Deltares/xugrid/blob/fae62b62b9ee7c4d32dcdb5f2eb27efef82e2b2a/data/elevation_nl.nc) | 27.0 KB<br>科学派生 | 打开 elevation_nl_512faces.nc，选择 viz-ugrid-window。 网格选 mesh，字段选 elevation，读取网格。 显示 512 个三角形、627 个节点及原始高程；未知 CRS 不自动投影到经纬度。 预期：显示 512 个三角形、627 个节点及原始高程；未知 CRS 不自动投影到经纬度。 |
| Aladin 天文天空图<br>`viz-aladin` | [NASA 哈勃 WFPC2 天文成像样本](http://localhost:7001/dataset/seek/nasa-hst-wfpc2)<br>`WFPC2ASSNu5780205bx.fits`<br>[来源](https://fits.gsfc.nasa.gov/fits_samples.html) | 63.4 KB<br>官方样例 | 打开 WFPC2ASSNu5780205bx.fits，选择 viz-aladin。 应加载本地 100×100 FITS 图像；检查 TAN 天球 WCS 存在，不要求公网底图。 预期：SIMPLE 主图像、100×100、RA---TAN / DEC--TAN；实际 Aladin 画布待浏览器验收。 |
| Cesium 三维地球<br>`viz-cesium` | [可视化样例·地图｜USGS 2024 年全球 M≥6 地震地图](http://localhost:7001/dataset/seek/viz-usgs-2024-m6-earthquakes)<br>`usgs-2024-m6-wgs84-2d.geojson`<br>[来源](https://earthquake.usgs.gov/fdsnws/event/1/) | 129.0 KB<br>科学派生 | 打开 usgs-2024-m6-wgs84-2d.geojson，选择 viz-cesium。 缩放/平移，检查 99 个全球强震点和属性，不依赖公网底图。 预期：99 个 USGS 2024 M≥6 二维 WGS84 点；深度km在 depth_km 属性，不作三维高度。 |
| MapLibre / deck.gl 地理要素<br>`viz-maplibre` | [可视化样例·地图｜USGS 2024 年全球 M≥6 地震地图](http://localhost:7001/dataset/seek/viz-usgs-2024-m6-earthquakes)<br>`usgs-2024-m6-wgs84-2d.geojson`<br>[来源](https://earthquake.usgs.gov/fdsnws/event/1/) | 129.0 KB<br>科学派生 | 打开 usgs-2024-m6-wgs84-2d.geojson，选择 viz-maplibre。 缩放/平移，检查 99 个全球强震点和属性，不依赖公网底图。 预期：99 个 USGS 2024 M≥6 二维 WGS84 点；深度km在 depth_km 属性，不作三维高度。 |
| OpenLayers 地理图层<br>`viz-openlayers` | [可视化样例·地图｜USGS 2024 年全球 M≥6 地震地图](http://localhost:7001/dataset/seek/viz-usgs-2024-m6-earthquakes)<br>`usgs-2024-m6-wgs84-2d.geojson`<br>[来源](https://earthquake.usgs.gov/fdsnws/event/1/) | 129.0 KB<br>科学派生 | 打开 usgs-2024-m6-wgs84-2d.geojson，选择 viz-openlayers。 缩放/平移，检查 99 个全球强震点和属性，不依赖公网底图。 预期：99 个 USGS 2024 M≥6 二维 WGS84 点；深度km在 depth_km 属性，不作三维高度。 |
| Shapefile 地图<br>`shapefile` | [Natural Earth 全球国家与地区边界](http://localhost:7001/dataset/seek/open-natural-earth-countries)<br>`ne_110m_admin_0_countries.shp`<br>[来源](https://www.naturalearthdata.com/downloads/110m-cultural-vectors/110m-admin-0-countries/) | 180.9 KB<br>科学原件 | 打开 ne_110m_admin_0_countries.shp，选择 shapefile。 确认同目录 SHX/DBF/PRJ 自动配对，全球国家/地区多边形和属性显示。 预期：Natural Earth 1:110m 图层，177 要素；小比例尺制图数据不用于精密测量或法律边界。 |
| NetCDF 地图<br>`netcdf-map` | [NOAA 近地面气温月气候态](http://localhost:7001/dataset/seek/open-noaa-air-climatology)<br>`air.sig995.mon.ltm.1991-2020.nc`<br>[来源](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html) | 657.4 KB<br>科学原件 | 打开 air.sig995.mon.ltm.1991-2020.nc，选择 netcdf-map。 变量 air，time=0（一月），查看经纬度图。 预期：一月气候态，degC；原73×144，受限显示按索引抽样，不是2020年单月实况。 |

### 数值曲线（16）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| Plotly 数值图表<br>`viz-plotly` | [可视化样例·数值曲线：NOAA 全球海洋表面大气甲烷月均曲线（1983—2026）](http://localhost:7001/dataset/seek/viz-noaa-global-methane)<br>`ch4_mm_gl.csv`<br>[来源](https://gml.noaa.gov/ccgg/trends_ch4/) | 21.8 KB<br>科学原件 | 点击 ch4_mm_gl.csv，选择 viz-plotly decimal 横轴、average 纵轴呈浓度曲线；预览窗口前200条、全文件515条 预期：decimal 横轴、average 纵轴呈浓度曲线；预览窗口前200条、全文件515条 |
| 衍射与散射曲线<br>`viz-diffraction` | [CoCrCuFeNi 高熵合金工艺对比 XRD 样本](http://localhost:7001/dataset/seek/mendeley-high-entropy-alloys)<br>`CoCrCuFeNi_AC.xrdml`<br>[来源](https://data.mendeley.com/datasets/x35xrkxpjb/1) | 19.8 KB<br>科学原件 | 从现有数据集 mendeley-high-entropy-alloys 打开 CoCrCuFeNi_AC.xrdml。 在视图选择器选择 viz-diffraction，核对以下预期并测试基础交互。 预期：Scan 1 展示 3,046 点，2Theta/deg 横轴与原始 counts；不归一化。 |
| 质谱谱图<br>`viz-mass-spectrum` | [插件测试·Pyteomics 单条 MGF 质谱官方示例](http://localhost:7001/dataset/seek/viz-plugin-mass-pyteomics)<br>`example.mgf`<br>[来源](https://github.com/levitsky/pyteomics/blob/5ace4adc7467f617db8ad30864a1746e65a91448/doc/source/_static/example.mgf) | 768 B<br>官方样例 | 打开 example.mgf，选择 viz-mass-spectrum。 检查结构或数值与说明一致，不启用外部网络请求。 预期：MGF显示1条谱、41个峰；m/z与原始强度原样保留。 |
| MCA 仪器谱<br>`viz-mca-spectrum` | [插件测试·Sigima 2048 道 MCA 官方样本](http://localhost:7001/dataset/seek/viz-plugin-mca-sigima)<br>`spectrum_ascii.mca`<br>[来源](https://github.com/DataLab-Platform/Sigima/blob/51ee0ed558a57a2f5be7dec72b95c3af742f5bca/sigima/data/tests/curve_formats/spectrum.mca) | 6.5 KB<br>官方样例 | 打开 spectrum_ascii.mca，选择 viz-mca-spectrum。 检查结构或数值与说明一致，不启用外部网络请求。 预期：2048道原始计数，横轴channel；不自动应用能量标定。 |
| NMRium 核磁谱图<br>`viz-nmrium` | [插件测试·NMRium 合成 1H 明文谱](http://localhost:7001/dataset/seek/viz-plugin-nmr-project)<br>`synthetic-1h.jdx`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 8.5 KB<br>项目夹具 | 打开 synthetic-1h.jdx，选择 viz-nmrium。 检查结构或数值与说明一致，不启用外部网络请求。 预期：合成1H谱1024点、PPM轴、两个峰；已处理JCAMP，不执行FID处理，不作化学解释。 |
| JSROOT 直方图与曲线<br>`viz-jsroot` | [插件测试·JSROOT 合成非均匀分箱直方图](http://localhost:7001/dataset/seek/viz-plugin-root-project)<br>`synthetic-histogram.root`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 12.9 KB<br>项目夹具 | 打开 synthetic-histogram.root，选择 viz-jsroot。 检查结构或数值与说明一致，不启用外部网络请求。 预期：合成TH1D直方图，边界0、1、3、6、10；非等宽分箱保持不变。 |
| IGV 基因组轨道<br>`viz-igv` | [原创基因组阅读器功能样例：SAM/BED/BLAST](http://localhost:7001/dataset/seek/viz-bio-alignments-and-tracks)<br>`synthetic-features.bed`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 99 B<br>项目夹具 | 参考文本填写chr_demo TAB 5000（来自demo.chrom.sizes），确认后本地浏览三条BED特征；不自动联网加载参考。 预期：明确提供本地chrom.sizes后，IGV绘制chr_demo标尺和本地BED注释，不联网猜测参考基因组。 |
| BLAST 命中工作台<br>`viz-blast-hits` | [原创基因组阅读器功能样例：SAM/BED/BLAST](http://localhost:7001/dataset/seek/viz-bio-alignments-and-tracks)<br>`synthetic-hits.blast6`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 125 B<br>项目夹具 | BLAST文件有正向与反向命中，数值仅为功能测试；不推断真实同源关系。 预期：点击筛选显示2条原创命中；最低一致性90过滤后剩1条，可点条形或表格打开详情。 |
| FASTQ 质量分析<br>`fastq-quality` | [Biopython官方FASTQ最小测试样例](http://localhost:7001/dataset/seek/viz-bio-biopython-fastq)<br>`example.fastq`<br>[来源](https://github.com/biopython/biopython/tree/5bbc6c12c505301f2d681f932c30fdb8fcbe9a6e/Tests/Quality) | 234 B<br>官方样例 | 质量概览确认3条完整读段；按Phred+33解释。 预期：显示3条官方测试read的逐位置平均Phred+33质量曲线；不是大规模测序统计。 |
| FASTA / FASTQ 序列浏览器<br>`viz-sequence-browser` | [Biopython官方FASTQ最小测试样例](http://localhost:7001/dataset/seek/viz-bio-biopython-fastq)<br>`example.fastq`<br>[来源](https://github.com/biopython/biopython/tree/5bbc6c12c505301f2d681f932c30fdb8fcbe9a6e/Tests/Quality) | 234 B<br>官方样例 | 序列浏览器选择record=0、start=1、窗口250、quality_encoding=phred33，点击显示所选序列；该记录实际25字符。 输入CCC并点击搜索，检查模体命中及逐碱基质量颜色。 预期：先点击显示所选序列，显示第一条25字符read及逐碱基质量，再搜索CCC字面模体。 |
| 流式细胞原始事件<br>`viz-fcs-window` | [FlowCal官方流式细胞示例FCS](http://localhost:7001/dataset/seek/viz-bio-flowcal-fcs)<br>`sample001-digital-values.fcs`<br>[来源](https://flowcal.readthedocs.io/en/latest/python_tutorial/read.html) | 2.11 MB<br>官方样例 | 打开sample001-digital-values.fcs而非24-bit原件，检查33024事件和8通道。 选择scatter、channels=[0,1]、event_offset=0、event_count=256；显示原始数字值，不能自动作原仪器线性化或MESF解释。 预期：目录33024事件/8通道，读取前256事件后绘制TIME与FSC原始数字值散点，不做原仪器指数/增益/补偿解释。 |
| EDF/BDF 通道时间窗<br>`viz-edf-signals` | [PhysioNet公开EEG基线与明确标注的声化转换](http://localhost:7001/dataset/seek/viz-bio-physionet-eeg)<br>`S001R01.edf`<br>[来源](https://physionet.org/content/eegmmidb/1.0.0/) | 1.28 MB<br>科学原件 | EDF选择通道0，起点2秒、时长3秒并点击读取时间窗；检查160Hz与物理单位；不要把注释通道作普通信号。 预期：真实EDF第一通道曲线可见，明确读取2–5秒窗口，160Hz采样；注释通道不作为普通信号。 |
| 音频与 WAV 波形<br>`viz-audio-waveform` | [PhysioNet公开EEG基线与明确标注的声化转换](http://localhost:7001/dataset/seek/viz-bio-physionet-eeg)<br>`eeg-channel0-sonification.wav`<br>[来源](https://physionet.org/content/eegmmidb/1.0.0/) | 19.6 KB<br>科学派生 | WAV选择音频波形/播放；约1.22秒对应约61秒EEG，不能作原始声音或诊断解释。 预期：WAV显示1声道/8000Hz/9760帧峰值包络，1.22秒可播放和跳转；是EEG的50倍时间压缩声音化，不是原始声学观测。 |
| MiniSEED / SAC 地震波形<br>`viz-seismic-window` | [插件样例｜USGS ANMO 宽带垂向地震记录](http://localhost:7001/dataset/seek/viz-usgs-anmo-seismic)<br>`IU.ANMO.10.BHZ.first-record.mseed`<br>[来源](https://www.fdsn.org/networks/detail/IU/) | 512 B<br>科学派生 | 打开 IU.ANMO.10.BHZ.first-record.mseed，选择 viz-seismic-window。 记录 0，样本起点 0，样本数 223，读取波形窗。 应为 223 点、0.025 秒间隔、STEIM2；振幅单位 unknown，不能标为 nm/s。 预期：应为 223 点、0.025 秒间隔、STEIM2；振幅单位 unknown，不能标为 nm/s。 |
| FITS 数值曲线<br>`fits-series` | [NASA 哈勃 FOS 光谱误差数据样本](http://localhost:7001/dataset/seek/nasa-hst-fos)<br>`FOSy19g0309t_c2f.fits`<br>[来源](https://fits.gsfc.nasa.gov/fits_samples.html) | 43.2 KB<br>官方样例 | 打开 FOSy19g0309t_c2f.fits，选择 fits-series。 HDU=0、横轴=axis1、axis0=0，绘制曲线。 预期：原数组2×2064，FITS FILETYPE=ERR，实际误差谱不是通量谱；横轴 PIXEL，单位 ERGS/CM**2/S/A，预览可抽样到最多1000点。 |
| NetCDF 数值曲线<br>`netcdf-series` | [NOAA 近地面气温月气候态](http://localhost:7001/dataset/seek/open-noaa-air-climatology)<br>`air.sig995.mon.ltm.1991-2020.nc`<br>[来源](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html) | 657.4 KB<br>科学原件 | 打开 air.sig995.mon.ltm.1991-2020.nc，选择 netcdf-series。 变量 air，横轴 time，lat=36、lon=72（0°N、180°E）。 预期：12 个日历月气候态值，degC，不是逐年趋势。 |

### 表格（13）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| 表格<br>`csv` | [可视化样例·表格：Palmer 南极企鹅观测](http://localhost:7001/dataset/seek/viz-palmer-penguins)<br>`penguins.csv`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 15.2 KB<br>科学原件 | 点击 penguins.csv，选择 csv 344 行、8 列；NA 缺失原样保留 预期：344 行、8 列；NA 缺失原样保留 |
| DBF 只读数据表<br>`viz-dbf-table` | [插件测试 · DBF 国家属性表与原始边界](http://localhost:7001/dataset/seek/viz-test-dbf-natural-earth)<br>`countries.dbf`<br>[来源](https://www.naturalearthdata.com/downloads/110m-cultural-vectors/110m-admin-0-countries/) | 16.8 KB<br>科学派生 | 打开 countries.dbf；同目录完整 SHP/SHX/PRJ/CPG 使默认地图预览可以先打开。 在可视化选择器选择 viz-dbf-table，点击读取数据库分页，核对 8 列、177 行国家/地区属性。 切换下一页；POP_EST/POP_YEAR/GDP_MD/GDP_YEAR 保留来源值和缺失哨兵，不推定当前年份。 需要完整168字段时查看 original/；小比例尺边界不用于精确面积或法律边界认定。 预期：完整177行、8列；同名SHP配套保证默认地图可打开，再切DBF表格；所有原数值和坐标未修改。 |
| SQLite 只读表格<br>`viz-sqlite-table` | [插件测试 · SQLite 只读表格 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-sqlite-table)<br>`penguins.sqlite`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 28.7 KB<br>科学派生 | 选择文件 penguins.sqlite，再选择插件 viz-sqlite-table 选择 penguins，344条、8列；翻到下一页，bill_length_mm可画当前页数值图 预期：选择 penguins，344条、8列；翻到下一页，bill_length_mm可画当前页数值图 |
| DuckDB 只读数据库<br>`viz-duckdb-table` | [插件测试 · DuckDB 只读数据库 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-duckdb-table)<br>`penguins.duckdb`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 536.6 KB<br>科学派生 | 选择文件 penguins.duckdb，再选择插件 viz-duckdb-table 选择 penguins，344条、8列；SQL NULL保留，读文件不执行源SQL 预期：选择 penguins，344条、8列；SQL NULL保留，读文件不执行源SQL |
| Parquet / Arrow 分块表格<br>`viz-columnar-window` | [插件测试 · Parquet / Arrow 分块表格 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-columnar-window)<br>`penguins.parquet`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 18.1 KB<br>科学派生 | 选择文件 penguins.parquet，再选择插件 viz-columnar-window 344行、8列，6个行组；选择列并分页，仅读取窗口 预期：344行、8列，6个行组；选择列并分页，仅读取窗口 |
| SQL 转储字面量<br>`viz-sql-dump` | [插件测试 · SQL 转储字面量 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-sql-dump)<br>`penguins.sql`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 28.6 KB<br>科学派生 | 选择文件 penguins.sql，再选择插件 viz-sql-dump 先在来源数据库中选择 SQLite，再读取SQL转储目录；系统故意不推断方言。 静态解析 CREATE 与344条INSERT；不恢复数据库、不执行SQL 预期：静态解析 CREATE 与344条INSERT；不恢复数据库、不执行SQL |
| 压缩包目录<br>`viz-archive-directory` | [插件测试 · 压缩包目录 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-archive-directory)<br>`penguins-bundle.zip`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 6.7 KB<br>科学派生 | 选择文件 penguins-bundle.zip，再选择插件 viz-archive-directory 列出 penguins.csv 与 README.md 两个成员，不解压到宿主目录 预期：列出 penguins.csv 与 README.md 两个成员，不解压到宿主目录 |
| Access 只读数据库<br>`viz-access-table` | [插件测试 · Access 只读数据库 · Access 普通表](http://localhost:7001/dataset/seek/viz-test-access-table)<br>`synthetic-v2010.accdb`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 368.6 KB<br>项目夹具 | 预览主文件并选择 viz-access-table 选择 Samples 普通表；测试中文、空值、精确小数、日期，Empty为空表；不包含用户数据。 预期：选择 Samples 普通表；测试中文、空值、精确小数、日期，Empty为空表；不包含用户数据。 |
| MySQL 表空间结构目录<br>`viz-mysql-sdi` | [插件测试 · MySQL 表空间结构目录 · MySQL 8.0.46 元数据](http://localhost:7001/dataset/seek/viz-test-mysql-sdi)<br>`mysql-8.0.46.ibd`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 131.1 KB<br>项目夹具 | 预览主文件并选择 viz-mysql-sdi 检查 measurements 表字段、主键和二级索引元数据；不是数据库行数据恢复。 预期：检查 measurements 表字段、主键和二级索引元数据；不是数据库行数据恢复。 |
| SST 物理点记录<br>`viz-sst-records` | [插件测试 · SST 物理点记录 · RocksDB 物理键值](http://localhost:7001/dataset/seek/viz-test-sst-records)<br>`rocksdb-6.11.4.sst`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 855 B<br>项目夹具 | 预览主文件并选择 viz-sst-records 逐条浏览键值、序号、删除标记；二进制用安全表示，不执行数据库操作。 预期：逐条浏览键值、序号、删除标记；二进制用安全表示，不执行数据库操作。 |
| PostgreSQL 备份目录<br>`viz-postgres-dump` | [插件测试 · PostgreSQL 备份目录 · PostgreSQL 18.6 原生目录](http://localhost:7001/dataset/seek/viz-test-postgres-dump)<br>`synthetic-postgres-none.dump`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 2.0 KB<br>项目夹具 | 预览主文件并选择 viz-postgres-dump 浏览两张表及TABLE DATA目录；不恢复或解压数据段，不能当作行查询。 预期：浏览两张表及TABLE DATA目录；不恢复或解压数据段，不能当作行查询。 |
| Excel 工作表预览<br>`viz-excel` | [插件测试 · Excel 工作表预览 · UCI 建筑能效](http://localhost:7001/dataset/seek/viz-test-excel)<br>`ENB2012_data.xlsx`<br>[来源](https://archive.ics.uci.edu/dataset/242/energy%2Befficiency) | 76.2 KB<br>科学原件 | 预览 ENB2012_data.xlsx，选择 viz-excel 选择工作表，核对 X1–X8、Y1、Y2；切换行窗口；共有768条有效模拟记录 预期：768条有效模拟记录，10列。空白格式行不应被误认为实测数据。 |
| FastQC / MultiQC 测序质控<br>`viz-fastqc` | [Biopython官方FASTQ最小测试样例](http://localhost:7001/dataset/seek/viz-bio-biopython-fastq)<br>`example.fastq`<br>[来源](https://github.com/biopython/biopython/tree/5bbc6c12c505301f2d681f932c30fdb8fcbe9a6e/Tests/Quality) | 234 B<br>官方样例 | FastQC应完成解析生成报告；小样本的WARN/FAIL是质量评价，不等于工具失败。 预期：已获授权的一次3-read FastQC任务succeeded，Basic Statistics显示3条序列/25长度，MultiQC汇总和各模块表格可见；WARN/FAIL是小样本质量评价。 |

### 文本（2）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| 文本与代码<br>`text` | [可视化样例·文本：NOAA 月均二氧化碳观测](http://localhost:7001/dataset/seek/viz-noaa-mauna-loa-co2)<br>`co2_mm_mlo.txt`<br>[来源](https://gml.noaa.gov/ccgg/trends/data.html) | 59.6 KB<br>科学原件 | 点击 co2_mm_mlo.txt，选择 text 保留 NOAA 注释，可分页读取 预期：保留 NOAA 注释，可分页读取 |
| 归档成员文本预览<br>`viz-archive-members` | [插件测试 · 归档成员文本预览 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-archive-members)<br>`penguins-bundle.zip`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 6.7 KB<br>科学派生 | 选择文件 penguins-bundle.zip，再选择插件 viz-archive-members 选择 penguins.csv 成员，显示有界文本预览；切换 README.md 预期：选择 penguins.csv 成员，显示有界文本预览；切换 README.md |

### 结构（7）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| 分子与晶体结构<br>`molecular` | [RCSB 泛素蛋白晶体结构（1UBQ）](http://localhost:7001/dataset/seek/pdb-ubiquitin)<br>`1UBQ.pdb`<br>[来源](https://www.rcsb.org/structure/1UBQ) | 78.6 KB<br>科学原件 | 从现有数据集 pdb-ubiquitin 打开 1UBQ.pdb。 在视图选择器选择 molecular，核对以下预期并测试基础交互。 预期：3Dmol 正确解析 1 个模型、660 个原子；可旋转并显示蛋白结构。 |
| Mol* 生物大分子<br>`viz-molstar` | [可视化样例·三维结构｜RCSB 抹香鲸肌红蛋白三维结构（1MBN）](http://localhost:7001/dataset/seek/viz-pdb-myoglobin-1mbn)<br>`1MBN.cif`<br>[来源](https://www.rcsb.org/structure/1MBN) | 191.7 KB<br>科学原件 | 从现有数据集 viz-pdb-myoglobin-1mbn 打开 1MBN.cif。 在视图选择器选择 viz-molstar，核对以下预期并测试基础交互。 预期：Molstar 正确解析 1,260 条 atom_site 原子记录；模型显示完整且无外部 PDB 请求。 |
| GRO 多帧轨迹<br>`viz-gro-trajectory` | [插件测试·MDAnalysis 双帧水分子轨迹官方样本](http://localhost:7001/dataset/seek/viz-plugin-gro-mdanalysis)<br>`two_water_gro_multiframe.gro`<br>[来源](https://github.com/MDAnalysis/mdanalysis/blob/8b8875c73782c8c7f9710f6f6b9ae715bd5ad1bb/testsuite/MDAnalysisTests/data/two_water_gro_multiframe.gro) | 648 B<br>官方样例 | 打开 two_water_gro_multiframe.gro，选择 viz-gro-trajectory。 切换第 1 / 2 帧，确认每帧 6 个原子。 预期：2帧，每帧6原子；可切帧，坐标nm、速度nm/ps、时间ps。 |
| VTU 仿真网格与场<br>`viz-simulation-mesh` | [插件测试·scikit-fem 数值网格与 OBJ 表面](http://localhost:7001/dataset/seek/viz-plugin-mesh-scikit-fem)<br>`mesh.vtu`<br>[来源](https://github.com/kinnala/scikit-fem/blob/a9ea0d58af5364948ccbd8a89b4eaf05940cf407/docs/examples/meshes/cube_oriented_sub.msh) | 9.9 KB<br>官方样例 | 打开 mesh.vtu，选择 viz-simulation-mesh。 打开网格并选择 GmshEntity / 分量 0；其含义是实体分类标签，不是物理场。 预期：81节点、340单元，类型5/10；可切换第340个单元，坐标与场值单位未知。 |
| OBJ 三维模型<br>`obj` | [插件测试·scikit-fem 数值网格与 OBJ 表面](http://localhost:7001/dataset/seek/viz-plugin-mesh-scikit-fem)<br>`surface.obj`<br>[来源](https://github.com/kinnala/scikit-fem/blob/a9ea0d58af5364948ccbd8a89b4eaf05940cf407/docs/examples/meshes/cube_oriented_sub.msh) | 4.7 KB<br>官方样例 | 打开 surface.obj，选择 obj。 检查结构或数值与说明一致，不启用外部网络请求。 预期：显示由原网格提取的156三角面、81节点；可旋转和缩放。 |
| VTK 三维科学模型<br>`viz-vtk` | [插件测试·scikit-fem 数值网格与 OBJ 表面](http://localhost:7001/dataset/seek/viz-plugin-mesh-scikit-fem)<br>`surface.obj`<br>[来源](https://github.com/kinnala/scikit-fem/blob/a9ea0d58af5364948ccbd8a89b4eaf05940cf407/docs/examples/meshes/cube_oriented_sub.msh) | 4.7 KB<br>官方样例 | 打开 surface.obj，选择 viz-vtk。 检查结构或数值与说明一致，不启用外部网络请求。 预期：vtk.js解析81节点、156多边形；当前插件关闭，未进行运行中浏览器渲染。 |
| LAS 点云窗口<br>`viz-pointcloud-window` | [插件样例｜PDAL LAS 1.2 彩色点记录](http://localhost:7001/dataset/seek/viz-pdal-las12)<br>`1.2-with-color.las`<br>[来源](https://github.com/PDAL/PDAL/blob/58af674be2bcde44477ed0b51f5f0ac0a158ce8d/test/data/las/1.2-with-color.las) | 36.4 KB<br>官方样例 | 打开 1.2-with-color.las，选择 viz-pointcloud-window 并读取目录。 点起点 0，点数 1065，读取连续点窗口。 显示 1065 点及 RGB/强度/分类；缩放因子 XYZ 均 0.01，坐标单位未知，不能标为 EPSG:4326。 预期：显示 1065 点及 RGB/强度/分类；缩放因子 XYZ 均 0.01，坐标单位未知，不能标为 EPSG:4326。 |

### 文档（7）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| PDF.js 文档阅读<br>`viz-pdfjs` | [可视化样例·文档：科研软件稳健性开放论文](http://localhost:7001/dataset/seek/viz-plos-research-software)<br>`research-software-robustness.pdf`<br>[来源](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1005412) | 1.21 MB<br>科学原件 | 点击 research-software-robustness.pdf，选择 viz-pdfjs 10 页开放论文，翻页和缩放 预期：10 页开放论文，翻页和缩放 |
| HTML 文档<br>`html` | [插件测试 · HTML 文档 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-html)<br>`penguins.html`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 41.0 KB<br>科学派生 | 选择文件 penguins.html，再选择插件 html 静态网页展示344行观测，无脚本、外部资源 预期：静态网页展示344行观测，无脚本、外部资源 |
| Markdown 文档<br>`markdown` | [插件测试 · Markdown 文档 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-markdown)<br>`penguins.md`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 21.7 KB<br>科学派生 | 选择文件 penguins.md，再选择插件 markdown Markdown标题与8列表格；344条原始文本记录 预期：Markdown标题与8列表格；344条原始文本记录 |
| DOCX 清晰阅读<br>`viz-docx` | [插件测试 · DOCX 清晰阅读 · python-docx 段落和表格](http://localhost:7001/dataset/seek/viz-test-docx)<br>`blk-inner-content.docx`<br>[来源](https://github.com/python-openxml/python-docx/tree/v1.2.0/tests/test_files) | 11.9 KB<br>官方样例 | 预览 blk-inner-content.docx，选择 viz-docx 显示官方段落/表格文档；不执行宏或外链。 预期：显示官方段落/表格文档；不执行宏或外链。 |
| Word 文档预览<br>`viz-word` | [插件测试 · Word 文档预览 · python-docx 段落和表格](http://localhost:7001/dataset/seek/viz-test-word)<br>`blk-inner-content.docx`<br>[来源](https://github.com/python-openxml/python-docx/tree/v1.2.0/tests/test_files) | 11.9 KB<br>官方样例 | 预览 blk-inner-content.docx，选择 viz-word 显示官方段落/表格文档；不执行宏或外链。 预期：显示官方段落/表格文档；不执行宏或外链。 |
| ONLYOFFICE 本机只读阅读<br>`viz-onlyoffice` | [插件测试 · ONLYOFFICE 本机只读阅读 · python-docx 段落和表格](http://localhost:7001/dataset/seek/viz-test-onlyoffice)<br>`blk-inner-content.docx`<br>[来源](https://github.com/python-openxml/python-docx/tree/v1.2.0/tests/test_files) | 11.9 KB<br>官方样例 | 预览 blk-inner-content.docx，选择 viz-onlyoffice 显示官方段落/表格文档；不执行宏或外链。需现有本机文档服务就绪；未配置时记录依赖阻碍，不改变插件开关。 预期：显示官方段落/表格文档；不执行宏或外链。需现有本机文档服务就绪；未配置时记录依赖阻碍，不改变插件开关。 |
| PowerPoint 演示预览<br>`viz-powerpoint` | [插件测试 · PowerPoint 静态阅读 · 官方字体样例](http://localhost:7001/dataset/seek/viz-test-powerpoint-font)<br>`txt-font-typeface.pptx`<br>[来源](https://github.com/scanny/python-pptx/tree/v1.0.2/features/steps/test_files) | 25.8 KB<br>官方样例 | 预览 txt-font-typeface.pptx，选择 viz-powerpoint 检查文本字体与页面缩放，无外部资源请求；静态预览不执行演示动作 预期：静态文本字体页面成功显示，可以调整缩放。 |

### 结构树（4）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| 结构化数据树<br>`viz-structured-tree` | [插件测试 · 结构化数据树 · Palmer 企鹅格式转换](http://localhost:7001/dataset/seek/viz-test-structured-tree)<br>`penguins.json`<br>[来源](https://github.com/allisonhorst/palmerpenguins) | 77.8 KB<br>科学派生 | 选择文件 penguins.json，再选择插件 viz-structured-tree 展开 observations 数组，344项；JSON null对应原CSV的19个NA 预期：展开 observations 数组，344项；JSON null对应原CSV的19个NA |
| BSON 文档记录<br>`viz-bson` | [插件测试 · BSON 文档记录 · BSON 原生类型](http://localhost:7001/dataset/seek/viz-test-bson)<br>`synthetic-pymongo-4.17.0.bson`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 1.2 KB<br>项目夹具 | 预览主文件并选择 viz-bson 展开对象/数组；int64、Decimal128 保持精度；JS和正则仅显示安全标记，不执行。 预期：展开对象/数组；int64、Decimal128 保持精度；JS和正则仅显示安全标记，不执行。 |
| Redis RDB 键值记录<br>`viz-redis-rdb` | [插件测试 · Redis RDB 键值记录 · Redis 7.2.7 原生快照](http://localhost:7001/dataset/seek/viz-test-redis-rdb)<br>`native-redis-7.2.7.rdb`<br>[来源](https://github.com/km345297256/AI-DATASEEK) | 346 B<br>项目夹具 | 预览主文件并选择 viz-redis-rdb 浏览 DB 0/2，string/list/set/hash/zset；精确整数文本与 Unicode；不启动Redis、不恢复。 预期：浏览 DB 0/2，string/list/set/hash/zset；精确整数文本与 Unicode；不启动Redis、不恢复。 |
| 系统发育树 · Newick<br>`viz-phylogeny` | [可视化样例·系统树：放线菌 SepH 蛋白系统发育树（eLife 原始研究附件）](http://localhost:7001/dataset/seek/viz-elife-seph-phylogeny)<br>`seph_phyml_quoted.nwk`<br>[来源](https://elifesciences.org/articles/63387/figures) | 19.1 KB<br>科学派生 | 从现有数据集 viz-elife-seph-phylogeny 打开 seph_phyml_quoted.nwk。 在视图选择器选择 viz-phylogeny，核对以下预期并测试基础交互。 预期：720 节点、361 叶，枝长模式可用；标签搜索与折叠保留拓扑。 |

### 音视频（1）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| 本地视频<br>`viz-video-player` | [链霉菌 FtsZ-YPet 分裂环荧光延时观测（MP4）](http://localhost:7001/dataset/seek/viz-bio-elife-video)<br>`elife-63387-fig2-data1-v1.mp4`<br>[来源](https://elifesciences.org/articles/63387/figures) | 84.2 KB<br>科学原件 | 打开MP4并选择视频插件，播放并拖动进度条。 约11.75秒播放时间不等于生物学实验时间，不据此计算细胞分裂速率。 预期：510×74、11.75秒MP4能够播放并跳转中点；不是用播放秒数估算生物过程时长。 |

### 图网络（1）

| 插件 | 数据集与入口文件 | 大小 / 性质 | 操作与预期 |
|---|---|---|---|
| 科学关系网络 · Cytoscape<br>`viz-scientific-graph` | [可视化样例·关系网络｜Mangal 格陵兰植物—传粉者科学关系网络](http://localhost:7001/dataset/seek/viz-mangal-greenland-pollination-908)<br>`lundgren-olesen-2005-network-908.graphml`<br>[来源](https://mangal.io/api/v2/network?dataset_id=7&count=1000) | 12.3 KB<br>科学派生 | 从现有数据集 viz-mangal-greenland-pollination-908 打开 lundgren-olesen-2005-network-908.graphml。 在视图选择器选择 viz-scientific-graph，核对以下预期并测试基础交互。 预期：43 节点、63 有向边，原相互作用权重保留。 |

## GB 级窗口测试

[NOAA 四年气压层气温及 GB 窗口样例](http://localhost:7001/dataset/seek/viz-noaa-pressure-air-2022-2025)保留 2022–2025 四个年度原 NetCDF 文件，并提供 2025 全值连续 HDF5 副本。该副本保留气温 `1460×17×73×144` 及完整坐标，单位 K；没有抽样、补零或重复记录来“凑大文件”。来源与引用要求见 [NOAA PSL](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html)。

入口 `air.2025.contiguous.h5` 为 1,043,648,072 字节。选择 `air`，固定前两维索引为 0，后两维切片为 `[0:73:1, 0:144:1]`，点击读取显式切片：本机真实页面读取 **46,144 字节 / 2 次范围请求**并显示热图。窗口保持每次累计 8 MiB 安全预算；按原储存值展示，不擅自执行 CF 缩放或有限填充值掩码。这是指定切片的实测结果，不是所有 GB 文件的性能保证。

## 已知限制与保留的边界样本

1. **CZI-window**：16×16 ROI 已显示，约 7.9 秒；128×128 的真实范围请求长时间等待后返回 HTTP 503。原件、失败证据保留。小文件的 whole CZI 插件可显示 128×128；不据此宣称分块性能已解决。
2. **VTK**：原有设置是关闭，未自动启用。OBJ 样本通过 vtk.js 解析（81 点、156 面），运行中浏览器渲染未测试。若要测试它，可在插件管理中自行启用后使用对应入口。
3. **NOAA 原年度 NetCDF4**：原有 HDF5 分块索引超过读取次数预算，不能直接当作当前 array-window 的成功用例。保留原件，正向入口改为全值连续 HDF5，没有放宽读取上限。
4. **BigTIFF**：基础 TIFF 插件不兼容原 OME-BigTIFF；新增经典 TIFF 保留全部 1,440,000 个 uint16 像素，并已显示。Viv 使用原 OME 文件成功。
5. **独立 DBF**：[企鹅 DBF 边界样例](http://localhost:7001/dataset/seek/viz-test-dbf-penguins)的明确 DBF API 可读，但页面默认 Shapefile 匹配缺少同名 SHP。已在列表重命名为“边界测试”，未假造企鹅地理坐标。正向样例改用 Natural Earth 原几何+精选属性，177×8 单元与原件一致，已实际分页。原 Natural Earth 168 字段仍保留在 `original/`，不放宽 128 字段上限。
6. **PPTX 嵌入对象**：[官方图表边界样例](http://localhost:7001/dataset/seek/viz-test-powerpoint)含嵌入工作簿，当前策略预期拒绝；已明确重命名，原字节未删。正向入口是另一份不含嵌入对象的官方字体夹具，不关闭嵌入对象/外链检查。
7. **科学语义**：雷达、DICOM、NIfTI/NRRD体、空间表达及部分谱图为明确合成测试；MiniSEED只取首个完整记录且不做仪器响应校正；NASA FOS复用文件是误差谱，不能当成通量谱；ENVI月份不是波长；未知CRS/单位不做推断。SQL仅静态解析，MySQL SDI仅元数据，PostgreSQL仅TOC，不恢复数据库、不执行源SQL。

以上边界样本保留实体文件，可继续用于回归；本轮未删除任何数据。

## 保护原功能与回归检查

- 仅新增目录/文件/注册记录、来源描述、测试脚本和导入器来源枚举；未改 AgentLoop、PlanActFlow、SSE、FastAPI运行主机或插件实现，没有重新部署服务。
- 导入前后核对原61个数据集的公开元数据指纹、全部83插件开关；不改变旧数据，不把宿主路径写入API/URL/localStorage/sessionStorage。数据仍按allowlist校验并只读挂载。
- 浏览器使用独立临时配置，文件/预览接口真实；仅推荐问题接口使用明确的验收占位文本，未调用模型或创建Agent会话。FastQC仅执行一次明确范围的小样本AnalysisJob；ONLYOFFICE使用系统原有本机只读服务。
- 前端 `npm run type-check`、`npm run build` 通过；Compose 配置检查通过。
- 后端最后一轮完整测试（包含新增清单/安全测试）：6803 passed、31 skipped、190 subtests passed，耗时79.71秒。后端使用已有 `.venv`，没有安装依赖。
- **完整 sandbox pytest 未运行成功**：本机环境缺pytest/科学依赖组合，现有科学运行镜像缺pytest。没有为了测试临时安装或更改运行镜像；已用现有镜像的真实读取器、完整性比较与真实浏览器补充验证，不能将这些声称为完整sandbox回归通过。

## 仓库交付与再次验收

- 机器可读矩阵：`backend/app/resources/visualization-plugin-test-matrix.json`，83条，一插件一条，固定入口字节数和SHA-256。
- 冻结来源：`backend/app/resources/visualization-descriptors/plugin-tests-*.json`；注册清单：`backend/app/resources/visualization-plugin-datasets/plugin-tests/*.json`。
- 准备/导入：`backend/scripts/prepare_visualization_datasets.py --profile plugin-tests` 与既有 `import_curated_host_datasets.py --source plugin-tests`；正式数据不会被不一致内容覆盖。
- 离线转换：`backend/scripts/plugin_samples_*.py` 及 `backend/scripts/plugin_sample_reproduction/README.md`，保留原数据、单位和转换说明。GB离线准备预算显式设定，不改插件运行预算。
- 本机API只读验收：`backend/scripts/check_plugin_dataset_samples.py`。先用 `baseline --baseline <新基线JSON>` 保存当前状态，再以 `check --baseline <同一JSON> --matrix backend/app/resources/visualization-plugin-test-matrix.json --report <报告JSON>` 运行。只允许显式回环地址，不跟随重定向/代理，不触发模型、Agent或新质控任务。
- FastQC只读复查可另传 `--completed-jobs-json <运行快照JSON>`，快照格式为 `{"viz-fastqc":{"dataset_id":"viz-bio-biopython-fastq","entry_file":"example.fastq","job_id":"已有32位任务ID"}}`。没有快照或旧结果已失效时明确记为 `not_run`，不会自动新建任务。本轮复查使用已成功的同一任务。
- 本轮详细截图、原始失败与重试证据位于 `.cache/plugin-samples-20260918/`；缓存不作为分发数据，也不把大型截图或实体GB文件提交到Git。精简验收摘要另存 `docs/visualization-plugin-validation-2026-09-18.json`。
- 一次最终API复查途中，其他并行工作重新创建了前端容器，导致连接重置/拒绝；该次记录保留为中断历史，不将它归类为数据格式失败。本轮未执行该部署，也未覆盖并行任务的Cesium等代码修改。

这是可重复的功能检查起点，不是全量科学有效性、性能或安全认证。以后每增加一个插件，矩阵完整性测试会提示补上相应样本。
