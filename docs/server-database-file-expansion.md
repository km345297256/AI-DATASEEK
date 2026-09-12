# 服务器数据库文件扩展：静态预览与恢复边界

核验日期：2026-09-11。本文是本轮 SQL 转储、PostgreSQL 归档、BSON、Redis RDB 扩展的安全设计与后续候选清单，不表示下表全部工具已经部署。当前完成状态以对应集成验收记录为准。

## 支持层次

“识别文件”“解析目录”“读取物理记录”“恢复后的逻辑数据”是不同能力，界面和插件说明必须明确区分。仅检测后缀或 magic 不能标为已支持数据预览。

| 格式 | 不恢复数据库时可获得的有用内容 | 本轮／后续建议 | 不能承诺的能力 |
| --- | --- | --- | --- |
| SQL 文本转储 | 有界词法解析得到表结构、明确的字面量 INSERT／COPY 子集 | 本轮独立静态插件；保留普通文本阅读 | 不执行 SQL；不求值函数、表达式、客户端命令；不把部分解析称为完整恢复 |
| PostgreSQL custom／tar 逻辑归档 | 归档 TOC：对象类型、schema、表名、数据条目存在性 | 本轮优先；明确标注“目录预览” | 不验证数据段完整性、不生成行数或表格、不连接服务器 |
| MongoDB `.bson` 逻辑 dump | 独立 BSON 文档序列、类型和有界样本 | 本轮优先，精确保留 Int64／Decimal128／Binary 等类型 | 不等同 WiredTiger 物理文件；不执行 BSON JavaScript／正则 |
| Redis `.rdb` 快照 | 受支持版本和编码中的数据库编号、键、类型、过期信息与值样本 | 本轮限定子集，未知模块／编码明确拒绝 | 不启动 Redis、不加载模块、不把旧快照的过期键当作现在仍存在 |
| MySQL 8.x InnoDB `.ibd` | 官方 `ibd2sdi` 可离线提取表／列／索引等 SDI 元数据 | 下一优先级：独立“结构目录”插件 | 不是行数据读取；不保证提交一致性；不支持缺少 SDI 的旧版本／undo／临时空间 |
| RocksDB 单个 `.sst` | 官方 `sst_dump` 可读属性、物理键值、序号和删除标记 | 后续可先做单文件物理记录插件 | 单个 SST 不是整个数据库最新视图；不能忽略其他层、WAL、合并操作或 tombstone |
| LevelDB／RocksDB 完整目录 | 在一致快照与匹配引擎下可得到逻辑键值 | 后续独立目录型 AnalysisJob 设计 | 当前单文件授权不能扩为扫描整个目录；不能在原目录启动会生成日志的引擎 |
| LMDB `data.mdb`／环境目录 | 匹配位宽／字节序并完成离线结构验证后，可按数据库／键读取 | 后续独立验证器与临时快照适配 | `.mdb` 后缀不等于 Access；只读打开仍可能写锁文件；不加载用户提供的锁文件 |
| SQL Server `.bak` | 官方 HEADERONLY／FILELISTONLY 可给备份集和组成文件目录，但需要 SQL Server 引擎与权限 | 用户明确选择厂商后再设计 | 不是独立离线工具；不能仅凭备份头声称可读表数据 |
| SQL Server `.mdf/.ndf/.ldf` | 原始页／日志取证需专门解析；可靠逻辑数据通常依赖完整文件集与引擎恢复 | 不作为本轮浏览器预览自动恢复 | 单个 MDF／LDF 不等于完整库；附加／重建日志会改变临时库状态 |
| Oracle `.dmp` | Data Pump 的 SQLFILE 可产生 DDL，但需要服务端 Oracle 作业、DIRECTORY 和授权 | 用户明确需要且提供版本／备份方式后单独设计 | 不是无服务端的本地解码；传统 exp 与 Data Pump 不能按后缀互换 |
| MongoDB `.wt`／WiredTiger 目录 | 专用引擎可访问一致 checkpoint 与 metadata；必要时结合 journal | 后续完整快照工作流 | 单个 collection `.wt` 不保证可解释，更不保证逻辑一致；需匹配版本／压缩／加密配置 |

## PostgreSQL：为什么首选受限 TOC 解析

[`pg_restore` 官方文档](https://www.postgresql.org/docs/18/app-pgrestore.html)区分连接数据库恢复和仅输出脚本；没有 `--dbname`、只使用 `--file` 时不会把转储 SQL 发给数据库。但文档也警告，恢复不可信归档可能执行来源超级用户构造的代码。因而“先恢复再预览”不属于本轮只读可视化。

固定参考 [PostgreSQL 18.6 源码](https://github.com/postgres/postgres/tree/REL_18_6/src/bin/pg_dump)中的归档读取实现，而不是滚动分支：

- [`pg_backup_archiver.h`](https://github.com/postgres/postgres/blob/REL_18_6/src/bin/pg_dump/pg_backup_archiver.h)：归档版本 1.14／1.15／1.16 的字段差异。
- [`pg_backup_archiver.c`](https://github.com/postgres/postgres/blob/REL_18_6/src/bin/pg_dump/pg_backup_archiver.c)：TOC 的长度前缀、对象字段和目录输出。
- [`pg_backup_custom.c`](https://github.com/postgres/postgres/blob/REL_18_6/src/bin/pg_dump/pg_backup_custom.c)：custom 目录附加 offset 状态。
- [`pg_restore.c`](https://github.com/postgres/postgres/blob/REL_18_6/src/bin/pg_dump/pg_restore.c)：连接模式只在显式数据库参数下启用。

设计推论：`--list` 是面向人的空格拼接文本，不是可靠的结构化接口；schema、对象名、owner 可包含空格，不能通过拆词准确分栏。TOC 本身未压缩，专用有界读取器可读取必要的长度前缀字段，跳过 SQL／owner／数据库名，避免为目录浏览引入完整恢复程序及压缩数据处理。

初版建议仅接受明确版本的 custom 归档及普通 tar 中唯一、正规 `toc.dat` 成员。读取完整 TOC 后才返回目录，禁止假造对象；拒绝截断、重复 ID、超限字段／依赖、未知版本与歧义编码。tar 只在内存内定位成员，禁止提取、路径跳转、符号链接、硬链接、设备、稀疏文件及扩展头。TOC 不压缩不代表整个文件无压缩：数据块仍可能为 gzip／LZ4／Zstandard，本轮不解压、不宣称验证过数据块。

必须用官方 `pg_dump` 自造 custom 和 tar 文件，以同版本 `pg_restore --list` 作为独立目录 oracle；测试可在无业务数据的一次性环境中生成，不能启动或恢复用户数据库。纯 Python 读取器不新增生产数据库服务，兼容现有 Linux ARM64 sandbox；未来若需要官方工具，仅构建固定版本的客户端，不安装、运行 PostgreSQL 服务端。官方下载提供[固定 18.6 源码与校验文件](https://www.postgresql.org/ftp/source/v18.6/)。

本轮实现补充：已完成纯 `pg-dump` reader 和前后端同结构验证，真实 PG18.6 原创 none／gzip／LZ4／Zstandard custom 与 tar 目录验收通过；详见[原生样本与独立 oracle](../sandbox/tests/fixtures/database-dumps/README-postgres.md)。对象目录不是恢复清单的原样文本：只展示明确允许的对象标识，DATABASE、COMMENT、ACL、USER MAPPING、FOREIGN SERVER 等复合 tag 的名称及命名空间固定隐藏，防止间接泄露来源库名或所有者。后端与前端也必须拒绝这些敏感类型上的未隐藏标签。

首版请求仅 `tree` 且 options 为空；最多 16 MiB 输入、8 MiB TOC、1,024 对象、单字段 1 MiB、单对象 256／全件 16,384 依赖、tar 4,096 成员。超限拒绝整件，不默默截掉对象。`objects_total` 是读取到的 TOC 项总数，包含配置和明确隐藏的元数据对象，不是表数。页面返回 `data_verified:false`，不提供 SQL、对象执行或数据页。

## 其他引擎的官方依据

### MySQL

[`ibd2sdi` 官方说明](https://dev.mysql.com/doc/refman/8.0/en/ibd2sdi.html)明确这是 SDI 提取工具，输出 JSON，不访问 redo／undo，读取未提交的 SDI；表空间结构目录可以有用，但不能当表行内容。输出还可能含原文件名、内部私有字段，因此即使后续集成，也只允许表名／列／索引等约定字段，禁止原样透传 JSON。

[可传输表空间说明](https://dev.mysql.com/doc/refman/8.0/en/innodb-table-import.html)要求正确的导出／导入流程，常伴随 `.cfg`；加密空间另有 `.cfp`。此路径涉及目标数据库写入，不应混入静态预览。完整原始目录还可能涉及系统表空间、redo、undo、数据字典和密钥，不能自动尝试引擎版本直到“能打开”。

### SQL Server

[备份头和历史说明](https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/backup-history-and-header-information-sql-server?view=sql-server-ver17)指出 HEADERONLY／FILELISTONLY 等是 Transact-SQL，需相应数据库权限。它们不恢复用户库，但仍引入服务端执行环境。[数据库文件说明](https://learn.microsoft.com/en-us/sql/relational-databases/databases/database-files-and-filegroups?view=sql-server-ver17)及[附加数据库说明](https://learn.microsoft.com/en-us/sql/relational-databases/databases/attach-a-database?view=sql-server-ver17)要求考虑完整数据／日志文件集。

当前本机是 ARM 环境；[微软容器支持说明](https://learn.microsoft.com/en-us/sql/linux/containers/deploy?view=sql-server-ver17)限定 Linux x86-64。不能悄悄安装仿真容器、接受新的服务许可或对外开放端口。加密备份还需来源证书／密钥，不应让用户把密钥写进文件 URL 或日志。

### Oracle

[Data Pump 概述](https://docs.oracle.com/en/database/oracle/oracle-database/26/sutil/oracle-data-pump-overview.html)说明文件访问属于服务端，通过 DIRECTORY 对象授权。[Import 参数说明](https://docs.oracle.com/en/database/oracle/oracle-database/26/sutil/parameters-available-command-line-mode-import-oracle-data-pump.html)中的 SQLFILE 是输出 DDL 的作业模式，并非一个无需 Oracle 服务的解码器。需要区分传统导出、Data Pump、分卷／压缩／加密 dump，以及版本和字符集；本轮不接受恢复权限、不开新 Oracle 服务。

### MongoDB／WiredTiger

[WiredTiger 存储引擎说明](https://www.mongodb.com/docs/manual/core/wiredtiger/)解释了 checkpoint、metadata 与 journal 的一致性关系；[文件系统快照备份说明](https://www.mongodb.com/docs/v8.0/tutorial/backup-with-filesystem-snapshots/)要求协调数据／日志快照和写入活动。原始 `.wt` 不是 `.bson` 导出流。后续若接入必须显式授权完整快照、冻结副本、确定版本和压缩／加密依赖；不能自动扫描邻近文件，不使用 repair／salvage 修改原件。

### LevelDB／RocksDB／LMDB

[LevelDB 文件组成](https://github.com/google/leveldb/blob/main/doc/impl.md)包括 CURRENT、MANIFEST、日志和排序表；单个 `.ldb` 仅是其中一部分。[RocksDB 官方工具说明](https://github.com/facebook/rocksdb/wiki/Administration-and-Data-Access-Tool)支持 SST 属性／物理记录检查，并提醒即使只读数据库操作也可能产生目录日志。优先单文件 SST 的显式“物理记录”，目录逻辑视图需[一致 checkpoint](https://github.com/facebook/rocksdb/wiki/Checkpoints)及单独的目录输入合同。

[py-lmdb 信任模型](https://lmdb.readthedocs.io/en/latest/#trust-model-and-offline-verification)明确警告不可信 mmap 结构、锁文件和架构差异。未来只能在同一冻结临时副本上先完整验证，再在断网子进程按匹配位宽／字节序读取；不使用用户提供的锁文件，且不将不能解析的二进制键值猜成 JSON。

## 统一插件规范与新增权限

本轮沿用 Cordis v2 外层契约：独立插件 ID／起停、只读 `file:read`、授权 file_id 与 version 绑定、固定 reader／adapter、输入与输出预算、超时、取消和结构化错误。前端只呈现声明式安全字段，不引入 SQL 控制台、iframe 管理器、连接串或凭据输入。不修改 PlanActFlow、AgentLoop 或 SSE。

后续完整引擎恢复必须另立架构决策，并取得用户对具体文件、厂商版本、资源、许可／密钥使用和恢复动作的授权。用隔离 AnalysisJob 表达：无网络、无业务凭据、源数据只读，写入仅临时副本；限制进程、CPU、内存、磁盘、解压量和运行时间；完成后只导出经过校验的数据 artifact 并销毁临时环境。现有可视化开关不等于授予恢复、修复、旁车扫描或服务安装权限。
