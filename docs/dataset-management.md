# 本机数据集管理与开放数据目录

入口：<http://localhost:7001/datasets>（侧栏「数据集管理」）。

## 使用

1. 按系统领域和来源组合筛选，或搜索名称、简介和来源。来源包括「本地数据」「国际开放数据」「ScienceDB」「青藏高原数据中心」「化学数据中心」「国家基因组科学数据中心」；分类计数不代表机构数据库总量。
2. 点击数据集名称或「进入探查」，复用现有 /dataset/seek/:datasetId 页面。原始数据只读挂载，衍生成果写沙箱输出区。
3. 管理员通过「添加本地数据集」登记名称、简介、领域与允许目录内的绝对路径。默认输入为 `/Users/luchangfa/Documents/Codex/Data`；建议选择其中具体的数据集子目录。真实路径不进入 URL、localStorage、sessionStorage 或 API 响应。
4. 新登记持久保留。编辑只更新名称、简介和领域；「移除」仅归档登记，保留原始文件和既有成果，不释放磁盘空间。
5. 探查页可在新任务前选择 Agent 配置；领域不匹配会提示。数据集分类不会偷偷改写 Agent 配置或开启 Code Mode / SubAgent。

### 本地数据长期保留与旧会话

页面新增本地数据使用持久登记接口，登记后会留在「数据集管理 → 本地数据」，后续可直接进入探查，不需要重新填写路径。长期保留的是数据库登记和对原目录的引用，不是把原文件复制到浏览器或自动备份；仍须保管好本机原始文件。移除仅归档登记，重新导入公开目录也不会偷偷恢复已归档条目。

旧 `tds_` 临时提交接口继续保留原有 TTL、配额和所有者隔离。本次对识别到的一条此前本地临时数据集另建了长期登记：**只复制名称/简介等登记信息并重新验证同一目录，不复制、移动或删除原始数据**。原临时记录、过期时间、会话的原数据集 ID 和既有成果不改写。旧会话仍按原规则工作；未来分析从新的长期条目进入。这是本次显式补登记，不是后台自动迁移所有历史 `tds_`。

## 本批国内科学数据

ScienceDB、TPDC、NGDC/GWH 各准备 10 个不同数据集，ChemDC 已完成 2 个，共 32 个真实实体目录，存储在本机 `/Users/luchangfa/Documents/Codex/Data/open-catalog`。完整来源、许可证、实际格式、样本范围和统计见[国内科学数据目录](china-dataset-catalog.md)。

ChemDC 的催化剂性能数据（`cn-chemdc-0002039`，完整 PDF/DOCX）和室温有机自旋器件电光补偿数据（`cn-chemdc-0006995`，完整 6 XLSX + DOCX）已通过用户登录的官网流程取得，可从「化学数据中心」来源筛选后进入探查。前者是文档型数据，未预先转换成数值表；后者保留原始工作表及目录，须留意 `--` 占位符、单位差异和重复实验列。其余 8 个候选仍未完整取得，不登记、不计入这 32 个。CC 许可、公开目录和下载权限不能混为一谈；浏览器安全提示由用户自行处理，不关闭保护或用其他工具绕过。

本批实体留在宿主 Data 目录，通过只读 host-path 挂载供分析；仓库只保存来源与哈希清单。下面的 18 个国际开放样例仍沿用原来的随项目资源与 dataset-data 卷，彼此不替换。

## 随项目提供的 18 个数据集

9 个领域，每个 2 个；共 37 个文件，3,746,472 bytes（约 3.57 MiB）。均已实际下载，不需要首次点击时从外网取数据。模型问答仍需要配置好的模型服务；数据文件可本机离线读取。

| 系统领域 | 官方数据来源 |
| --- | --- |
| 通用数据分析 | [Iris 鸢尾花形态数据](https://archive.ics.uci.edu/dataset/53/iris)；[Wine 意大利葡萄酒成分数据](https://archive.ics.uci.edu/dataset/109/wine) |
| 表格与工作簿 | [Wine Quality 红白葡萄酒质量表](https://archive.ics.uci.edu/dataset/186/wine+quality)；[Concrete 混凝土抗压强度工作簿](https://archive.ics.uci.edu/dataset/165/concrete+compressive+strength) |
| 地学与遥感 | [Natural Earth 全球国家与地区边界](https://www.naturalearthdata.com/downloads/110m-cultural-vectors/110m-admin-0-countries/)；[NOAA 近地面气温月气候态](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html) |
| 图像数据 | [BBBC007 果蝇细胞双通道显微图像（2 视野样本）](https://bbbc.broadinstitute.org/BBBC007)；[BBBC039 细胞核图像与实例分割掩膜（官方预览对）](https://bbbc.broadinstitute.org/BBBC039) |
| 化学与分子结构 | [RCSB Crambin 小蛋白晶体结构（1CRN）](https://www.rcsb.org/structure/1CRN)；[RCSB 泛素蛋白晶体结构（1UBQ）](https://www.rcsb.org/structure/1UBQ) |
| 生物序列 | [NCBI λ 噬菌体完整参考基因组](https://www.ncbi.nlm.nih.gov/nuccore/NC_001416.1)；[NCBI 拟南芥叶绿体完整参考基因组](https://www.ncbi.nlm.nih.gov/nuccore/NC_000932.1) |
| 空间与天文 | [NASA 哈勃 WFPC2 天文成像样本](https://fits.gsfc.nasa.gov/fits_samples.html)；[NASA 哈勃 FOS 光谱误差数据样本](https://fits.gsfc.nasa.gov/fits_samples.html) |
| 文档证据 | [PLOS 可复现计算研究十条准则（开放论文 PDF）](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1003285)；[PLOS Jupyter 科研笔记本十条准则（开放论文 PDF）](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1007007) |
| 谱学与衍射 | [国际碳酸钙高分辨率 XRD 衍射谱样本](https://data.mendeley.com/datasets/kxd48sck74/1)；[CoCrCuFeNi 高熵合金工艺对比 XRD 样本](https://data.mendeley.com/datasets/x35xrkxpjb/1) |

这是一组轻量、真实的分析入门数据，不等同于下载各机构的完整数据库。完整条目、研究子集、官方教学样本及网页预览对均在卡片和简介中明确标注。

### 来源与许可

每个目录的 manifest.json 记录 publisher、source_url、license、license_url、sample_scope、原始下载 URL、大小和 SHA256；SOURCE.md 保留可读来源说明。

- UCI 数据使用其官方 CC BY 4.0 标注，保留作者署名；Iris/Wine 新增带表头 CSV，同时保留原始 .data，未改观测值。
- Natural Earth 为公共领域；NOAA 数据按其政府数据使用说明并保留来源致谢。
- BBBC 两项按各自 CC0 声明；BBBC007 是两视野四张原始 TIFF，BBBC039 为官方 PNG 图像/掩膜预览对，不冒充原始 16 位图像库。
- NCBI 两项是非个人的公开参考序列，按 NCBI 公共序列使用政策记录，不擅自标为 CC0，也不消除潜在第三方权利。
- PLOS 论文按原文 CC BY 声明；2013 年论文未擅自指定版本。文档样本不是底层实验数据。
- RCSB / NASA 按各自 CC0 数据政策保留来源；NASA FITS 是官方教学样本。
- 两项 Mendeley 研究是不同研究的 XRDML 子集，CC BY 4.0。碳酸钙上游 PowDLL 转换产生的时间、计数时间和 Kβ 占位信息不能视为真实实验条件。

## 接口与兼容边界

- GET /api/v1/datasets/manage：公开内置数据 + 有权管理的持久登记，支持 query、offset、limit、include_archived。
- POST /api/v1/datasets/registrations：管理员登记允许目录中的本机数据，持久 owner-scoped 记录。
- PATCH /api/v1/datasets/{id}：仅更新 name、description、domain。
- DELETE /api/v1/datasets/{id}：软归档 enabled=false，绝不删除目录或数据文件。
- 旧 POST /datasets/submissions 及 tds_ 临时记录继续保留原有 TTL、配额和所有者隔离，不自动迁移或删除。本次显式建立的长期副本是独立登记，不更改旧记录或会话绑定。
- 内置数据保存在 backend/app/resources/datasets，首次使用目录服务时校验并导入现有 dataset-data 卷。SHA256/大小/完整清单不符即拒绝导入；不提供任意 URL 下载接口。
- 本批外部目录清单位于 backend/app/resources/external-datasets，实体保存在宿主 Data/open-catalog。只有管理员命令可登记为带来源的公开精选；普通浏览器登记不能设置 curated、伪造来源或绕过 owner 隔离。
- 仅域内所有者或管理员可以管理登记；管理权限不扩大私有数据探查权限。
- 不更换 PlanActFlow、SSE、FastAPI 或 Docker 隔离。部署仍使用单一 Compose 项目和 ./run.sh；本次继续仅绑定本机 7001。

## 管理员导入已准备的公开实体

这是本机管理员操作，不是浏览器接口。所有容器命令继续使用单一 Compose 项目及 `./run.sh`，不建立第二套栈、不新增公网端口。

1. 先人工核实来源、许可证及下载权限。[本批公开下载描述](../backend/app/resources/external-descriptors)可供复核；经审阅的描述可交给 [prepare_external_datasets.py](../backend/scripts/prepare_external_datasets.py) 获取真实文件，保留原包，进行有界安全解压并生成清单。准备脚本不是运行时的任意 URL 下载服务；它拒绝未审阅的重定向、空/错误响应和已有文件覆盖，遇到站点变更需重新人工核实。受保护数据需由用户通过官网正常流程取得。
2. 每个清单必须声明完整文件集合、大小和 SHA-256；额外文件、缺失文件、路径穿越、符号链接或内容不符都不能以“尽量导入”放行。实体目录必须在 `DATASET_HOST_PATH_ALLOWLIST`（或本机执行节点的允许根）内，不将允许根扩大为 `/`。
3. 使用 [import_curated_host_datasets.py](../backend/scripts/import_curated_host_datasets.py) 做默认 dry-run。`--host-root` 是 Docker 宿主真实根；`--inspection-root` 必须是**同一根目录的精确只读 bind mount**，不能以拷贝目录、不同路径、读写挂载或其下覆盖挂载代替。脚本会核实调用容器的挂载身份，再检查内容；默认不插入数据、不创建/重配置执行节点。

下面只校验已准备的 TPDC 清单，不下载、不改源数据、不登记数据库：

```bash
./run.sh run --rm --no-deps \
  --entrypoint /opt/venv/bin/python \
  -v /Users/luchangfa/Documents/Codex/DataSeek:/workspace:ro \
  -v /Users/luchangfa/Documents/Codex/Data/open-catalog:/reviewed-data:ro \
  backend /workspace/backend/scripts/import_curated_host_datasets.py \
  --catalog /workspace/backend/app/resources/external-datasets \
  --host-root /Users/luchangfa/Documents/Codex/Data/open-catalog \
  --inspection-root /reviewed-data \
  --source tpdc
```

确认默认校验输出 `apply: false` 且所选清单全部通过后，在**同一命令末尾添加 `--apply`**才会建立登记。`--source` 可选 `scidb`、`tpdc`、`chemdc`、`ngdc`；省略时校验/登记当前目录中全部已准备的清单，并不自动下载缺失来源。`--apply` 会重新校验，不能依赖早先结果跳过变更检查。

已有相同清单的登记可幂等复用；身份、目录或清单冲突时拒绝覆盖。已有名称编辑和软归档状态保留。命令只读取实体、写受控登记，不移动文件、不清理原包，也不更改 AgentLoop、SSE、模型配置或旧会话。

新增**随项目样例**继续沿用 `backend/app/resources/datasets/*/manifest.json` 合约；原有目录测试固定校验 18 份、每领域 2 份。新增**宿主外部数据**使用 external-datasets 清单和独立导入检查，不将宿主实体搬进仓库。两者都必须保留许可、来源、明确范围并补充相应检查。
