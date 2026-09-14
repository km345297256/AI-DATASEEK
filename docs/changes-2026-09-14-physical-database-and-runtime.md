# 物理数据库只读预览与实验运行入口更新

日期：2026-09-14；提交分支：`v2`；对比基线：`2f5f10484e558004ca4c2abea353ed5c33fe01bd`。

## 改动摘要

### 1. 两个独立 Cordis 插件

- `viz-mysql-sdi`：读取受支持 MySQL `.ibd` 文件中的 SDI 表、列和索引目录，不读取业务行，也不承诺事务一致性。首版原生样本为 MySQL 8.0.46，限定未加密、未压缩的 16 KiB 独立表空间及已支持的字典标识。
- `viz-sst-records`：读取受支持 RocksDB `.sst/.ldb` 的物理点记录，以十六进制展示键值，精确保留序号和删除／合并标记；另支持严格受限的 LevelDB 1.23 原生单表子集。它不合并层、WAL 或 tombstone，不代表数据库当前逻辑状态。
- 两者共用新增的 `physical-database` 受信适配器。总量从 81 个插件／76 个适配器增至 **83 个插件／77 个适配器**，公共契约仍为版本 2。

新增后端分派和独立载荷校验、沙箱读取器、LevelDB 块结构校验器、前端预览组件、搜索分页、生命周期测试及原生合成夹具。契约继续由单一规范生成到前端、后端和 Cordis host。

### 2. 读取边界与效率

- 输入最多 16 MiB，旧 LevelDB 分支最多 4 MiB；结果最多 1024 条、2 MiB。超限拒绝，不把截断记录冒充完整结果。
- 原生读取只调用固定的离线 `ibd2sdi` 和 `sst_dump`，不接收任意参数、SQL、路径或命令；镜像保留工具来源和许可文件，不带入 MySQL 服务端运行入口。
- 保留文件所有者、版本、Cordis revision、数据集路径 allowlist 与只读挂载边界。原生诊断和宿主路径不返回浏览器。
- 前端每页 50 行，搜索与翻页使用已验证结果，不额外读取文件或调用模型；切换文件、停用插件及卸载时取消旧请求。
- 保留既有 AgentLoop、PlanActFlow、SSE、FastAPI 和 Docker 隔离架构，不添加数据库管理平台或自动恢复操作。

### 3. SQL Server 实验入口（不是文件预览插件）

在唯一 Compose 栈增加显式 `database-readers` profile 下的 `sqlserver-reader-probe`。默认启动不运行，后端不依赖它；固定 SQL Server 2022 CU26 官方镜像摘要及 amd64 平台。

入口使用临时内存凭据，只查询固定的引擎版本。容器无网络、无端口、无用户数据卷，非 root、只读根文件系统、禁用 capabilities，并限制内存、CPU、进程和总时长。诊断仅输出固定分类，不透传日志正文。

`ready` 只表示候选引擎可启动，始终明确 `file_preview_available:false`。本批没有实现 `.bak/.mdf` 预览、恢复或附加；Oracle、WiredTiger、LMDB 等仍属于后续准入方案，不能将目标版本清单当作已支持清单。

### 4. 打包与测试维护

- 新增可选 `frontend/Dockerfile.prebuilt` 及专用忽略文件，通过 `FRONTEND_DOCKERFILE` 选择，默认前端 Dockerfile 不变。仅用于打包已完成类型检查和构建的新 `dist`，不提交构建输出。
- 扩充真实 HTTP 验收脚本的安全断言和插件目录数量检查。
- 修正 DuckDB 测试对全局时钟的替换，改为仅模拟被测模块，避免干扰并行 HTTP 测试；不是生产 DuckDB 行为变更。
- 附带原创 MySQL／RocksDB／LevelDB 二进制夹具、原生对照结果、哈希及生成代码，不包含用户数据库。

## 本次提交前验证

以下仅记录 2026-09-14 本次重新执行的检查，不与其他任务的历史验收重复累计。

| 检查 | 结果 |
| --- | --- |
| 前端类型检查、单元测试、生产构建 | 通过；2,652 项测试通过、0 跳过；保留现有大分块等构建提示 |
| Cordis host 构建及测试 | 80 通过，0 失败，0 跳过 |
| 后端完整回归 | 5,281 通过、32 项条件跳过、190 个子测试通过；0 失败 |
| 沙箱完整回归 | 5,872 通过、0 失败、0 跳过、563 个子测试通过；40 条依赖／参考库警告 |
| 契约生成一致性 | 77 个适配器校验通过 |
| Compose 配置 | `./run.sh config --quiet` 通过 |

后端和沙箱使用既有镜像的一次性容器、只读源码及临时测试参考库。后端隔离真实模型和业务存储；不修改生产依赖，不重建或更新服务。本次不运行 SQL Server 引擎，不修改 Colima／Rosetta，不进行在线上传或完整浏览器端到端复验。

随附集成文档记载的历史 HTTP、浏览器、部署和授权维护结果仍保留其原范围，不作为本次重新执行的证据。

## 提交范围与相关文档

提交实现、测试、合成夹具、部署配置和 MD 文档。保留且不提交本地备份 `frontend/src/visualizations/extended/DocumentPreview 2.vue`；不纳入环境密钥、测试日志和构建产物。

本次共 64 个文件。原改动在验证期间保持稳定，未发现疑似密钥，原生样本及对照文件哈希与 manifest 一致。两个 `*.sst.oracle.txt` 文件包含原生工具生成的尾随空格，保持原文和哈希；排除这两个对照文件后，其余暂存差异空白检查通过。

- [物理数据库集成与格式限制](physical-database-integration.md)
- [SQL Server 实验入口与历史验收](sqlserver-experimental-runtime.md)
- [数据库主流版本准入方案](database-mainstream-version-plan.md)
- [原生夹具来源](../sandbox/tests/fixtures/physical-database/README.md)

本次仅提交推送到 `v2`，不修改 `main`，不部署。
