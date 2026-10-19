# DataSeek 数据连接器标准 v1

2026-10-14。产品决策：连接器是横向扩展数据访问方式的能力单元；数据来源与文件版本是调用后的追溯记录，不能替代连接器。聊天是使用入口，插件目录是配置和发现入口。

## 参考与取舍

- [OpenAI：Building MCP servers](https://developers.openai.com/api/docs/mcp) 为 ChatGPT 检索场景描述 search / fetch 工具及可引用的 id、title、url 结果。借鉴结构化工具契约与来源引用，不把文档检索协议直接当作大文件下载协议。
- [Manus：Connectors](https://www.manus.im/features/connectors) 区分应用连接、Custom MCP 和 Custom API；连接器提供外部访问，Skills 提供可复用任务方法。
- [Manus API：Connectors](https://open.manus.im/docs/v2/connectors) 将账户连接与任务使用分开，任务传入已连接项的 ID。DataSeek 后续账户连接采用同样的定义 / 连接实例 / 本次任务选择分层。

以上是公开功能与协议文档，不代表我们掌握两家产品内部实现。以下字段、调用方式与目录布局是 DataSeek 自身的设计。

## 用户如何使用

1. 在“插件 → 数据连接器”发现可访问的数据源和实际能力。公开来源无需登录；私有来源在接入账户后才能用于任务。
2. 在新任务或已有对话的输入框点击“数据连接器”，勾选来源并点“完成”；所选来源显示为可移除标签。随后在对话中提出需求，例如“从某数据源查找某类数据，读取元数据”。系统发现连接器，按其参数声明调用。
3. 检索返回数据资源，用户可核对来源；提供文件获取能力时，再将所选资源导入本次任务。
4. 导入完成后，用 Skill 指导转换、质检、分析，用工具执行。任务进度统一呈现过程与结果。
5. 来源和文件版本进入任务文件详情，执行记录进入任务进度详情；摘要、内部 ID 不占据聊天主界面。

公开 NCBI 检索、元数据读取以及有界 ZIP 文件获取已接入。账户连接、资源选择卡片、大文件异步获取仍待实现；元数据成功仍不能表示文件已获取。

## 四个对象的职责

| 对象 | 职责 | 示例 |
| --- | --- | --- |
| ConnectorDefinition | 提供方、版本、能力、参数与结果契约、访问方式 | NCBI 元数据、公共统计 API、对象存储、数据库适配器 |
| ConnectionInstance（后续） | 某用户对某数据源的账户、授权范围和凭据引用 | 某个只读数据库连接、某个存储桶账户 |
| Skill | 如何处理数据的流程、知识与脚本 | 表格质检、气候数据转换 |
| MCP / HTTP / native adapter | 把连接器操作接到实际接口的方式 | MCP tool mapping、固定 API 适配器 |

Skill 可以调用连接器，不携带账户密钥。MCP 服务也可以提供非数据工具，因此现有 MCP 目录保留；只有完成数据契约映射的操作才作为数据连接器能力展示。连接器不绑定生物、气象等某个领域。

## 可执行声明与调用

仓库目录 `data-connectors/` 保存 JSON Schema 与实例。Python 模型 `ConnectorManifest` 是当前 schema 的来源，回归测试防止已发布 JSON 与运行时代码漂移。

声明包括：`schema_version`、稳定 `id`、`version`、`title`、`description`、`transport`、`auth`、`domains`、`operations`、`limitations`。每个操作声明名称、说明、输入 / 输出 JSON Schema、效果与超时。仅列出已经实现的操作，不用一个通用 enabled 标志假装所有能力都可用。

当前接口：

- `GET /api/v1/data-connectors`：返回服务端已注册的公开声明，不返回凭据。
- `POST /api/v1/sessions/{session_id}/data-connectors/{connector_id}/call`：由服务端检查会话归属，再调用指定能力。
- Agent 工具 `data_connector_list` / `data_connector_call`：经过既有工具执行、超时、取消与网络效果控制，用户和会话由宿主注入，不允许模型指定。

示例：

```json
{
  "operation": "search",
  "arguments": {"taxon": "1386", "page_size": 2}
}
```

发送到已注册的 `ncbi-datasets-v2`。结果外壳固定为：

```json
{
  "connector_id": "ncbi-datasets-v2",
  "connector_version": "1.0.0",
  "operation": "search",
  "kind": "metadata",
  "resources": [],
  "next_cursor": "",
  "total_count": 0,
  "files_downloaded": false
}
```

资源包含 `id`、`external_id`、`title`、`source_url`、`retrieved_at`、结构化 `metadata`，已保存来源记录时附 `candidate_id`。空 resources 只代表一次成功的空结果；失败返回错误，不能降级为空成功。NCBI 的 `next_cursor` 作为下一次 `page_token`，保留原筛选条件。

## 能力分类和扩展约束

| 能力 | 作用 | 本轮支持 |
| --- | --- | --- |
| search | 检索资源及翻页 | NCBI 已实现 |
| describe | 读取确切资源版本的元数据 | NCBI 已实现 |
| acquire | 将资源获取为受管理的任务文件 | NCBI 单个版本 genomic FASTA ZIP，最多 32 MiB |
| query | 对结构化数据源做受限只读查询 | 后续扩展 |

不强制每种来源都支持搜索：例如已知对象链接可以只提供 describe / acquire。今后 acquire 返回独立 acquisition job 与文件回执，不复用 metadata 结果伪装成功；大文件异步下载必须处理取消、配额、重试和内容校验。

当前 v1 增加明确的 files 结果模型，NCBI 声明升级为 1.1.0。files 结果必须包含已保存文件的 ID、名称、字节数、来源链接、本地 SHA-256，不能在 metadata 中塞本地路径绕过文件接入。

扩展一个可信 native 连接器的步骤：

1. 定义有界参数模型、结构化结果和来源限制。
2. 实现适配器，使用宿主传入的用户 / 会话上下文；返回规范化资源。
3. 在 `build_data_connector_service` 的注册工厂中登记 `RegisteredConnector` 与显式回调。模型输入只能选择声明的操作，不能指定任意 Python 函数或 URL。
4. 校验声明和运行时 schema 一致、能力均已实现；增加固定小样例，覆盖错误、分页与归属隔离。
5. 目录和通用调用工具从同一注册工厂读取，无需增加专用聊天组件。

当前 JSON 是版本化能力声明，不是可上传执行的插件安装包；不能只上传 manifest 就获得执行权限。第三方 HTTP / MCP 适配、包分发和安装流程尚未交付。

## 账户与访问边界

- 未来凭据保存在服务端，ConnectionInstance 仅暴露状态、账户显示名和允许能力；浏览器 / 模型 / 日志不接收密钥。
- 使用既有会话归属检查；共享会话不加载私有来源管理面板。
- `domains` 是声明信息，不代替实际网络限制。当前 NCBI 适配器仍使用其固定来源、响应体上限、分页限制、超时与错误脱敏；未来通用 HTTP 接入须补域名、解析结果及重定向检查后才能开放。
- 本地目录或数据库连接也要走各自的只读授权。保持 DATASET_HOST_PATH_ALLOWLIST，真实宿主路径不返回浏览器或写入 URL / 浏览器存储。
- 提供方内容是数据，不能作为执行指令；元数据、文件获取、分析成功必须分别记录。

## 本轮落地与后续顺序

本轮交付目录、可执行元数据标准、统一调用、NCBI 适配和界面收拢。保留旧 ncbi_* 工具与来源记录兼容，不迁移或删除用户数据。

接下来优先扩展一个非生物领域的公开来源，验证横向能力；再做 ConnectionInstance、HTTP / MCP 映射；然后接入持久 acquire 和对话数据卡片。使用小样例验证，不开展具体科学研究。

## 本轮验收

- 后端 5,416 项通过、32 跳过、190 子测试通过；排除原有含导入时外部数据库写操作的 `test_admin_skill_delete.py`。
- 前端 2,678 项测试通过，类型检查与生产构建通过；Compose 配置检查通过。
- 沙箱宿主测试仍有 PyMuPDF、python-pptx、原生 GRIB 依赖导致的 3 项既有收集错误。
- 本地统一调用接口实际读取 `GCF_000009045.1` 的一条元数据，来源记录落入会话 `8f295a853bd34d6c`，明确 `files_downloaded=false`。凭据与本地验收回执不提交。
- 浏览器核对任务进度内执行记录、简短技能标签、任务文件中的来源详情及插件连接器目录。任务条显示最近一次执行的状态；新请求未进入执行时，不把上一条成功记录解释为新请求成功。


## 任务选择接入（2026-10-14）

新任务与会话输入框都有可见的“数据连接器”按钮，“+”菜单保留同一入口。选择仅绑定来源，不会立即联网执行；发送自然语言请求后才调用。任务提交的 data_connectors 保存于用户消息元数据，刷新后恢复，安全续作继承原选择。前置决策和实际 Agent 都能读取本次来源，通用目录与调用工具限定到所选连接器；未选择时保持自动发现兼容行为。这是任务来源偏好，不替代用户权限与网络授权。

当前仅提供 NCBI 公开元数据来源；面板从服务端声明读取名称、操作与限制。本节描述最初入口交付状态；NCBI 小文件下载现已接入，账户接入和第三方安装仍未交付。


## NCBI 小文件获取与交付（2026-10-14）

连接器版本 1.1.0 新增 acquire，输入与 describe 一样为完整 accession.version。参考 [NCBI 官方下载说明](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/command-line/datasets/download/genome/)，请求固定 HTTPS API 的 genome download，include_annotation_type=GENOME_FASTA；不接收外部 URL、不跟随重定向。首版是有界请求，不是已实现的持久下载队列。

传输最多 32 MiB，包内总量最多 64 MiB、最多 1,000 条目，拒绝加密、路径穿越、符号链接、重复条目、损坏 CRC、版本不匹配或缺少 genomic FASTA 的包。包校验通过后才上传文件存储并登记到所属会话。返回 kind=files、files_downloaded=true 和真实文件回执；SHA-256 为本地计算，不冒充官方发布校验值。已有完整文件在同一用户/会话内复用；失效引用可重新下载并替换获取指针，原始来源记录仍不可变。

Agent 将该受管文件复制到沙箱 output，复用同一个文件引用交付。新增 data 类型的 ZIP 成果校验，必须对实际字节生成大小/摘要回执，下载包不能冒充图表、表格或报告。沙箱 API 明确接受 data 类型；取消、超时和校验失败不会变成成功结果。

下载调用旁显示等待批准、下载校验中、保存成功或具体失败原因。旧 ncbi_plan_acquisition 仍只返回计划并明确提示“未开始下载”，模型被指示使用 acquire 执行真实下载。

本地验收：a7754f37151145fe 下载 GCF_000009045.1，交付接口再次取得 1,252,046 字节 ZIP，CRC 完整性通过，包内存在指定版本 genomic.fna。此为小样例功能验证，未开展科学分析。生产构建通过；沙箱全量仍受三项原有宿主依赖收集错误影响。

## GEO / SRA 主题检索

内置 `ncbi-geo` 和 `ncbi-sra` 连接器，均提供 `search`：

```json
{"query":"衰老","page_size":3,"offset":0}
```

使用 NCBI 官方 E-utilities 的 ESearch / ESummary。GEO 限定 `gse[ETYP]`，返回 GSE 研究系列；SRA 返回测序实验，附关联研究、Run 编号、物种和文库策略。SRA 的命中数量是实验数量，不能当作独立研究数量。结果中的原始词、实际检索式、检索时间和来源链接保留在工具执行记录中。

“衰老”展开为 `aging OR senescence`；其他主题可使用英文关键词或 Entrez 检索式。未指定物种时不擅自增加物种条件。通用 NCBI 主题请求默认检索两个库，各取 3 条；用户明确选择连接器时遵守其选择。结果仅为关键词匹配样例，不宣称全面筛选或生物学相关性已经验证。

每页 1–20 条，保持 query 不变，将 `next_cursor` 转为整数传入下一页 `offset`（最多 10000）。固定官方 HTTPS 接口，不接受调用者指定 URL，不跟随重定向；限制响应大小、超时、请求频率。公开元数据查询无需逐次批准；下载等其他操作不因此获得授权。

这两个连接器支持 `search`、`list_files`、`acquire`。基因组组装查询与下载仍使用 `ncbi-datasets-v2`。

官方协议：[GEO programmatic access](https://www.ncbi.nlm.nih.gov/geo/info/geo_paccess.html)、[SRA search](https://www.ncbi.nlm.nih.gov/sra/docs/srasearch/)。

## 对话内结果操作

成功检索事件提供可展开的结果列表，支持逐条勾选、全选和打开来源。列表从服务端保存的成功工具结果生成；导入请求只提交工具调用编号和选中资源编号，不接受浏览器提供的下载地址或宿主路径。

- NCBI 基因组：下载 genomic FASTA ZIP 到当前任务沙盒（每次最多 3 个，每包最多 32 MiB）。
- 所有内置连接器：把所选编号、标题、来源导入 CSV 检索清单。清单是独立的整理功能，不冒充表达矩阵或 FASTQ。
- 成功导入后，用户可点击“用这些文件分析”，把文件附件加入当前输入框并补充分析要求；不自动发送任务。
- 导入仅允许任务所有者操作；运行中的任务暂不允许导入，避免与分析并发。只有沙盒上传成功才显示已导入，部分失败按文件反馈。


## GEO/SRA 实际文件下载

在结果面板勾选研究后，点击“查看可下载文件”，再勾选实际文件并“下载所选文件到沙盒”。每个文件分别显示下载、校验、导入结果，单项失败不覆盖其他成功项；可重试失败项，已成功持久化且校验一致的文件复用缓存。成功文件可直接带入下一条分析请求。

- GEO：读取 `ftp.ncbi.nlm.nih.gov` 官方 Series 的 `suppl/`、`matrix/` 目录。`_RAW.tar` 标注原始包，矩阵明确标注处理后数据，其他补充文件不推断其原始性。根据官方 RAW 文件清单补充列出可独立获取的 GSM 样本文件，避免下载整个研究包。保留原始压缩格式，不自动解包。
- SRA：使用 ENA `filereport` 查询实验对应的 Run 与公开 FASTQ 文件，固定从 `ftp.sra.ebi.ac.uk` HTTPS 下载。逐文件核对官方 MD5、字节数和 gzip 完整性。双端数据由用户选择两个 mate，不将单端文件称作完整双端数据。
- 每次最多查看 3 条研究，每条最多展示 20 个文件；每个 GEO/SRA 文件最多 64 MiB，gzip 展开校验最多 256 MiB。超限文件显示原因并禁用，不截断或用元数据替代。基因组 ZIP 仍为 32 MiB。当前没有后台大文件队列或断点续传。
- `list_files` 参数为 `accession`；返回 `resources[].id` 及文件属性。`acquire` 参数为 `accession`、`file_ids`（单个文件）。文件选择凭据保存于当前用户、当前任务，下载不接受客户端 URL；公开文件列表查询不要求重复审批。
- 若无公开文件、ENA 尚未镜像、校验失败或下载超时，返回对应原因，不报告导入成功。SHA-256 为本地计算值；只有 SRA 的 MD5 与 ENA 提供值进行了比对。

协议依据：[GEO 下载说明](https://www.ncbi.nlm.nih.gov/geo/info/geo_paccess.html)、[ENA 文件报告](https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/file-reports.html)。

下载对支持 HTTP Range 的已知长度文件采用最多 8 路、每段 256 KiB 的有界并行传输；严格检查 Content-Range 和每段字节数，合并后再做整体校验，失败不交付残缺文件。单次下载总超时 45 秒。

小样例验收使用 GSE1000 的 GSM15785.cel.gz（3,460,434 字节）和 DRX000488 的 DRR000792.fastq.gz（1,125,203 字节）。本地和线上均通过真实检索结果选择文件、下载、上传沙盒；在沙盒读取 CEL 文件头和 FASTQ 记录，后者共 6,493 条，逐条核对序列与质量长度。本验证只检查下载及可读性，不作研究结论。
