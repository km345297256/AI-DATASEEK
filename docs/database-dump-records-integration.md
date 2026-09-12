# SQL、PostgreSQL 备份、BSON 与 Redis RDB 插件集成

实现日期：2026-09-11；本机实际验收完成：2026-09-12。本轮经确认增加四个独立的有界只读文件插件，不增加数据库恢复或连接能力。公共插件协议仍为 `contract_version: 2`；合并目录为 **81 个插件、76 个批准适配器**。

## 使用方式

在“插件 → 可视化”分别启停下表插件。进入数据集探查页面，打开相应文件，通过预览类型选择器切换插件。

| 插件 | 文件与操作 | 首版边界 |
| --- | --- | --- |
| SQL 转储字面量（`viz-sql-dump`） | `.sql`；选择 PostgreSQL / MySQL / SQLite 方言，点击读取目录，再选表、字段和分页 | 受支持 `CREATE TABLE`、`INSERT VALUES` 和 PostgreSQL `COPY FROM stdin` 文本；不执行 SQL，不计算默认值、约束或类型转换 |
| PostgreSQL 备份目录（`viz-postgres-dump`） | `.pgdump/.dump/.backup/.tar`；对象类型、命名空间、名称；本地筛选和分页 | custom/tar 归档 TOC；仅 UTF-8，归档版本 1.14.0/1.15.0/1.16.0；不解压、校验或恢复数据段 |
| BSON 文档记录（`viz-bson`） | `.bson`；分组目录、手动分页、展开文档字段及类型 | 连续 BSON 文档流；不是 MongoDB archive、WiredTiger 原始文件或压缩 dump |
| Redis RDB 键值记录（`viz-redis-rdb`） | `.rdb`；逻辑库、键、类型、值与原始过期时刻 | RDB 11；string/list/set/hash/zset，受限 raw/int/LZF/intset/listpack/quicklist2；校验 CRC64，拒绝零校验和及未支持编码 |

为保留旧体验，`.sql` 默认仍使用“文本与代码”，普通 `.tar` 默认仍显示“压缩包目录”。新 SQL 转储／PG TAR 能力通过手动切换启用，不抢占原默认。插件开关彼此独立，也不改变 SQLite、DuckDB、DBF、Access、Shapefile 等现有开关。

## 必须明确的数据语义

- SQL 是**源文件字面量**，不是恢复后的数据库。数字保留原始字符串（符号、小数位和指数均不经浮点转换）；COPY 字段即使声明为数值也保持文本。页序号来自转储片段。无法解释的结构或行语法明确拒绝；其他跳过片段计数并显示说明，不伪装成已执行。
- SQL 首版不猜方言。不支持复杂表达式、INSERT SELECT、部分列依赖默认值、复杂建表 AST 等；MySQL/PostgreSQL 普通引号字符串含反斜杠时拒绝，避免对 SQL 模式作错误推断。可回到原有文本预览查看源文件。
- PG “目录可读”**不等于备份可恢复**。界面固定提示“仅完成归档目录读取；未解压、校验或恢复数据段”。不返回定义 SQL、所有者和源数据库名称；数据库、ACL、COMMENT 等非公开对象类别只保留固定隐藏标签。
- BSON 整数、Decimal128、日期毫秒与 MongoDB 逻辑时间戳保留各自类型。Decimal128 同时保留精确文本和 BID 原始位表示。JavaScript、正则等明确标记未解释，不执行或发送其代码。
- Redis 整数压缩保存的字符串仍是文本，不猜测业务类型。过期键保留快照中的时刻，不按现在的系统时间过滤。单个快照不代表当前服务器状态。
- 二进制仅显示长度／子类型。超长或不安全文本、字段名明确省略；NULL 与空字符串不同。未知 RDB 对象、损坏结构或超预算整体拒绝，不悄悄跳过记录。

## 插件适配结构

四个清单独立由 Cordis 管理；`database-dump` 批准 `sql-dump`、`pg-dump`，`database-records` 批准 `bson`、`redis-rdb`。共用外层协议不意味着混用内部数据语义：SQL 字面量、PG 对象目录与文档节点分别进行严格载荷校验。

固定链路为：已授权文件 → v2 能力与选项校验 → 文件版本／用户／预算检查 → 无网络只读沙箱 → 独立纯载荷校验 → 安全前端组件。后端与沙箱纯协议副本保持逐字节一致，前端再次校验结构及当前文件/目录/选项绑定。

保留 AgentLoop、PlanActFlow、SSE、FastAPI 主机、AnalysisJob 和原有 Docker 数据隔离边界。不增加公网端口、数据库凭据入口、SQL 控制台、自动旁车扫描、任意 iframe 或用户脚本执行。

## 性能和资源约束

所有新增插件只接受单个完整文件，最多 **16 MiB 输入 / 2 MiB 输出**，不是大型数据库随机读取方案。

- SQL：最多 32 表、每表 128 字段；每次最多 200 行 × 16 列；语句、词元、嵌套、结构和扫描时长均有单独预算，INSERT/COPY 使用线性受限扫描，不构建巨型表达式树。
- PG：最多 1024 目录对象、8 MiB TOC、4096 TAR 成员；仅解析长度有界的目录字段，不读取数据内容。前端每页 100 个对象，搜索／筛选不增加后端读取。
- BSON/RDB：每页最多 50 条，每条 128 节点、深度 8、每页总节点 4096；完整校验文件但只保留所选页。RDB 单块解压最多 1 MiB、累计 32 MiB，先检查声明长度再分配。
- 切换文件、选项、插件或关闭预览会取消旧请求；相同文件/插件信息刷新不重复读取。除目录外，行和记录分页由用户明确发起。所有预览不产生模型调用。

大文件和超出支持子集的恢复需求不强行放宽交互式预算，后续应另行设计明确授权的隔离 AnalysisJob。

## 固定依赖与原生证据

生产仅新增 `sqlglot==30.18.0`（受限 CREATE AST）及 `pymongo==4.17.0`（仅固定 16 字节 Decimal128 值类型，不创建 MongoClient）；依赖写入沙箱锁文件。PG/RDB 为专用有界格式适配器，生产不新增 PostgreSQL 或 Redis 服务器。

样本全部原创：SQLite 3.51.0 原生 iterdump、PostgreSQL 18.6 custom/tar/plain 与官方目录 oracle、PyMongo 4.17.0 编码 BSON、Redis 7.2.7 原生 SAVE。生成工具仅运行于临时隔离测试环境，无业务卷／业务凭据，结束后清理。wire 构造样本另行标记，不当作原生兼容证据。

PG 18.6／归档 1.16 已有真实 writer 与官方 `pg_restore --list` 对照；归档 1.14/1.15 目前仅格式级测试，不能宣称所有历史版本、压缩和加密方案已兼容。

详情：[SQL 设计与测试](sql-dump-preview-research.md)、[BSON/RDB 载荷规范](database-records-payload.md)、[服务器格式边界与官方来源](server-database-file-expansion.md)、[PG 原生样本](../sandbox/tests/fixtures/database-dumps/README-postgres.md)、[记录文件原生样本](../sandbox/tests/fixtures/database-records/README.md)。非商业用途不取消依赖许可和来源保留义务。

## 验收与本机发布

2026-09-11 离线完整验收记录：

| 检查 | 结果 |
| --- | --- |
| 前端类型检查与生产构建 | 通过 |
| 前端单元测试 | 2603 通过，0 跳过 |
| Cordis 插件宿主 | 78 通过，含四个新插件各自的真实 fiber 释放测试 |
| 后端全量 | 5222 通过、32 条件跳过、179 子测试通过 |
| 沙箱全量（全部指定参考库启用） | 5496 通过、0 跳过、563 子测试通过 |
| 最终 Chrome 完整回归 | 155/155 通过；新增 10 个场景全部通过；无外部请求、意外请求或控制台错误 |
| 候选镜像一致性 | 后端、沙箱源码及清单与工作区一致；原 NumPy 1.26.4 保持不变 |

后端 32 项跳过分别为 11 个需真实后端／文件存储的测试、11 个需真实沙箱的测试、3 个需专用 Mongo URI 的测试、7 个已被 Agent tool loop 替代的旧 fast-path 测试。离线模型与 HTTP 测试使用替身或进程内调用，不把它们当作实际线上验收。

Decimal128 校验另使用固定 PyMongo 4.17.0 的 2278 个原生编码样本与 100000 个随机编码对照，严格绑定 BID 与规范数值文本；这些检查不产生模型调用。

旧视频清理用例在前一次全量及单独复核中出现过偶发 `paused=false`，当时网络／解码／缓冲／Blob 已清空；最终完整回归通过。未修改旧媒体生产逻辑，也未降低断言，不能据此宣称该既有偶发状态已被修复。

最终浏览器报告：`/var/folders/n2/wz3bk3pj2ps8y5qk2zx257v40000gn/T/dataseek-visualization-browser-cYOTIB/results.json`。测试日志：`/private/tmp/dataseek-database-files-{frontend-final-tests,backend-full,sandbox-full,cordis}.log`。

### 2026-09-12 本机复核与实际 HTTP 验收

继续任务时，发现中断期间应用已经更新，四个新插件已注册在当前服务中。因此本次没有用旧候选镜像覆盖现有服务，也没有再次重启服务。实际入口仍为 `http://localhost:7001`，仅绑定 `127.0.0.1:7001`；插件页返回 HTTP 200，目录报告 Cordis、81 个插件，四个新插件均恢复为验收前的启用状态。

核对结果：19 个相关后端／内嵌沙箱文件、四个插件清单、六个验收及安全脚本与工作区哈希一致；四个新前端组件与已验收生产构建逐字节一致。本次重新运行前端类型检查、协议生成一致性检查（76 个适配器）及 Compose 配置检查，全部通过。

| 实际服务检查 | 通过结果 | 临时文件上传／清理 |
| --- | --- | --- |
| SQL / PG / BSON / RDB | 64 项检查，包括只读结果、精度、分页、版本、非法选项、损坏文件、独立停用及源样本哈希 | 12 / 12 |
| 原有 DuckDB / DBF / Access | 70 项检查 | 9 / 9 |
| SQLite / 雷达 / UGRID | 45 项检查 | 9 / 9 |
| NetCDF / FITS / FASTQ | 7 项正向、5 项拒绝检查 | 4 / 4 |
| PDF / Word / Excel / PowerPoint 及扩展科学格式 | 15 项正向、20 项拒绝检查，另含 4 项 FastQC 作业检查 | 14 / 14 |

五组验收均通过，合计 48 个临时文件全部清理；另清理本次创建的 2 个 FastQC 作业及 1 个产物。所有临时插件开关恢复，清理错误为空，未残留本次预览／样本容器。测试没有访问原有用户文件内容，没有调用模型端点，也没有执行数据库恢复。这里的 Office 结果指原有文档转换与预览接口，不等同于重测 ONLYOFFICE 编辑器的全部交互。

以 **9 月 12 日本次继续前** 为基线（不使用中断前的旧状态）：181 个文件、51 个数据集、30 个会话、1954 条会话事件、214 个分析作业、2 个产物、501 条模型追踪及 488 条用量记录保持不变；会话、事件、文件、作业、产物、模型与用量的已检查元数据摘要也完全一致。MongoDB、Redis、MinIO、Office gateway、ONLYOFFICE 的容器标识及启动时间在本次前后相同。

本次记录的镜像标识：

- Backend：`sha256:4ae6d49816a352031fd45e58a3362a1e38c3ec5e5682ba2c890445f21352f06e`
- Frontend：`sha256:b455c313449cd93d18f040ef57880f1fac8b2e43b14bd59305429afea994052d`
- Sandbox：`sha256:d5d28c92532c7ba07c9022a9809fb2a55cc92e2e102c24662317b1f1384e7408`

实际验收日志：`/private/tmp/dataseek-database-files-{new,old-db,batch-five,science,office}-http-20260912.log`。临时日志目录不保证长期保留，本节保存验收结果摘要；前一节的 9 月 11 日测试记录不冒充本次重新执行的全量测试。

状态：**当前本机服务已包含四个新插件，实际验收完成**。未提交或推送 Git，未新增公网端口或第二套部署。

## 尚未纳入的格式

SQL Server `.bak/.mdf/.ldf`、MySQL 原始 `.ibd`、Oracle `.dmp`、WiredTiger `.wt`、LevelDB/RocksDB/LMDB 完整存储目录不在本次四插件的支持承诺中。MySQL SDI 元数据、RocksDB 单 SST 物理记录可作为下一步独立静态插件；需要引擎恢复的格式仍需单独确认厂商、版本、完整文件集、资源与权限。
