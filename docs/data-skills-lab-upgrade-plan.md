# DataSeek 科学数据实验室升级清单

日期：2026-10-13。状态：设计与代码差距分析；LAB-01 首批基础代码已实现，见下方实施记录。本文不代表所述能力已经上线。

实施记录（2026-10-13）：已增加 skill.yaml v1 解析与校验、旧技能兼容、发布版本和内容摘要、版本化包原地编辑保护、聊天提交摘要绑定、运行期 Skill 资源快照，以及精确工具依赖激活检查。[包规范与实际边界](data-skill-package-v1.md)说明使用方法。独立发布仓库、验证记录、草稿编辑、多实例发布事务和 Run/Step 调度仍未完成，LAB-01 不标记为全部完成。

本批验证：后端 5,306 通过、32 跳过、190 子测试通过；排除工作区原有 test_admin_skill_delete.py（含导入时运行的数据库操作）。前端类型检查、生产构建及 2,652 项测试通过，Compose 配置检查通过。沙箱测试在收集阶段遇到缺失 pymupdf、python-pptx 和受控 Linux GRIB 环境，尚未通过。本批代码已提交为 bf79123，并推送至两个远程仓库的 datalab 分支。已通过 ./run.sh 构建并更新本地前后端容器；http://localhost:7000/、数据集与技能目录接口均返回 HTTP 200。上述检查确认服务可用，不代表后续 LAB-02 至 LAB-11 已完成。

产品基线：以对话为入口、以 Data Skills 为核心，支持数据发现、获取、导入、理解、处理、分析和成果复用。用户可以从任何阶段进入；技能与工具可以手动选择，也可以由系统推荐。研究方法通过技能仓库积累。

本次只检查代码和官方数据源文档，未执行新数据源的端到端下载或分析。现有工具的清单是复用候选，不代表已通过新场景验证。工作区已有聊天状态及测试修改，本次不覆盖这些修改。

## 1. 现有实现与差距

| 能力 | 检查依据 | 已有基础 | 需要补齐 |
| --- | --- | --- | --- |
| 技能定义 | [Skill](../backend/app/domain/models/skill.py)、[Registry](../backend/app/domain/services/skills/registry.py) | SKILL.md、metadata.json、脚本/参考文件、作用域 | 稳定包标识、发布版本、分类、输入输出、依赖、验证记录；文件加载目前未读取 skill.yaml |
| 技能发现与安装 | [路由](../backend/app/interfaces/api/skill_routes.py)、[选择器](../backend/app/domain/services/skills/selector.py) | 目录、上传、安装、偏好、关键词选择；选择器默认最多三个 | 版本锁定、兼容性过滤、显式选择优先、依赖解析和可解释推荐 |
| 会话转技能 | [生成器](../backend/app/domain/services/skills/session_skill_creator.py) | 提取目标、计划、工具参数和 shell 命令 | 参数化、私密内容剔除、数据引用替换、跨数据验证、草稿发布流程；不能直接将命令回放视为可复用方法 |
| 网络检索 | [SearchToolkit](../backend/app/domain/services/tools/search.py)、[结果模型](../backend/app/domain/models/search.py) | 通用网页检索，title/link/snippet 结果 | 科学数据查询条件、标识符、版本、许可、访问能力、候选合并与筛选解释 |
| 数据登记 | [数据模型](../backend/app/domain/models/dataset.py)、[数据接口](../backend/app/interfaces/api/dataset_routes.py) | 本地登记、上传/宿主挂载、目录筛选和预览，metadata 可保存来源 | 远程候选与实际资产分离、不可变数据版本、结构化获取记录、多输入衍生关系 |
| 数据获取 | [准备脚本](../backend/scripts/prepare_external_datasets.py)、[登记服务](../backend/app/application/services/data_center_dataset_service.py) | 特定公开数据准备脚本、已有文件登记 | 面向用户请求的连接器、获取计划、可恢复传输、配额、校验后登记；准备脚本不等同于通用下载服务 |
| 执行与恢复 | [AnalysisJob](../backend/app/domain/models/analysis_job.py)、[说明](analysis-jobs.md) | 持久化状态、版本比较更新、取消、租约、超时和 interrupted | 现有执行仍由 Agent 等待，不是独立工作队列；需要支持长下载、检查点、可恢复获取及多次 attempt |
| 对话与实验 | [Session](../backend/app/domain/models/session.py)、[PlanActFlow](../backend/app/domain/services/flows/plan_act.py) | 会话、计划、步骤、工具调用和 DoneEvent | Experiment/Run 的持久身份；DoneEvent 表示当前消息处理结束，不应作为未来实验成功的唯一依据 |
| 事件与复现 | [事件说明](session-events-and-replay.md) | 事件序号、断线回放、环境快照与目录版本 | run/step/attempt 关联、技能发布包锁定、输入数据版本和结果校验；事件回放不等于重新执行 |
| 成果 | [DataProduct](../backend/app/domain/models/data_product.py)、数据接口 | 与数据集和来源会话关联的产物，已有 version 字段 | 显式多输入血缘、运行参数引用、验证结论；原有 version 字段不能直接当作原始数据快照 |
| 对话交互 | [ChatBox](../frontend/src/components/ChatBox.vue)、[ChatPage](../frontend/src/pages/ChatPage.vue)、[完成提示](../frontend/src/components/AnalysisOutcomeNotice.vue) | 技能选择、工具卡片、作业状态、成果预览 | 候选数据卡片、获取进度、实验侧栏、按作用域展示完成状态 |

当前完成提示仍通过 hasRunningStep 和“是否为最新助手消息”等前端条件调整文案。该方式不能完整表达历史阶段、并行任务和后续运行的状态，需要后端输出明确的完成作用域。

## 2. 实施边界与对象约定

继续使用现有 Vue、FastAPI、Mongo、Redis、Cordis 和沙箱。先增加内部模块与工作进程，不先拆分大量服务。升级仍属于科学数据探索与分析，不扩展为知识库、网站发布或通用平台管理。

| 对象 | 第一版必须记录的内容 |
| --- | --- |
| Experiment | 目标、所有者、会话引用、数据引用、运行列表；用户归档实验与一次运行完成是不同操作 |
| Run | 明确的本次目标、计划版本、状态、固定技能/工具版本、输入版本、交付要求、起止时间 |
| Step | run_id、依赖步骤、所用技能、输入/输出引用、状态、验证要求 |
| Job / Attempt | step_id、操作类型、尝试次数、租约、进度、检查点、终态、错误类别 |
| DatasetCandidate | 数据源、稳定外部标识、可选版本、来源页、覆盖范围、访问状态、检索时间、元数据证据 |
| DatasetAsset / Version | 可访问的数据及其不可变版本；文件清单、内容校验值、结构摘要、来源和获取记录 |
| Artifact | 所属运行、文件/图表引用、输入版本、生成步骤、验证记录；可登记为后续分析的数据版本 |
| SkillRelease / Installation | 包标识、版本、内容摘要、依赖、验证记录；安装记录与发布包分离 |

数据状态不得混用：候选已发现不等于已下载；传输完成不等于校验通过；校验通过并登记后才可用。访问权限未知、需登录、不可下载和暂时失败要分别表达。

原始数据只读。新下载写入受控暂存区，校验后原子登记；转换结果产生新版本。宿主路径继续经过 DATASET_HOST_PATH_ALLOWLIST 校验，浏览器只获得逻辑引用，不获得真实路径。来源 URL 展示时剔除临时凭据；密钥由既有凭据边界处理。

同一数据源标识可能有多个版本，同一 DOI 也可能关联多个分发文件。候选合并保留来源证据，不仅按标题去重。不明版本标注 unknown，并记录实际文件哈希；可变远程引用未经快照不能宣称完全可复现。

## 3. Data Skill 包与仓库

发布包建议包含 SKILL.md、skill.yaml、examples/、tests/，按需包含 scripts/、references/、templates/。SKILL.md 负责方法解释，skill.yaml 是发布元数据的唯一权威来源；本地安装、所有者和偏好不写回公共包。

第一版 manifest 约定：schema_version、命名空间包标识、SemVer version、作者/许可证、一个主分类、多领域标签、输入输出 JSON Schema、语义约束、工具依赖、运行环境引用、网络/资源需求、验证方法。输入采用数据版本 ID 和逻辑文件引用；不得把本机路径或凭据硬编码进技能。

科学语义约束至少允许描述变量、单位、时间轴、坐标参考、维度和样本结构。参数缺省值必须有明确依据。缺失的必要信息进入等待输入或质量问题状态，不自动编造。

同一发布版本不可覆盖；修订生成新版本及内容摘要。Run 锁定实际安装版本、工具目录和环境快照。升级只影响新运行，历史运行保持原引用。撤回发布阻止新的安装，不抹去历史证据。

旧技能通过 legacy 适配器继续加载，标注“未声明能力约定/未验证”。已有 metadata.json 保留本地所有权信息。旧草稿编辑行为继续可用；编辑已发布包生成新草稿。验证标记来自受控验证记录，包作者自填 verified 不生效。

推荐先做规则过滤，再做语义排序：用户指定 > 输入兼容性与工具可用性 > 任务匹配。选择数量由具体步骤需要决定，不简单把默认上限从 3 改成更大的数。只有参与当前步骤的技能才加载完整上下文。

仓库目标是独立 Git 仓库与版本化发布包。第一阶段在当前仓库内用候选目录和本地索引验证协议，不在本次审查中创建或发布远端仓库。后续抽取时保持包 ID、发布摘要与校验方式不变。扩展者通过模板、校验命令、离线样例和 PR 贡献；同一协议支持官方、组织和个人技能。

会话沉淀流程：选择运行 → 提炼目标和参数 → 替换数据引用 → 清理私人上下文 → 生成草稿 → 第二份数据验证 → 人工审阅科学方法 → 发布。自动生成的内容不能自动获得“已验证”资格。

## 4. 优先级与可独立验收的改造项

P0 是首条闭环必需；P1 是第二条闭环及可复用仓库必需；P2 是首版之后的扩展。

| 编号 | 优先级 | 实施入口及交付 | 依赖 | 验收 |
| --- | --- | --- | --- | --- |
| LAB-01 | P0 | Skill 模型、Registry、路由：发布包 schema、legacy 适配、安装锁定 | 无 | 旧技能可用；新包非法字段/依赖不通过；运行中的版本不会热替换 |
| LAB-02 | P0 | Session/PlanActFlow/事件：增加 Run/Step 身份和明确的终态归属 | 无 | 阶段完成不能结束 Run；失败不显示成功；同会话再次运行不会修改旧记录 |
| LAB-03 | P0 | 数据模型与登记服务：Candidate、Version、AcquisitionRecord 和来源关系 | 无 | 搜索结果不会伪装已导入；同数据新版本不覆盖旧版；多输入来源可追溯 |
| LAB-04 | P0 | 新增 source connector 边界及 NCBI/NOAA 适配器 | LAB-03 | search/describe/plan_acquisition 的返回可验证；分页、限流、访问失败有明确结果 |
| LAB-05 | P0 | AnalysisJob 周边增加获取 worker、持久化队列/领取、检查点、attempt | LAB-02/03/04 | Agent 等待结束不丢下载；后端重启可识别并恢复安全任务；取消无重复登记 |
| LAB-06 | P0 | ChatPage/ChatMessage/AnalysisOutcomeNotice：按 Run/Step 投影状态；候选与获取卡片 | LAB-02/03/05 | 刷新、断线重连、迟到事件不会逆转终态；重复思考提示回归通过 |
| LAB-07 | P0 | 发布第一条 NCBI 场景所需技能，包装现有 sequence/table 工具 | LAB-01/04/05/06 | 从问题到真实数据、校验、CSV、图表和方法记录完整通过 |
| LAB-08 | P1 | NetCDF 技能链与 NOAA 场景，扩充实验侧栏及复用数据操作 | LAB-07 | 第二种数据结构通过；更换区域/数据无需修改核心 Agent |
| LAB-09 | P1 | 技能目录：阶段/领域筛选、输入兼容性、验证信息、固定版本选择 | LAB-01/07/08 | 不适用的技能有理由；用户选择得到执行或明确不兼容解释 |
| LAB-10 | P1 | 贡献模板、验证工具、版本发布流程、会话转草稿 | LAB-07/08/09 | 新增一个技能不改聊天核心；样例失败阻止标记为已验证 |
| LAB-11 | P2 | Google Dataset Search 辅助发现、多来源联合检索、SRA 大数据流程 | 基础闭环稳定 | 接入能力和失败模式经过各来源验证，不把网页入口当成稳定 API |

LAB-01/02/03 可分别开发，但跨模块集成必须以共同字段约定为准。优先交付顺序：基础约定 → NCBI 检索获取及分析闭环 → NOAA 闭环 → 仓库贡献与复用。前端先做必要的内联卡片，完整目录和实验侧栏随 P1 补齐。

获取 worker 首版可复用现有存储设施，用数据库原子领取和租约持有执行权。只有完成持久化登记后才确认消息/任务完成；缓存或通知丢失可从持久状态重建。它是新增能力，不是把当前 asyncio Task 改名为队列。

传输恢复只在源支持且对象版本未变时续传：校验 ETag/Last-Modified/长度或来源版本；缺少可靠校验则重下暂存文件。重试创建新 attempt，终态 attempt 不重开。登记需要幂等键和原子提交；不承诺网络副作用 exactly-once。非幂等的分析操作中断后先核对产物，再决定是否重跑。

Run 成功条件：当前计划的必要步骤成功、必要产物已登记且验证通过、无未完成的必要任务。允许明确记录已跳过的可选步骤。等待输入、失败、取消、部分交付分别展示。DoneEvent 可继续结束消息流，不能覆盖 Run 状态。所有新事件携带 run_id/step_id/attempt_id 和单调 revision；多 worker 发布复用或补齐每会话有序发布机制。

## 5. 首批技能候选

以下是拟开发清单。名字是建议的稳定包标识；工具复用必须经过输入/输出与科学语义适配。

| 技能 ID | 分类 | 输入 → 输出 | 实现来源 |
| --- | --- | --- | --- |
| ncbi-genome-discovery | 检索 | 物种与组装条件 → 候选及筛选证据 | 新连接器；NCBI Datasets |
| noaa-temperature-discovery | 检索 | 变量、时间范围、区域 → 有覆盖证据的候选 | 新连接器；初版限定 NOAA PSL 温度目录 |
| dataset-candidate-assess | 检索 | 候选与研究目标 → 适用性、排除理由、未知项 | 通用技能；不输出无依据的质量分 |
| ncbi-genome-acquire | 获取 | 固定 accession.version、文件选项 → 元数据/FASTA 版本 | 新连接器与获取 worker |
| dataset-http-acquire | 获取 | 经连接器解析的分发引用 → 校验文件和来源记录 | 新下载工具；允许公开 HTTPS 来源，重定向逐跳检查 |
| dataset-import-verify | 理解与质检 | 获取结果或本地输入 → 可用资产、文件清单和质量问题 | 现有登记/校验 + 新版本登记 |
| genome-basic-profile | 分析 | FASTA 版本 → 长度、有效碱基、模糊碱基、GC 统计 | sequence_inspect/validate/stats 等工具，需验证计算口径 |
| netcdf-quality-profile | 理解与质检 | NetCDF 版本 → 维度、单位、时间/坐标、缺失值报告 | data_format_inspect、cf_semantics_validate、spatial_grid_diagnose |
| netcdf-region-extract | 整理与转换 | 数据、变量、区域 → 子集版本与提取记录 | scientific_subset、hierarchical_array_extract 等适配 |
| netcdf-series-export | 整理与转换 | 子集、维度选择、单位策略 → CSV 与元数据说明 | 现有提取/单位转换工具组合，显式防止无界展开 |
| regional-temperature-cycle | 分析 | 月气候态、区域、权重 → 12 个月区域季节循环 | scientific_netcdf_area_weighted 等适配，不计算长期趋势 |
| scientific-results-package | 成果整理 | Run 的已验证产物和方法 → 表、图、代码/参数、来源说明 | 现有产物机制 + 新血缘记录 |

前六项不必全部新建独立 Python 工具：技能负责方法和决策，连接器及共用下载/校验服务负责确定性执行。候选筛选可给出建议，实际 ID、大小和可用性必须来自数据源或检查结果。

未来浏览器访问和第三方技能执行继续沿用执行权限边界；外部数据页内容不能被当作平台指令。下载任务需要检查目标地址、重定向、解压路径和体积限制，这些检查进入获取服务而不是依赖技能提示词。

## 6. 首批两个验证场景

### A. NCBI：公开细菌基因组的基础特征比较

用户指令示例：“检索 Bacillus 属公开的完整基因组，筛选最多 5 个不同物种的 RefSeq 组装，比较长度和 GC 含量，并保留数据来源。”

使用 NCBI Datasets 的物种和组装过滤能力，返回候选与筛选理由；明确“RefSeq 来源”和“reference genome 标记”是不同条件。如果用户要求 reference，按该条件筛选，不足 5 个时展示真实数量，不放宽条件凑数。候选按稳定规则排序，并在实际运行时记录查询和 accession.version。

先获取元数据；为验证真实序列获取与分析，再获取最多 5 个选定组装的 genomic FASTA。第一版仅处理公开非人类参考/组装序列，不包含 SRA 原始测序流程。下载前估算规模，默认总量预算建议 100 MiB，未知大小按实际传输上限停止；该值是待实现的产品默认值。

产物：候选与选中清单、原始元数据、FASTA 版本、genome_summary.csv、比较图、方法说明和来源记录。数据不足以支持相关性结论时仅做描述性比较；不将探索图解读为因果证据。

验收：accession 与版本一一对应；FASTA 可解析；长度和 GC 与独立确定性校验器一致。GC 明确采用 (G+C)/(A+C+G+T)，单独报告模糊碱基；元数据口径不同则解释差异。固定样例精确回归，在线检索仅检验来源/契约和不变量，不要求未来一直返回相同 5 条记录。

复用：换一组符合条件的 accession，无需编辑技能代码。异常覆盖分页、429、找不到标识符、残缺下载、源更新、用户取消和重新执行。

### B. NOAA：月气候态的区域季节循环

用户指令示例：“查找 NOAA PSL 的近地面月气候态气温数据，提取北纬 30–40 度、东经 110–120 度的区域，分析一年内的季节变化，导出 CSV 和图。”

首版候选固定覆盖已有样例对应的 NCEP/NCAR sigma 0.995 层 1991–2020 月气候态数据：air.sig995.mon.ltm.1991-2020.nc。来源目录用于重新发现/获取；现有仓库样例只用于离线回归。这个有限目录检索不能被宣传为整个 NOAA 平台全文检索。

运行时验证来源元数据、实际时间轴、变量、单位与垂直层含义。sigma 0.995 不能直接称为 2 米气温。该文件是 12 个月气候态，不能用来回答“1991–2020 年气温是否上升”；长期趋势需要另外取得逐年月时间序列，列为后续场景。

产物：来源记录、不可变原始版本、区域子集、12 行区域月均温 CSV、季节循环图、方法说明。规则经纬网采用明确面积权重；缺失值格点逐月重新归一权重。K 转摄氏度仅在单位确认后进行；若本来为摄氏度则不再转换。

验收：经度范围和纬度方向正确；区域有有效格点；月序/单位明确；原始文件未改动；加权均值与独立小样例在声明容差内一致。CSV 保留单位及覆盖说明，空区域和全缺失数据返回质量问题。

复用：仅改变区域再次运行，复用原始版本，产生新的 Run、子集和图表。联网获取与本地样例两条路径分别验收，不能用预装样例冒充联网下载成功。

## 7. 迁移、测试与发布门槛

采用新增集合/字段和兼容投影，先保持旧会话、旧数据目录和旧技能能打开。历史记录缺少版本或校验信息时标注 unknown/legacy，不反向捏造“已验证”状态。首版可将新会话与一个实验关联，旧会话按需建立关联，不从历史对话猜测多个实验。

当前工作区的完成提示改动保持原样，实施 LAB-02/06 时统一收敛到后端状态投影，避免新旧两套判定长期并存。新能力使用开关逐步启用；关闭新建能力后仍可读取历史 Run 和产物，不删除数据。

| 测试层次 | 要证明什么 |
| --- | --- |
| 模型与包校验 | legacy 兼容、依赖约束、发布不可覆盖、非法包不安装 |
| 执行状态 | 并行子任务、阶段完成、等待输入、取消、失败、重复及迟到事件均不误报 Run 成功 |
| 获取恢复 | 中断续传、对象变化、校验失败、磁盘预算、原子登记与幂等重试 |
| 数据与方法 | 单位、坐标、维度、缺失值、GC 分母、加权均值有独立预期值 |
| 隔离与路径 | 延续宿主路径 allowlist、只读挂载、所有权校验；浏览器和存储化 URL 不泄露真实路径 |
| UI 与回放 | 刷新后选择与状态一致；技能不兼容有原因；完整运行结束前仅展示阶段完成 |
| 联网验收 | 官方来源实际可检索和获取；时间/版本被记录；外部不可用明确为阻塞而非空数据成功 |

沿用并扩展 test_skill_integration、test_skill_scope_management、test_analysis_job_service、test_analysis_job_integration、test_event_recording_replay、test_dataset_mount_validator、test_dataset_browser_path_privacy 等既有测试。离线确定性用例是 CI 基础，联网用例独立执行并记录日期和来源；不因远端故障修改预期值。

代码实施完成后执行仓库要求：frontend 的 npm run type-check、npm run build；backend 与 sandbox 的 uv run pytest；docker compose config --quiet。涉及前端状态的修改还运行 frontend 的 npm test。实际容器更新通过 ./run.sh。本文为文档新增，不宣称已执行这些代码测试或已完成部署。

第一阶段的完成标准是 LAB-01 至 LAB-07 加场景 A 实际通过；可称为首版 Data Skills 实验室的标准是再完成 LAB-08 至 LAB-10 和场景 B，并由另一份数据证明技能可复用。Google 辅助检索、SRA、大规模多来源检索进入后续版本，不阻塞前两条闭环。

## 8. 官方来源与尚待验证的事实

### 执行状态修正记录（2026-10-13）

LAB-02/06 的首批修正明确了已有成果检查契约的作用范围：AnalysisOutcome 固定为 step，单个阶段成功不能证明整个任务完成。前端实时消息与历史回放统一显示“阶段已完成”；旧消息没有范围字段时仅显示“成果检查通过”，不追认整体任务成功。服务端兜底成功文案也改为“本阶段分析已完成”。

响应结束事件不再把仍在运行的步骤补写为成功，而是停止该步骤的加载指示并显示“执行结果尚未确认”；之后收到明确的步骤终态时才更新结果。阶段成果不会关闭后续阶段的进度提示。失败计划和空计划不显示“任务已完成”，聊天页已有步骤加载指示时不重复显示底部“思考中”。这些修正覆盖聊天、数据集分析和分享页的响应结束处理，以及历史回放。

验证：后端 5,308 通过、32 跳过、190 子测试通过；继续排除含导入时数据库操作的原有 test_admin_skill_delete.py。前端 2,662 项测试、类型检查和生产构建通过，Compose 配置检查通过。沙箱仍因本机缺少 PyMuPDF、python-pptx 及受控 Linux GRIB 环境在收集阶段失败，本批没有修改沙箱。

边界：本批未建立独立的持久化 Run/Attempt 模型，也未实现跨运行事件归属和恢复调度；LAB-02/06 尚未全部完成。unknown 是前端对缺失步骤终态的保守展示，不是新建的后端任务终态。

- [NCBI 获取基因组元数据](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/get-genome-metadata/)：支持按物种、accession 和组装属性检索；用于场景 A 的入口选择。
- [NCBI 获取基因组数据包](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/download-genome/)：用于场景 A 的获取适配设计。
- [NCBI Datasets FAQ](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/troubleshooting/faq/)：Datasets 不提供 SRA 获取，SRA 需要单独工具链。
- [NOAA PSL 数据源页](https://psl.noaa.gov/data/gridded/data.ncep.reanalysis.html)：来源与致谢依据。具体文件选择另有[仓库样例清单](../backend/app/resources/datasets/open-noaa-air-climatology/manifest.json)佐证；实际远程文件可用性、大小和元数据在实施预检时验证。
- [Google Dataset Search 帮助](https://datasetsearch.research.google.com/help)：支持数据发现；本轮设计未找到可直接依赖的公开检索 API 文档，不据此断言永远不存在 API。

未决实施事项：两个来源的实时连接测试与字段映射、环境依赖版本、预计获取规模、独立技能仓库的实际托管位置。它们有明确的验证步骤，不影响本次代码差距结论；不得将未验证接入写成已支持功能。
