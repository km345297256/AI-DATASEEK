# 科学领域扩展第二批：系统树、ENVI、GRIB、地震波形

日期：2026-09-10。新增四个可独立启停的 Cordis 插件，目录由 52 增至 **56 个插件、55 个批准适配器**。实施路线见[分批路线](domain-visualization-expansion-roadmap.md)。

## 新能力与使用方式

在本机[可视化插件管理](http://localhost:7001/plugins?tab=renderers)中启停插件，再从文件预览的视图菜单选择。插件停用不删除数据、不关闭同格式的其他插件。

| 插件 | 可用能力 | 首版边界 |
| --- | --- | --- |
| `viz-phylogeny` 系统发育树 | 矩形拓扑／枝长比例、标签搜索定位、子树折叠和展开 | 单棵 Newick，`.nwk/.newick/.tree/.tre`；不支持 NEXUS、phyloXML、NHX、多树或注释。内部数字标签不自动当支持度，缺失枝长不补零 |
| `viz-envi-window` ENVI 波段与像元光谱 | 双文件目录、单波段区域、像元的连续波段光谱窗口 | 从已登记数据集的 `.hdr` 打开，唯一配对原始数据；ENVI Standard、无压缩 BSQ/BIL/BIP、实数类型。不是通用 `.hdr` 或 ESRI EHdr，不支持单独上传头文件 |
| `viz-grib-window` GRIB 气象场 | 分页消息目录、显式选择场和经纬区域、原值与缺失位图热图 | GRIB2、规则经纬网 GDT 3.0、瞬时 PDT 4.0、简单打包 DRT 5.0；非全格式 GRIB 浏览器，不自动合并变量、层、时刻或重投影 |
| `viz-seismic-window` 地震波形 | 分页记录目录、单记录样本窗、时间与单位声明、局部连续性提示 | MiniSEED v2 固定记录长度，受限 blockette 与 INT16/INT32/FLOAT32/FLOAT64/STEIM1/STEIM2；均匀采样二进制 SAC6/7。不支持 MiniSEED v3、全 SEED、SAC ASCII／频谱或不等间隔数据 |

系统树的枝长模式仅在非根枝长完整且不全为零时启用；拓扑显示不表达进化距离。使用自有受限 SVG 视图，没有将 Phylotree 桌面／Web 软件完整搬入项目。详见[Newick 支持规范](phylogeny-newick-format.md)。

### ENVI：明确的双文件作用域

1. 将 `.hdr` 与原始数据文件置于同一个已登记、只读的数据集目录。
2. 支持 `cube.hdr + cube.bin` 一类同名配对，也支持 `cube.bin.hdr + cube.bin`；数据后缀为 `.img/.dat/.bin/.raw/.bsq/.bil/.bip`，或无后缀。同名多候选直接拒绝，不猜测选择。
3. 从 `.hdr` 打开。首次只读取头文件；选择单波段区域或像元及连续波段范围后，显式读取数据。
4. 图像按原始行列索引显示。光谱坐标和单位使用头文件声明；无声明时使用波段索引。不会把 GHz/MHz/波数统一解释为波长，不排序、不换算。

头文件最大 64 KiB，支持数据类型 1/2/3/4/5/12/13（uint8、int16、int32、float32、float64、uint16、uint32）。不支持复数、64 位整数、压缩、分类、指针型 `data file` 字段或嵌套元信息。原始数据长度必须精确匹配维数、类型和 header offset。

`data ignore value` 按实际存储类型比较；拒绝无效标记、溢出和非零标记下溢为零。缺失值、NaN/Inf 留空，不插值。不应用增益、反射率缩放、坏波段修复或地理配准。

## 协议与权限

- 公有 `contract_version: 2` 和 `POST /files/{opaque_id}/visualization` 保持不变；未新建业务通信层、任意 range/tile URL 或旁路接口。
- 系统树使用批准的 `tree` 结果与专用 `phylogeny` 对象；不混同 NeXus、普通文件树或关系网络。
- GRIB／ENVI 走已有 `array` 结果，地震波形走 `series`；各自严格绑定参数、格式、源大小、实际读取量、目录选择和科学元数据。
- ENVI 复用既有对象作用域的身份、stat、范围、最终快照和取消控制，只增加精确两个资源：`header` 与 `data`。不解析头文件提供的主机路径、不返回配套文件身份，不授予任意同目录读取。
- ENVI 两个文件及声明共同构成版本；只读了头文件时，最终校验仍检查未读取的数据文件。任一成员变化，旧窗口版本失效。
- GRIB 非首目录页及地震目录分页绑定初次版本；所有新数值窗口必须由客户端携带目录版本，宿主不能代为补上缺失版本。
- 新清单仅授予 `file:read`，`shared:false`，不授权 `bytes/prepare/page/job`、URL、SQL、凭据或写入。
- AgentLoop、PlanActFlow、SSE、FastAPI、Docker 只读数据隔离边界保持不变。打开可视化不运行模型、拟合、滤波或隐式 AnalysisJob。

## 性能与资源限制

| 能力 | 预算 |
| --- | --- |
| Newick | 整文件 ≤4 MiB，输出 ≤1 MiB，≤1000 节点／64 层；有界 SVG，不依赖网络布局 |
| 三类新窗口 | 源或精确配对总计 ≤8 GiB；每次累计读取 ≤8 MiB，单次范围 ≤1 MiB，输出 ≤2 MiB |
| ENVI | ≤2048 波段；图像 ≤128×128，单次光谱 ≤128 波段，≤256 次范围读取。附近样本合并读取，中间跨过的字节也计入实际预算 |
| GRIB | ≤128 次范围读取，每页最多 8 条消息，单消息 ≤1 MiB、完整网格 ≤16384 点；显示 ROI 不等于只解码 ROI，解码量如实显示 |
| MiniSEED／SAC | ≤128 次范围读取，每页最多 16 条记录，输出 ≤16384 样本；STEIM 先解码命中的整记录再取窗口，不把输出样本数冒充解码量 |

ENVI 首次不读整个立方体；SAC／未压缩 MiniSEED 直接读选定样本，STEIM 不能直接跳入差分链；GRIB 初始目录不展开 Section 7 数据值。过大区域、消息或编码直接拒绝，不静默降级全文件读取。上述是读取／输出预算，不代表完整进程 RSS 或固定倍数性能提升。

窗口解析继续在无网络、无端口、无主机与数据集挂载、非 root、只读根文件系统的短命容器中执行，保留既有 CPU、内存、并发及硬截止限制。

## 依赖与验证方法

- 新增生产依赖：Python `eccodes==2.48.0`，Linux 原生 `eccodeslib==2.48.2.27`，锁定的传递依赖 `eckitlib==2.2.0.27`；C ecCodes 运行时为 `2.48.2`。使用镜像内定义与样例资源，不依赖宿主系统库或外部下载。
- 兼容性回归发现：上游 Python SDK 的 `findlibs` 递归全局加载 eckit 动态库，会使既有 CZI ZSTD 写入崩溃。生产 GRIB 改用固定版本官方 ecCodes C 核心的最小只读适配层，明确且局部加载所需动态库；不修改全局环境、第三方函数或旧 CZI 实现。官方 Python SDK 仅在独立测试子进程生成参照样本，生产仍使用真实 ecCodes 解码，不自写 GRIB 解码算法。
- ENVI 受控范围读取使用标准库，输出由既有 Plotly 绘制，并以 GDAL 生成的真实 ENVI 文件独立核对 BSQ/BIL/BIP。
- 地震读取使用有界实现；测试环境临时安装 ObsPy `1.4.2`，用其 libmseed/SAC 实现独立交叉验证。ObsPy 不加入生产依赖。
- 前端不新增运行依赖，系统树使用受控 SVG，数值图复用现有 Plotly。
- `check_domain_batch_two_http.py` 验证新单文件插件的本机 HTTP，以及孤立 ENVI 头文件拒绝；仅使用精确标记的测试副本并恢复开关。
- `check_envi_scope_worker.py` 在一次性环境的临时目录中验证 ENVI 配对→主机范围代理→真实隔离 worker，包括两文件版本及未读成员最终检查；不登记或更改生产数据集。该项不能冒充已在真实业务数据集上验收。

格式依据：[ENVI 官方头文件定义](https://www.nv5geospatialsoftware.com/docs/enviheaderfiles.html)、[ENVI ignore value 类型语义](https://www.nv5geospatialsoftware.com/docs/envi_setup_head.html)、[GDAL ENVI 驱动](https://gdal.org/en/stable/drivers/raster/envi.html)。各领域详细边界以专用格式文档和测试为准。

## 验收状态

已完成源码回归、最终镜像复测、本机部署及实际接口验收，以下不把跳过项计为通过。

| 检查 | 结果 |
| --- | --- |
| 前端完整测试 | 1,206 通过，0 跳过；type-check、build 通过，保留既有大 bundle 提示 |
| 后端完整测试 | 3,537 通过，32 条既有条件跳过；其中新增 ENVI 双资源代理边界 28 条通过 |
| 科学运行环境完整测试 | 1,769 通过，0 跳过；强制加载科学依赖及 ObsPy 独立参照，133 个子断言另计 |
| Cordis 插件主机 | 59 通过，包括新增插件的真实生命周期与启停隔离 |
| 新视图真实 Chrome | 13 通过：Newick 4、ENVI 4、GRIB 2、地震 3；核对图形、坐标、取消及释放，零外联／页面异常 |
| 旧视图真实 Chrome | 10 通过：CSV、科学曲线、地图、FASTQ QC、Shapefile、结构树、生理信号、数组曲线／热图、OME-Zarr |
| 协议与配置 | 55 个适配器生成副本一致；Compose 配置和变更空白检查通过 |

Chrome 视图测试使用合成的真实读取器输出和受控接口响应，不冒充真实用户数据集的端到端验收。后端跳过项需要独立的实时服务／数据库测试配置或属于既有兼容测试；实际 HTTP 验收另列，不将其自动折算成全部跳过项已覆盖。

特别回归：GRIB、旧 CZI 与新增原生兼容测试共 92 条在同一测试运行内通过；两个独立初始导入顺序均在同一子进程内执行真实气象数值读取和 CZI ZSTD 写后读校验。地震解析 96 条独立参照测试和 24 组只读审阅断言通过；末帧非法尾编码采取更严格拒绝，未虚称 ObsPy 1.4.2 的旧 libmseed 也拒绝该输入。

### 实际本机接口与隔离运行验收

| 验收 | 正常路径 | 拒绝路径 | 临时文件上传／清理 |
| --- | --- | --- | --- |
| 本批 Newick／GRIB／MiniSEED／SAC 与孤立 ENVI 头文件 | 7 | 22 | 8／8 |
| 前一领域批次 Parquet／Arrow、科学网络、NeXus | 11 | 18 | 8／8 |
| 旧 HDF5、CZI 与仪器图像窗口 | 9 | 29 | 7／7 |
| 既有 NetCDF 地图／曲线、FITS 图像／曲线、FASTQ | 7 | 5 | 4／4 |

四组实际 HTTP 验收共 **108 项通过**。使用同一个已部署 frontend/API 和真实隔离读取器；27 个测试副本均清理完成，可以由夹具重新生成。原始数据未删除，个人插件偏好恢复并读回核验，无清理错误、无新增会话或模型调用。四组验收分别核验业务摘要一致，不声称整个数据库字节不变。

ENVI 单独在一次性后端的临时托管目录验证真实「数据集引用→双文件作用域→范围代理→隔离 worker」：3 项正常、6 项拒绝通过，核验 4 个实际 worker 无网络／无数据集挂载／非 root／只读根目录，结束无残留，临时源已清理。没有给生产数据集登记测试路径；不把该检查描述为使用了用户实际数据集。

通过 `./run.sh` 更新现有前后端和 sandbox 镜像；重启前确认无运行／等待会话和活动 AnalysisJob，仅重新创建 frontend/backend，数据库、Redis、MinIO 和 Office 服务未重启。最终构建镜像再次执行科学环境完整回归，仍为 **1,769 通过、0 跳过**。

最终只读核验：`GET /api/v1/visualizations` 返回 `engine: cordis`、56 个插件，四个新插件均启用、契约版本为 2。前端仍只绑定 **127.0.0.1:7001**，未增加第二套栈或公网端口。刷新[插件管理](http://localhost:7001/plugins?tab=renderers)即可看到新能力。

本轮本机日志目录：`/private/tmp/dataseek-domain-batch-two.DgHXNj`（临时诊断资料，可能被系统清理）。包含完整测试、镜像构建、部署、真实 ENVI worker 和四组 HTTP 结果；本文件保留关键结果与支持边界。未提交或推送 Git。

## 下一批

质谱、流式细胞术、衍射／散射、电子显微谱像，尚未实施。
