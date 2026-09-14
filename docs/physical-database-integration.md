# 物理数据库文件：第二批只读插件

日期：2026-09-13。状态：已更新到现有本机 `127.0.0.1:7001` 服务，实际 HTTP 验收通过。仅本轮下述两项插件已完成，后列四类仍待版本和样本确认。

## 本轮范围

新增两个独立 Cordis 插件，共用批准的 `physical-database` 适配器，公共协议仍为 v2。目录扩展到 **83 个插件、77 个批准适配器**。不改变 AgentLoop、PlanActFlow、SSE、FastAPI 或只读数据集挂载；用户可以分别启停。

| 插件 | 可读内容 | 明确边界 |
| --- | --- | --- |
| `viz-mysql-sdi` MySQL 表空间结构目录 | `.ibd` 中的表、列、索引以及隐藏列信息 | MySQL 8.0 SDI 80019／字典 80023、未加密未压缩 16 KiB 独立表空间；固定官方工具 8.0.46。不是行数据，也不是事务一致快照 |
| `viz-sst-records` SST 物理点记录 | `.sst/.ldb` 单文件中的十六进制键值、精确序号、value/deletion/merge/single-deletion 标记 | RocksDB block-based 文件、字节序比较器；拒绝范围删除和非零外部全局序号。不合并 SST 层、WAL、merge 或 tombstone，不能称为最新逻辑数据 |
| 同一 SST 插件内的旧格式分支 | LevelDB 1.23 原生单表子集 | 最多 4 MiB、无压缩、无过滤／其他元数据块、平坦索引、仅 value/deletion；不是整个目录或通用 LevelDB 格式支持 |

MySQL 首版的实际原生样本来自 8.0.46；符合上述字典标识的其他 8.0 文件仍受严格结构检查，不据此承诺所有历史发行版本。RocksDB 固定离线工具为 Ubuntu `6.11.4-3`，不把可识别 `.sst` 后缀当作支持所有新格式／定制比较器／时间戳。

## 使用方式

在“插件 → 可视化”分别启停，进入数据集后打开相应文件并选择预览类型。首次只做有界静态读取；搜索和每页 50 行的翻页完全在已验证结果上进行，不增加读取或模型调用。

- MySQL：`hidden=1` 表示可见列，其余值为隐藏列；索引 `columns` 是从 0 开始的 SDI 列位置，包含引擎内部字段。复杂 ENUM／SET／表达式不透传字面量，以内部类型代码明确表示；不展示源数据库名、文件路径、所有者或完整 DDL。
- SST：键和值以十六进制展示，不猜测字符串编码或 JSON。序号保留十进制文本，避免 JavaScript 浮点失真。`omitted=key/value/both` 表示超限省略，原字节长度保留；不能把省略栏的空显示当作实际空值。删除标记与空值独立显示。
- 界面持续显示“不是最新逻辑状态／不保证提交一致性”，不提供 SQL、连接、恢复或修复按钮。

## 适配与安全约束

输入最多 16 MiB（旧 LevelDB 分支 4 MiB），最多 1024 条目录／点记录，输出最多 2 MiB。键最多展示 128 字节，值最多展示 512 字节，超出明确省略而非误报为空。超过记录总预算则拒绝整个预览。

固定链路：授权 file_id 和版本 → Cordis 开关／能力／格式／参数 → 断网非 root 一次性沙箱 → 固定离线程序 → 独立严格载荷验证 → 前端再次检查 reader、格式、大小及版本。所有 options 必须为空，不允许用户控制程序参数、路径或工具名称。切换文件、禁用插件或卸载组件会取消旧请求，迟到响应不得覆盖新文件。

原生进程无 shell、无网络、无业务卷和凭据，只读唯一临时副本，运行时限 12 秒；标准输出最多 8 MiB、错误输出最多 8 KiB。错误流非空、进程异常、未知格式或超预算均拒绝，诊断文本不返回浏览器。结束前检查原副本字节未变、未生成其他文件；上层保持现有容器 CPU／内存／进程约束。

MySQL 官方工具校验 SDI；不宣称已校验所有表行页。SST 固定工具校验所读块，输出属性中的总点记录、删除／合并计数与实际返回结果交叉核对；出现范围删除或自定义比较器即拒绝。旧 LevelDB 没有完整属性目录，因此先由额外的有界验证器检查所有块 CRC32C、重启点、索引句柄及文件连续覆盖，再与原生工具逐条比对。

生产镜像只复制离线 `ibd2sdi` 和 `sst_dump` 及版权文件，不安装／启动 MySQL 服务、不带入 `mysqld`、`mysql` 或 `ldb` 数据库管理入口。构建时固定签名 Ubuntu 包版本，缺失则失败，不自动降级。非商业用途仍需保留上游许可说明。

## 原生证据与测试

样本和可复查来源见[原创样本说明](../sandbox/tests/fixtures/physical-database/README.md)。MySQL 8.0.46、RocksDB 6.11.4 和 LevelDB 1.23 均使用官方原生写入器创建原创样本；没有恢复用户数据库。测试覆盖原生回读、精确值／删除、损坏校验、逐字节截断、严格参数与载荷、文件版本绑定、独立 Cordis fiber 释放、迟到响应隔离以及实际浏览器清理。

### 本轮验收结果

| 检查 | 结果 |
| --- | --- |
| 前端类型检查、生产构建 | 通过；保留现有大体积科学 SDK 分包告警 |
| 前端单元测试／Cordis 生命周期测试 | 2652／80 项通过 |
| 实际浏览器组件回归 | 157 项通过，含两个新预览组件及原有预览 |
| 后端完整回归 | 5275 项通过、179 项子测试通过、32 项跳过；不能将跳过项算作通过 |
| 沙箱完整回归 | 5872 项通过、563 项子测试通过，无跳过；开启原生数据库与科学格式依赖检查 |
| 统一契约生成检查／Compose 配置检查 | 77 个适配器一致，配置通过 |
| 新插件在线 HTTP | 31 项通过；版本冲突、非法参数、损坏、范围删除、格式错配、禁用均按预期拒绝 |
| 旧功能在线 HTTP | 数据库记录 64、数据库表 70、雷达／海洋网格 45、NetCDF／FITS／FASTQ 12、Office 等统一预览 35 项通过；另有 4 项质量检查作业断言通过 |

所有在线验收共上传并删除 54 个独立临时样本，另清理仅验收创建的 2 个质量检查作业和 1 个产物；插件偏好恢复、清理错误为空。未读取用户原文件，未调用模型、未恢复用户数据库。发布前后业务摘要逐项相同：181 个文件、51 个数据集、31 个会话、1957 条事件、214 个分析作业、2 个 spill artifact、501 条模型轨迹和 488 条 token 记录。

回归中修复了旧 DuckDB 测试对全局 `time.monotonic` 的替换：该模拟会干扰并发本地 HTTP 测试服务器，现仅替换被测模块的时钟对象。生产 DuckDB／文件传输代码未因此改变；修复后完整沙箱回归通过。后端与沙箱生产解析源码、插件清单和前端已打包产物均完成镜像一致性核对。

### 发布与可选前端打包

更新仍使用 `./run.sh up -d --no-deps backend frontend`；没有第二套 Compose、额外前端端口或新数据库服务。MongoDB、Redis、MinIO 容器 ID 与启动时间保持不变。

默认前端 Dockerfile 不变。本轮 Node 基础镜像下载停滞、官方镜像源超时，因此增加可选 `frontend/Dockerfile.prebuilt`，将刚通过类型检查和构建的产物打包到相同 Nginx 镜像、配置和入口脚本。使用方式：

```bash
npm --prefix frontend run type-check
npm --prefix frontend run build
FRONTEND_DOCKERFILE=Dockerfile.prebuilt ./run.sh build frontend
```

该入口不代替构建／测试，不应打包旧 `dist`。专用忽略文件只允许产物与固定 Nginx 配置、入口文件进入打包上下文；默认 `./run.sh build frontend` 仍走原 Node 容器构建。任何实际更新前仍须确认系统空闲。

## 后续格式与主流版本范围

2026-09-14 更新：用户已决定以主流版本为准，不再等待用户提供具体版本或样本。新的[目标版本与准入方案](database-mainstream-version-plan.md)替代下表中的版本征询；下表保留原设计的输入信息和禁止自动执行的动作。四类插件仍未完成，目标版本不等于已验证兼容。

用户确认 SQL Server、Oracle、WiredTiger、LMDB 均会使用，以下不是被删除的需求，也未以“仅识别后缀”的占位插件冒充完成。

| 格式 | 下一步所需信息 | 不自动执行的动作 |
| --- | --- | --- |
| SQL Server `.bak/.mdf/.ndf/.ldf` | SQL Server 版本；备份还是物理文件；是否加密及是否完整文件集 | 启动厂商服务、接受许可、附加库／重建日志或恢复备份；本机 ARM 与官方 Linux x86-64 支持边界也需单独处理 |
| Oracle `.dmp` | Oracle 版本；传统 exp 还是 Data Pump；是否多卷／压缩／加密 | 运行 Oracle 服务端 Data Pump／DIRECTORY／SQLFILE 作业；不能仅凭后缀选择导入器 |
| WiredTiger `.wt`／目录 | MongoDB／WiredTiger 版本、压缩／加密方式、完整一致 checkpoint／metadata／journal 文件集 | 扫描邻近文件、在原目录启动引擎、repair／salvage 或自动升级数据文件 |
| LMDB `data.mdb`／目录 | 生成平台位宽／字节序、LMDB 版本、单文件还是环境目录、named DB／DUPSORT 使用情况 | 使用用户锁文件、未经离线结构验证直接 mmap、把 `.mdb` 自动交给 Access 或把物理页猜成 JSON |

完整目录或引擎依赖方案应走明确授权的隔离 AnalysisJob：冻结副本、源数据只读、受限临时写入、无业务凭据、容量／超时预算、只导出校验后的 artifact。可视化开关本身不授予恢复、修复或目录扫描权限。

## 官方依据

- [MySQL ibd2sdi](https://dev.mysql.com/doc/refman/8.0/en/ibd2sdi.html)：离线 SDI 及未提交读取语义。
- [RocksDB SST 工具](https://github.com/facebook/rocksdb/wiki/Administration-and-Data-Access-Tool)：点记录、序号、十六进制输出与校验。
- [LevelDB 表格式](https://github.com/google/leveldb/blob/main/doc/table_format.md)：数据块、元索引、索引及页脚。
- 其他厂商的具体边界及官方链接见[服务器数据库文件扩展设计](server-database-file-expansion.md)。
