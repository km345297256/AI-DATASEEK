# main 可视化能力迁移至 Cordis

日期：2026-09-11。目标分支为 `v2`；对照源是拉取后的 `origin/main` 固定提交 **`6f6b32eebf76ae502adfe6d74e2c77ab92d1968f`**（2026-09-10）。没有切换分支、合并整个 main、自动提交或推送。

## 迁移结果

main 的旧注册表有 12 项，天文与 TIFF 指向同一实现，去重后为 11 组。现有 68 个 Cordis 插件中已经有分子、PNG、OBJ、Shapefile、HTML 等对应能力。本次补入 6 个独立工作台，目录变为 **74 个插件、73 个可信 adapter**，并保留当前各领域已集成的工具。

| main 的能力 | Cordis 插件 | 迁入交互 |
| --- | --- | --- |
| 高维矩阵 | `viz-matrix-workbench` | NPY/NPZ、稀疏 NPZ、MTX、MAT；变量/轴/切片、复数四分量、热图/表格、原索引 ROI、稀疏非零结构、三维联动、播放、剖面与已保存结果曲线 |
| FITS/TIFF 科学图像 | `viz-astronomy-workbench` | HDU/立方切片、TIFF 页/波段/RGB、拉伸/色图、直方图、像素与 WCS、区域统计、峰候选、表格/光谱、GeoTIFF 元数据；明确支持有界 `.fits.gz` |
| SAM/BAM/CRAM 比对 | `viz-alignment-browser` | 参考序列与区间、覆盖度、read 排列、CIGAR 块/错配/插入/删除/剪接、配对、详细信息、缩放/平移、当前区域 JSON 导出 |
| FASTA/FASTQ 序列 | `viz-sequence-browser` | 多记录、位置标尺、窗口与跳转、GC 概览、字面模体搜索、命中导航、质量编码与轨道、复制窗口；支持 `.faa` 序列别名 |
| 基因组注释 | `viz-genome-tracks` | VCF/GFF/GTF/BED、WIG/BedGraph；染色体/区间、平移/缩放、轨道开关、链方向、特征详情、真实信号值 |
| BLAST 命中 | `viz-blast-hits` | Query 选择、一致性与已知覆盖率筛选、分页、正反向命中图、详情、结果复制与极小 E-value 原文 |

新增插件默认启用，优先级 `-20`，不改旧插件标识、已有优先级或用户开关。用户可以在插件页启停，并在文件预览中选择工作台。同一扩展名可以有多个不同用途的插件，例如 TIFF 图像、地理视图与科学图像工作台。

main 已有能力不是重复安装：分子与晶体沿用当前 `molecular`、Mol* 等插件，其中 `.mmcif` 由既有 Mol* 处理；PNG/OBJ/Shapefile/HTML 保留现有 Cordis 适配。未被 main 注册的 `BioTextPreview.vue` 旧表格、实际上只映射到 ImageFilePreview 的“任意 API/component”占位项，不再建成虚假的新工具。

## 协议适配，而不是复制旧注册表

1. `contracts/visualization-adapters.json` 为可信 adapter/readers 的单一声明来源，生成前端、FastAPI 和 Cordis host 的三份映射；插件 manifest 没有动态 import URL、执行脚本或任意 endpoint。
2. 六插件只使用现有 `POST /api/v1/files/{opaque_id}/visualization`，保留 contract v2 外层 `plugin_id/version/revision/kind/payload/metadata/warnings/sampled`，不添加 main 的 prepare/render/release 私有通道。
3. 首次只读取目录/头信息；所有数据窗口、像素、区域或曲线请求，必须携带客户端从首次响应取得的版本。浏览器不传宿主路径，不写入 URL、localStorage 或 sessionStorage。
4. API 在读取前和返回后检查用户权限、私有 spill、插件状态、版本、目录修订与预算；worker 结果还要通过独立、精确的惰性 schema，绑定原始格式、源大小与请求选择。backend/sandbox 的纯 schema 副本逐字一致。
5. 原生解析只在现有一次性 worker 内：无网络、非 root、只读根文件系统、无数据集挂载、无凭据、受限 tmpfs/CPU/内存/PID、独立硬超时、输出截断防护及退出清理。
6. 前端只按构建期可信映射加载组件。切换文件、选择、插件状态/版本、关闭及卸载会取消旧任务，迟到响应不重新挂载；视图清理不遗留 worker/blob。

没有迁入 main 的主机临时文件 TTL 缓存、签名 URL 全量直读、S3 分支变动、旧插件管理器。没有改动 AgentLoop、PlanActFlow、SSE、领域工具注册或数据集 allowlist/只读挂载边界。本工作区其他并行任务的分析链路修复不属于本迁移。

### 比对与注释插件组合

比对工作台从当前预览的**已授权关联文件**选择注释，并通过独立启用的 `genome-tracks` 请求目录和窗口。两个源文件各自绑定版本，停用注释插件立即移除叠加。

这取代 main 浏览器里直接、不受限地解析本地注释文件的做法。当前没有额外上传入口：没有已授权关联注释时，明确显示没有可选文件。不会扫描邻居目录、根据文件名查找参考文件、隐式上传或扩大资源授权。染色体名称相同也不代表参考组装相同，用户需要确认；不做坐标重映射。

## 重要安全与科学语义修正

- **CRAM 外部参考回退**：实际反例证明仅指定 dummy 参考不足，原生库仍会读头部 `UR`。现先在隔离 tmpfs 用固定参数重写 CRAM 头，去除 `UR`、保留 `SN/LN/M5` 校验，拒绝路径型参考名，并把参考查找/缓存设为新建空目录。内嵌/无参考编码可用；依赖外部参考者明确拒绝。合法参考文件仍存在的同进程与新进程测试都不能读它。重写子进程以 OS 文件描述符重定向，CRAM 二进制不会进入 JSONL 输出。
- **比对的真实性与效率**：深度按 M/=/X 覆盖碱基除以箱宽计算，不计删除/剪接；展示或扫描截断分别说明。CIGAR/MD 用 run walker，1600 万长度的 intron 也不展开逐碱基数组；结果覆盖度校验使用 `O(blocks + bins)` 差分算法。缺 MD 不猜错配，MAPQ 255 明确为不可用。跨参考或非互为配对的 reads 不画配偶连接。
- **BLAST 覆盖率**：没有 Query 总长不猜测覆盖率。仅有明确 qlen 声明才计算覆盖率；反向区间与 `1e-350` 等原始 E-value 保留。
- **序列与轨道**：Phred+33/+64 必须明确选择，不自动猜；FASTA 不一概标成 DNA。VCF/GFF/WIG 规范转换到 0 基半开；零长度 BED 插入不伪造长度，WIG/BedGraph 空隙不插值，VCF `A→G`/`N→〈DEL〉` 标签不丢失含义。
- **矩阵与图像**：不加载对象/pickle、MAT 外链/VDS/未知过滤器；稀疏非零结构不等同随机抽样。缺失值在曲线断开、图像透明，显示降采样不改变原始像素与 ROI 查询坐标。

## 明确的兼容性边界

| 插件 | 完整源输入 | 主要显示/解码约束 |
| --- | --- | --- |
| Matrix | 128 MiB，读取存储时每段仍 ≤64 MiB | 16 维、每显示轴 ≤512 点，专属结果 ≤8 MiB；不扩大旧 array 的 16,384 标量限制 |
| Astronomy | 32 MiB；gzip 解压后也 ≤32 MiB | 总解码预算 128 MiB、单平面约 209 万标量、64 HDU/页、PNG 最长边 1024，结果 ≤4 MiB |
| Alignment | 64 MiB | ≤10 万扫描记录、400 万查询碱基、200 万碱基区域、2000 展示 reads、1000 分箱，结果 ≤4 MiB |
| Sequence/Genome/BLAST | 各 16 MiB | 各自记录/窗口/显示预算，结果各 ≤2 MiB；详细限制见子文档 |

这些为 **whole 有界预览**，不是大型文件的索引/分块服务。尤其没有把 main 的 Astronomy 2 GiB、Alignment 1 GiB 主机缓存能力原样搬入。超预算明确拒绝并提示改用分析工具，不静默少读后声称完整。

保留已有 HDF5/NetCDF、GeoTIFF 等分块插件作为不同场景的选择；大型 FITS/BAM/CRAM 的专属范围读取、受控多文件参考/索引授权可另行设计。公网天文调查服务/底图、不受控外部参考、任意用户执行器不在此次迁移范围。

## 验证与发布

### 已上线：2026-09-11

统一发布已更新既有 backend/frontend 和供新任务使用的 sandbox 镜像，本机入口仍为 `http://127.0.0.1:7001/plugins?tab=renderers`；正式插件 API 返回 `engine=cordis`、74 个插件。未新增服务栈/公网端口，未重启旧任务容器或基础存储服务。

本次六插件的真实 HTTP 验收 **100/100 通过**：32 项正常读取、68 项格式/参数/版本/权限/停用等拒绝检查；20 个合成上传全部删除，`cleanup_errors=[]`、`business_state_preserved=true`、模型接口调用数为 0。验收使用正式运行镜像，不只是源码挂载测试。

统一版本的后端测试为 4,743 通过、32 跳过、151 subtests；沙箱 2,787 通过、0 跳过、449 subtests。冻结代码的 14 个迁移 Chrome 场景全部通过。其他批次、历史 HTTP 回归和镜像一致性汇总由[本机统一发布记录](changes-2026-09-11-unified-local-release.md)持续记录，不与本次六插件的 100 项接口验收混计。

### 开发与冻结检查

代码冻结前：矩阵、科学图像、生物序列/轨道/BLAST、比对已分别通过真实格式解析、前后端严格契约、版本/权限/取消回归和真实 Chrome 测试。比对最终专项为 **43 项前端 +164 项原生/统一 worker、20 subtests**，包括跨插件注释状态与独立版本。

冻结后的统一 Chrome 复验为 **14/14 通过**：Matrix 1、Astronomy 3、Bio 7、Alignment 3；0 个外部请求、0 个非预期 API 请求，所有视图卸载后没有存活 worker/blob。初轮中发现并已修复的 fixture 插件身份绑定、轨道指针捕获和错误场景准备步骤不作为最终失败遗留。

可重复入口：

```bash
node scripts/sync-visualization-contract.mjs --check
cd frontend && npm run type-check && npm test && npm run build
cd ../plugin-host && npm test
```

完整后端/沙箱测试按项目既有命令运行；原生参考库验证使用一次性测试容器，不把测试库装入生产环境。`sandbox/tests/test_main_migration_worker.py` 覆盖六插件真实 worker/stdio 接线；`backend/tests/test_main_migration_visualization.py` 验证专用协议与原有边界。

部署后验收脚本：`backend/scripts/check_main_migration_visualization_http.py`。在同一栈的本机入口串行运行，先由无网络、非 root、无挂载 worker 生成 20 份合成文件，再检查真实 API 的正例、错误格式、无版本/错版本、跨用户、禁用、恢复。只清理本次精确标识的测试上传，恢复偏好/测试所有者，核对业务快照；不调用 Agent 或模型，不读取用户数据集。

本任务没有独立发布：统一发布任务协调当前同一工作目录内的其他批次与 BUG 修复，使用既有 `./run.sh` 完成上线。完整发布结果以统一发布报告为准；本节已上线状态以正式服务与实际 HTTP 验收为依据，不把“代码冻结/编译通过”等同部署。

详细说明：

- [高维矩阵迁移](main-matrix-workbench-migration.md)
- [科学图像工作台迁移](main-migration-astronomy-workbench.md)
- [生物数据可视化迁移](main-migration-bio-visualizations.md)

源代码对照：[main 固定提交](https://github.com/km345297256/AI-DATASEEK/tree/6f6b32eebf76ae502adfe6d74e2c77ab92d1968f)。CRAM 的参考查找语义参见 [pysam AlignmentFile 文档](https://pysam.readthedocs.io/en/v0.23.3/api.html)，本项目采用真实反例验证而非仅依赖参数说明。
