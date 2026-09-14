# 常见数据库文件可视化：选型与本轮边界

核验日期：2026-09-11。本文记录来源、选型和实现范围。DuckDB、DBF、Access 三个独立插件的原集成与发布见[数据库文件集成说明](database-visualization-integration.md)。用户随后确认追加 SQL、PostgreSQL 目录、BSON 和 Redis RDB；最新范围与验收见[转储和记录插件](database-dump-records-integration.md)。其他后续候选不表示已支持。

## 清单与取舍

2026-09-13 新增 MySQL SDI 与 SST 两个独立插件的实现；LevelDB 仅支持经过原生对照的严格单表子集。当前代码目录为 83 个插件，发布／验收状态与未支持的服务器格式见[物理数据库文件集成](physical-database-integration.md)。

| 数据类别 | 文件 | 当前方案 | 本轮处理 |
| --- | --- | --- | --- |
| SQLite 单文件数据库 | `.sqlite`、`.sqlite3`、`.db` | 既有 `viz-sqlite-table`，APSW 3.53.4.0 | 保留既有 ID、行为、开关、预算；不另装 SQLite 桌面浏览器 |
| DuckDB 分析数据库 | `.duckdb`、`.ddb` | DuckDB 官方引擎 1.5.5 + 受控本地表格组件 | 新增独立 `viz-duckdb-table`；表目录、明确选列和分页；不开放 SQL 控制台 |
| dBASE / DBF 单表 | `.dbf` | dbfread 2.0.7 + 有界字段校验 | 新增独立 `viz-dbf-table`；首版限定 dBASE III 无 memo 单文件；不是全部 FoxPro 方言 |
| Microsoft Access | `.mdb`、`.accdb` | MDB Tools / libmdb 1.0.1 + 专用受限原生读取适配层 | 新增独立 `viz-access-table`；实体表结构、标量、分页；不执行 VBA、保存的查询或链接表 |
| SQL 文本转储 | `.sql` | 原“文本与代码”默认 + 新 `viz-sql-dump` 可手动切换 | 明确方言后预览受支持结构和字面量；不执行、恢复或自动导入 SQL |
| 列式数据文件 | `.parquet`、`.parq`、`.arrow`、`.feather` | 既有 PyArrow 列／行分块插件 | 保留现有实现，不因引入 DuckDB 而改为整文件读取或引入 SQL |
| 科学层级容器 | HDF5、NetCDF4、NeXus、H5AD 等 | 既有 array-window、H5Web、NeXus、空间组学等专项插件 | 保留明确的科学结构、坐标、单位和切片语义；不是关系型数据库管理器 |
| 地理数据库 | `.gpkg`、SpatiaLite、`.gdb` 目录、`.mbtiles` | 后续候选：GDAL 对应驱动 + 独立地图／属性表插件 | 本轮不注册，不能用普通 SQLite 表预览冒充地图支持 |
| PostgreSQL 逻辑备份 | custom/tar dump | 新 `viz-postgres-dump`：有界 TOC 目录 | 不解压、验证或恢复数据段，不启动数据库服务 |
| 其他服务器备份／存储文件 | SQL Server `.bak/.mdf/.ldf`、MySQL `.ibd`、Oracle `.dmp` | 后续候选；取决于引擎、版本、配套文件 | 不自动恢复、不提供连接凭据入口 |
| 键值／文档逻辑文件 | Redis `.rdb`、MongoDB `.bson` | 新 `viz-redis-rdb` / `viz-bson` 独立只读插件 | 固定版本与编码子集，精确字段类型、目录和手动分页 |
| 其他文档／键值存储文件 | WiredTiger、LevelDB/RocksDB/LMDB 目录等 | 仍为后续候选 | 不将 `.db` 当通用格式，不自动扫描目录／加载数据库扩展 |

新的三个插件仍使用 Cordis 契约版本 2，各自独立启停，共用受限 `database-table` 载荷和前端表格呈现。复用组件不表示取消格式识别、独立权限或引擎边界；不改变 AgentLoop、PlanActFlow、SSE、FastAPI 主机和只读数据集挂载。

## 官方选型依据

### DuckDB

采用 [DuckDB 官方项目](https://github.com/duckdb/duckdb)，而不是把其桌面／独立 Web SQL 管理器嵌入应用。核验时最新稳定版本为 [1.5.5](https://github.com/duckdb/duckdb/releases/tag/v1.5.5)，发布说明含多处边界访问修复；固定版本，后续升级重新验收。项目采用 [MIT 许可](https://github.com/duckdb/duckdb/blob/main/LICENSE)。

官方的[安全配置说明](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)明确指出外部文件访问和扩展能力需要主动约束；[扩展安全说明](https://duckdb.org/docs/current/operations_manual/securing_duckdb/securing_extensions)提供禁止自动安装／加载及锁定配置的选项。因此只读连接之外，还应关闭外部访问、Python replacement scan、自动安装／加载扩展、社区／未签名扩展和持久 secrets，只允许固定投影查询。引擎 `memory_limit` 不等于整个进程的内存上限，现有无网络、只读、非 root、CPU／内存／超时隔离仍不可省略。

### DBF

[dbfread 官方项目](https://github.com/olemb/dbfread)是纯 Python 读取器，[许可为 MIT](https://dbfread.readthedocs.io/en/latest/license.html)，固定 2.0.7。该项目的公开文档和版本长期稳定，不把它描述成近期频繁发布的项目。

[官方 DBF 对象说明](https://dbfread.readthedocs.io/en/latest/dbf_objects.html)说明默认按文件中的语言标识推断编码，并可能寻找 `.dbt/.fpt` memo 旁车。本轮限制为无需旁车的 dBASE III：只处理授权单文件，拒绝不支持的版本／类型／未知编码，不扫描邻近文件，不把缺失 memo 悄悄变成 NULL。数值字段的十进制文本单独保留，避免通用解析器经浮点丢失精度；缺失、空字符串、逻辑未知值和删除标记需要明确说明。

### Access

[MDB Tools 官方项目](https://github.com/mdbtools/mdbtools)提供 libmdb 和读取工具；固定 [1.0.1](https://github.com/mdbtools/mdbtools/releases/tag/v1.0.1)，该版包含空指针、释放后使用等修复。官方说明 libmdb 为 LGPL，命令行工具和 GUI 为 GPL；本轮使用库和自己的只读适配层，不安装 ODBC、SQL 控制台或 GUI。非商业用途不取消保留许可／来源等义务。

固定官方发布资产（不是滚动分支压缩包）：

```text
URL: https://github.com/mdbtools/mdbtools/releases/download/v1.0.1/mdbtools-1.0.1.tar.gz
SHA-256: ff9c425a88bc20bf9318a332eec50b17e77896eef65a0e69415ccb4e396d1812
```

该 SHA-256 是本轮从官方 release 资产实际下载后计算的完整文件摘要，用于固定构建输入，不宣称它是独立第三方安全认证。

不能直接以 `mdb-json` 的输出作为科学数据真值。[1.0.1 源码](https://github.com/mdbtools/mdbtools/blob/v1.0.1/src/util/mdb-json.c)按绑定长度决定字段是否输出，会混淆 NULL 与空文本，并可能整体读取 OLE 对象。专用读取适配层应直接检查有界目录／行结构与 NULL 位图，不调用 SQL，不读取数据库属性 blob、长文本／嵌入对象，不跟随链接；对原生行解析之前的长度、列序号和位图边界也必须做检查。

真实格式验收样本采用 [Jackcess 官方写入器](https://jackcess.sourceforge.io/cookbook.html)自创，再用其独立只读读取器回读。固定 Jackcess 4.0.11，仅在一次性测试容器使用，不给生产增加 JVM；[许可为 Apache-2.0](https://jackcess.sourceforge.io/license.html)。源码、oracle 和样本见 [Access 测试样本说明](../sandbox/tests/fixtures/database/README.md)。没有复制网络上许可不明的 Access 数据库，也没有读取用户文件。基本 oracle 覆盖 V2000 MDB 和 V2010 ACCDB，并不代表全部 Jet/ACE、加密文件或复杂 Access 对象已兼容。

## 为什么本轮不恢复服务器备份

数据库转储不是惰性的可视化资源。[PostgreSQL `pg_restore` 官方说明](https://www.postgresql.org/docs/current/app-pgrestore.html)特别警告恢复可执行源数据库超级用户选择的代码。[MySQL 官方说明](https://dev.mysql.com/doc/refman/8.0/en/using-mysqldump.html)说明常见 dump 包含 SQL 语句。追加实现只做 SQL 静态字面量和 PG TOC 目录解析；不能为了“预览”自动导入用户文件、执行任意 SQL 或让浏览器控制数据库引擎。

后续若确需恢复，应作为用户明确发起的隔离 AnalysisJob：固定引擎及版本、断网、临时数据库、最小权限、无宿主数据挂载写入、受限输出、到期销毁，并对数据库方言与备份类型单独验收。本轮不预先实现这些新增权限。

## 地理数据库需要独立语义

[GDAL GeoPackage 驱动](https://gdal.org/en/stable/drivers/vector/gpkg.html)描述了属性表、几何列、空间参考和扩展；[SpatiaLite 驱动](https://gdal.org/en/stable/drivers/vector/sqlite.html)还包含专门的空间内容；[OpenFileGDB](https://gdal.org/en/stable/drivers/vector/openfilegdb.html)通常是多文件目录。这些都需要按 CRS、几何类型、图层、空间窗口和旁车授权处理。

当前 `spatial-window` 是 **H5AD 空间组学**，并非 SpatiaLite 或 GeoPackage。现有 SQLite 插件明确拒绝虚拟表、触发器等结构，也不解码 BLOB 几何；因此本轮不把上述地理数据库列入已经支持的格式。将来可以在相同 Cordis 契约下增加地图与属性表两个独立插件，让用户按场景组合，而不扩大普通数据库插件的解析权限。
