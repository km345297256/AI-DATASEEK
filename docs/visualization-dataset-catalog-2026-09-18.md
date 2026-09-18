# 开放科学可视化样例（2026-09-18）

本批按当前 Cordis 可视化的 **10 个 `view_kind` 各补 1 套**，不是为每个具体插件分别配数据。当前源码有 83 份插件清单，10 套代表样例不意味着全部插件及格式均已覆盖。

## 在系统中使用

1. 打开本机 [数据集管理](http://localhost:7001/datasets)，搜索 **可视化样例**，应得到本批 10 套。
2. 点击 **进入探查**，点击数据文件右侧的预览图标。
3. 必要时在文件面板顶部 **选择可视化插件** 中选择下表建议的视图。每套均带 `SOURCE.md`，记录许可、范围、原件与派生文件说明。

| 类别 | 数据与官方来源 | 主预览文件 | 建议插件 |
| --- | --- | --- | --- |
| 图像 | [Light My Cells 犬细胞细胞核图像](https://www.ebi.ac.uk/biostudies/BioImages/studies/S-BIAD1047)，1200×1200 单通道 | `image_399_Nucleus.ome.tiff` | Viv（`viz-viv`） |
| 地图 | [USGS 2024 年全球 M≥6 地震](https://earthquake.usgs.gov/fdsnws/event/1/)，固定查询 99 个地震 | `usgs-2024-m6-wgs84-2d.geojson` | MapLibre / deck.gl（`viz-maplibre`） |
| 数值曲线 | [NOAA 全球甲烷月均值](https://gml.noaa.gov/ccgg/trends_ch4/)，515 条月记录 | `ch4_mm_gl.csv` | Plotly（`viz-plotly`） |
| 表格 | [Palmer 南极企鹅](https://github.com/allisonhorst/palmerpenguins)，344 行、8 列 | `penguins.csv` | 表格（`csv`） |
| 文本 | [NOAA Mauna Loa CO₂ 月均记录](https://gml.noaa.gov/ccgg/trends/data.html)，保留原始注释 | `co2_mm_mlo.txt` | 文本与代码（`text`） |
| 三维结构 | [RCSB 1MBN 肌红蛋白](https://www.rcsb.org/structure/1MBN)，1260 原子 | `1MBN.cif` | Mol*（`viz-molstar`） |
| 文档 | [科研软件稳健性开放论文](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1005412)，10 页 | `research-software-robustness.pdf` | PDF.js（`viz-pdfjs`） |
| 系统树 | [eLife SepH 蛋白系统树附件](https://elifesciences.org/articles/63387/figures)，720 节点、361 叶 | `seph_phyml_quoted.nwk` | 系统发育树（`viz-phylogeny`） |
| 视频 | [eLife FtsZ-YPet 荧光延时观测](https://elifesciences.org/articles/63387/figures)，47 帧、H.264 | `elife-63387-fig2-data1-v1.mp4` | 本地视频（`viz-video-player`） |
| 关系网络 | [Mangal 格陵兰植物—传粉者网络 908](https://mangal.io/api/v2/network?dataset_id=7&count=1000)，43 节点、63 边 | `lundgren-olesen-2005-network-908.graphml` | Cytoscape（`viz-scientific-graph`） |

实体位于本机 `/Users/luchangfa/Documents/Codex/Data/open-catalog/open-science/`，共 **29 个文件、4,235,966 字节（约 4.04 MiB）**，包含原件、必要的兼容副本和来源文档。宿主路径只用于运维，不传入浏览器、URL 或浏览器存储。

## 科学含义与显示限制

- **甲烷**：横轴选 `decimal`，纵轴勾选 `average` 或 `trend`，再点击“绘制”。单位为 ppb。不要把默认的 `year` 纵轴当作甲烷浓度。当前 Plotly 只读前 200 行窗口，完整 515 条记录仍保留在文件里，跨全时段分析需读完整文件。负的不确定度标记代表缺失，不能当作负标准差。
- **CO₂**：单位 ppm，原文件说明保留了早期 Scripps 来源、插值与停测期间替代站点，不能把所有记录当作同一站点未经处理的原始采样。
- **地图**：离线空白底图是当前插件设计；可缩小、平移查看全球点位，不连接在线底图。USGS 第三坐标是深度 km，不是高度 m，因此兼容副本将其原值保留到 `depth_km`，经纬度和全部要素不变，原始 GeoJSON 同时保留。
- **显微图像**：仅一张完整已发布的犬细胞图像，不是整个实验库。显示颜色和自动对比度不代表样品固有颜色或修改了原始强度。
- **系统树**：官方论文描述 360 条代表序列，但附件实测为 361 叶，以原文件为准，不擅自删叶。兼容副本只给一个含方括号的完整叶标签加 Newick 引号，标签字符、拓扑和枝长不变。
- **视频**：作者已对菌丝选择、拉直；本次原样下载，未转码。11.75 秒是播放时长，不等于实验生物学时间。定量速率还需论文提供的时空标定。
- **生态网络**：平台采用 CC0 默认数据政策（原存储库另有许可时从其），本条未标注其他覆盖许可；该事实如实记录，并保留原文引用。未记录的边不自动等于关系不存在，有向数据库编码也不是因果方向。
- **PDF** 是文献型样例，不冒充实验测量数据。

企鹅 CSV 原值与 19 个 `NA` 标记原样保留；表格技能的来源与缺失值处理规范只用于校验，没有将原文件改成新工作簿。候选核磁谱因坐标声明歧义被排除，未进入正式目录。

## 兼容与复现

没有修改 AgentLoop / PlanActFlow、SSE、FastAPI 接口、可视化插件实现、模型配置或插件开关。实体保持只读挂载，导入继续经过 `DATASET_HOST_PATH_ALLOWLIST`、精确只读 Docker bind、全清单大小和 SHA-256 验证。只为运维导入器增加明确的 `open-science` 来源白名单，不开放任意来源登记权限。

新增资源：

- `backend/app/resources/visualization-descriptors/open-science.json`：人工审查的公开来源、版本、许可、文件散列和派生关系。
- `backend/app/resources/visualization-datasets/open-science/*.json`：10 份严格数据集清单。
- `backend/scripts/prepare_visualization_datasets.py`：离线验证已下载缓存，默认不写；`--apply` 仅新建目录，不覆盖已存在数据。
- `backend/scripts/reproduce-map-graph.mjs`：地图与网络格式转换的确定性复现，默认只读比对；显式 `--write` 只创建缺失输出。
- `backend/scripts/reproduce-science-samples.py`：甲烷 CSV 去注释和 Newick 引号规范化的确定性复现，默认只读比对。

准备工具不访问网络。复现时按 descriptor 的官方 `url` 下载到暂存根下对应的 `viz-*` 目录，并验证固定大小及 SHA-256；派生文件按上述脚本生成。上游滚动数据发生改变时应人工审查为新版本，不能跳过散列检查。暂存目录准备齐全后：

```bash
backend/.venv/bin/python backend/scripts/prepare_visualization_datasets.py \
  --descriptors backend/app/resources/visualization-descriptors/open-science.json \
  --staging-root /absolute/reviewed-staging \
  --data-root /Users/luchangfa/Documents/Codex/Data/open-catalog \
  --catalog-root /Users/luchangfa/Documents/Codex/DataSeek/backend/app/resources/visualization-datasets \
  --download-date 2026-09-18
```

默认只验证；确认后才添加 `--apply`。已有目录和清单必须完全一致才幂等复用，用户编辑、残缺或冲突文件不会被覆盖。

登记也先 dry-run，再对同一命令添加 `--apply`：

```bash
./run.sh run --rm --no-deps --entrypoint /opt/venv/bin/python \
  -v /Users/luchangfa/Documents/Codex/DataSeek:/workspace:ro \
  -v /Users/luchangfa/Documents/Codex/Data/open-catalog:/reviewed-data:ro \
  backend /workspace/backend/scripts/import_curated_host_datasets.py \
  --catalog /workspace/backend/app/resources/visualization-datasets \
  --host-root /Users/luchangfa/Documents/Codex/Data/open-catalog \
  --inspection-root /reviewed-data --source open-science
```

没有重启服务、构建第二套部署或公开新端口。验收中的一次性容器不启动后端服务、不发布端口，退出即移除。

## 验收

本批已完成下载、只读验证和入库，数据集总数由 51 增至 61。

| 检查 | 结果 |
| --- | --- |
| 原有数据与设置 | 原有 51 个数据集的公开记录摘要一致；83 个可视化插件的启用偏好未变 |
| 文件完整性 | 29 个文件的清单、大小及 SHA-256 全部通过，只读挂载验证通过 |
| 接口预览 | 10/10 通过；同一文件重复预览复用文件标识 |
| 实际浏览器预览 | 10/10 通过；包括显微图像、三维结构、系统树、关系网络及视频实际播放，无页面异常 |
| 前端 | `npm run type-check`、`npm run build` 通过 |
| 后端全量回归 | 6653 passed、31 skipped、190 subtests passed；未启用需外部服务的 live integration 测试 |
| 最终目录与兼容性专项回归 | 174 passed，包含新目录、离线准备、旧目录导入及路径边界相关测试 |
| Compose 配置 | `./run.sh config --quiet` 通过 |
| Sandbox 全量测试 | 未运行：现有环境缺少 pytest 等测试依赖，临时安装未获批准；未安装依赖或修改 Sandbox 实现 |

浏览器验收使用独立无登录环境，仅允许本机读取及文件预览接口，拦截模型推荐问题并提供明确标注的验收占位内容；没有创建分析会话或消耗模型调用。截图中的占位推荐问题不代表模型分析结果。本轮验证覆盖文件预览及导入兼容性，不声称覆盖所有 83 个插件或完成模型分析正确性、全量 Sandbox 回归及性能基准测试。

本机验收日志、接口结果和预览截图保存在工作区 `.cache/visualization-datasets-20260918/`，包含 `backend-pytest.log`、`catalog-regression.log`、`api-check.json`、`browser-check.json` 和 `screenshots/`。该目录是未纳入版本管理的验收缓存，正式来源与完整性记录以资源清单及各数据集的 `SOURCE.md` 为准。
