# 数据库原始文件：主流版本目标与准入条件

核验日期：2026-09-14。

用户已确认不指定自己的具体版本，以主流版本为目标。**不再以等待用户提供版本或样本作为研发前置条件**：使用对应版本的原生工具生成原创样本，并保留独立读取结果作为验收依据。

本文是研发目标和架构边界，不是已支持清单。本次只完成版本选型与依赖可行性核验，未新增四类插件、未改变线上目录或部署服务。已上线目录仍为 83 个插件；上一批完成范围见[物理数据库文件集成](physical-database-integration.md)。

## 目标版本

| 数据库 | 首轮目标 | 兼容验证目标 | 输入与真实能力边界 |
| --- | --- | --- | --- |
| SQL Server | 2022（16.x）、2025（17.x） | 2019（15.x）备份 | `.bak` 优先验证备份集／文件目录；`.mdf/.ndf/.ldf` 作为完整数据与日志文件集另行设计。目录不等于表行数据；不得自动附加或重建日志 |
| Oracle | 19c、26ai | 21c Data Pump 导出 | `.dmp` 优先 Data Pump，明确区分传统 exp、多卷、压缩、加密；不凭后缀推断版本或运行导入 |
| MongoDB／WiredTiger | MongoDB 7.0、8.0 对应的 WiredTiger 构建 | 后续补测 8.3；6.0 仅历史兼容候选 | 完整一致的离线快照，不把单个 collection `.wt` 当 BSON。固定 MongoDB 源码版本所带引擎及压缩依赖，不拿独立 `wt` 版本号替代 MongoDB 版本 |
| LMDB | 0.9.x、64 位小端文件 | 以原生 0.9.31、0.9.35 写入器分别造样本 | `data.mdb`／单文件环境；主库、命名库、重复键分别验收。不接收源锁文件，不把 `.mdb` 一律交给 Access；1.0／不同位宽或字节序不列首轮支持 |

“主流”是本项目选择的稳定验证范围，不是对所有补丁、平台和文件特性的兼容保证。引擎、工具、依赖及原创样本最终固定到具体版本和摘要；未知文件版本明确拒绝，禁止轮流启动不同引擎试开。

选型依据：[微软生命周期表](https://learn.microsoft.com/en-us/sql/sql-server/end-of-support/sql-server-end-of-support-overview?view=sql-server-ver15)列出 2019／2022／2025 的支持窗口；[Oracle 官方说明](https://blogs.oracle.com/database/take-action-today-protect-your-oracle-database-against-ai-enabled-cybersecurity-threats)将 19c 和 26ai 列为长期支持版本；[MongoDB 生命周期](https://www.mongodb.com/legal/support-policy/lifecycles)列出 7.0、8.0 和 8.3，同时 6.0 已结束常规支持。因此首轮选择 7.0／8.0，而不是仅追逐短周期发行版。

## 实际核验与实施限制

### SQL Server

本机 `uname -m` 为 `arm64`。[微软官方容器说明](https://learn.microsoft.com/en-us/sql/linux/containers/deploy?view=sql-server-ver17)仅支持 Intel／AMD x86-64，明确不支持 Rosetta／QEMU 等仿真环境。当前“本机部署、不使用公网”的限制保留：不擅自改成远程数据库服务，也不把仿真当正式兼容方案。

官方 HEADERONLY／FILELISTONLY 需要引擎，不能通过安装 `sqlcmd` 客户端就实现离线备份预览。用户已明确接受**实验性本机 x86-64 仿真**，当前先实现默认关闭的隔离运行验证入口，见[实验运行环境](sqlserver-experimental-runtime.md)。该授权不包括自动恢复用户数据库。SQL 文本转储和通用表格导出可复用现有插件，但不得宣称替代了 `.bak/.mdf` 支持。

### Oracle

[GET_DUMPFILE_INFO](https://docs.oracle.com/en/database/oracle/oracle-database/21/sutil/using-oracle-data-pump-api.html)是服务端 PL/SQL 能力；SQLFILE 也不是无服务的独立文件阅读器。进一步实现需要单独决定是否引入厂商运行环境、许可与临时写入隔离。只读预览开关不授予恢复、执行来源 DDL 或使用来源凭据的权限。

### WiredTiger

[官方只读模式](https://source.wiredtiger.com/11.3.1/readonly.html)要求不需要恢复的数据库状态。未来采用授权的完整冻结快照及匹配引擎；缺少 metadata、checkpoint 或需要 recovery 的情况明确拒绝。单文件授权不得自动扫描邻近文件；`repair`、`salvage`、升级和 journal recovery 不作为打开预览时的自动步骤。

### LMDB

本轮在两次自动清理的一次性沙箱中安装并核验当前配置包源的 `lmdb==2.3.0`，未读取用户文件。实际结果：

```text
package: 2.3.0
lmdb.version(): (0, 9, 35)
importlib.util.find_spec("lmdb.verify"): None
from lmdb import verify: ImportError
```

[滚动 latest 文档](https://lmdb.readthedocs.io/en/latest/)目前描述了离线验证器及双引擎能力，但上述实际发布包不能据此假定存在这些接口。先核对固定源码／发布包和校验摘要；或实现独立有界字节验证／读取器，并用原生样本交叉测试。不得用 `readonly=True` 代替不可信文件结构校验，也不得因此关闭校验后直接交给 mmap 引擎。两个核验容器均自动删除，生产依赖未改。

## 统一接入要求

继续保留 Cordis 公共协议 v2、独立启停、声明式能力、文件归属与版本绑定、路径白名单、只读数据挂载和现有 SSE／AgentLoop。需要完整快照的格式使用明确的文件集输入合同；不能在现有单文件 reader 中暗中增加目录扫描。

每个实际可用插件须同时通过：原生正例与独立 oracle、格式和版本拒绝、损坏／截断／超限、权限与版本冲突、取消及迟到响应、启停隔离、前后端载荷一致性、浏览器预览、在线临时样本清理和旧功能回归。没有真实读取与这些证据，不新增只认后缀的占位插件。

版本范围已确定；SQL Server 本机仿真实验已获授权。后续仍须完成真实读取、输入文件集、审批与隔离适配，而不是继续询问用户数据库版本。
