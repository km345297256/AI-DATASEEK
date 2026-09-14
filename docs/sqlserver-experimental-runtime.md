# SQL Server 本机仿真实验入口

2026-09-14。用户已明确授权本机 x86-64 仿真实验，仍不开放公网、不自动恢复或修改用户数据库。

## 本次实现

在唯一 `docker-compose.yml` 中新增 `sqlserver-reader-probe`，仅属于显式选择的 `database-readers` profile。默认 `./run.sh up` 不启动它，后端也不依赖它。不新建 Compose 项目或前端端口。

固定官方 SQL Server 2022 CU26 镜像及经微软仓库核对的摘要：

```text
mcr.microsoft.com/mssql/server:2022-CU26-ubuntu-22.04
sha256:ba4c8329f48fb8f02e1416be6a930ebfd71268caee78aa985f3af4315e457c89
```

运行边界：`linux/amd64`、非 root、只读根文件系统、无 Linux capabilities、禁止提权、无网络、无端口、无数据卷和主机目录、无 Docker socket。临时数据只在有界 tmpfs 中；4 GiB 内存、2 CPU、256 进程，入口独立硬限制 180 秒。

入口每次在内存生成临时 SA 密码，不读取业务凭据，不把密码放在命令行、输出或项目配置中。只执行固定 `SERVERPROPERTY('ProductVersion')` 查询；不接收 SQL、文件路径、连接串、参数或标准输入。引擎创建的是全新临时系统库，不是用户数据恢复。SQL Server 退出／超时／版本不符则验证失败，并终止进程。

返回 `ready` 只表示该候选引擎在本机能启动、登录并返回预期主版本。始终返回 `file_preview_available:false`；**不是备份预览完成，也不据此新增 Cordis 占位插件**。后续 HEADERONLY／FILELISTONLY 的结果仍需严格字段投影、版本／权限绑定、真实样本和独立结果验证；耗时操作通过受控任务接入。

## 运行

先确认无正在执行的分析任务和足够的可用内存。仅使用既有 `run.sh`：

```bash
./run.sh --profile database-readers build sqlserver-reader-probe
./run.sh --profile database-readers run --rm --no-deps -T sqlserver-reader-probe
```

成功退出码为 0，失败为 1；硬超时也可能返回非 1 的失败码。任何非零结果均不得将读取器置为可用。`--rm` 清理该次容器及临时数据库；已下载镜像保留供后续复用。不要修改生产 MongoDB、Oracle 或 SQL Server 配置来满足实验启动。

## 验收状态

固定镜像已成功构建。新增安全测试 6 项通过；前端类型检查／构建通过；完整后端回归 5280 项通过、32 项跳过、190 项子测试通过（新增日志尾部脱敏测试随后单独通过）；完整沙箱回归 5872 项通过、563 项子测试通过。Compose 配置和补丁空白检查通过。

**首次真实启动被运行器的仿真兼容性阻塞；启用 Rosetta 后已通过启动验证（见下节），文件预览仍未接入。** 首次诊断过程：

1. 官方 `sqlservr` 带有 `CAP_NET_BIND_SERVICE+ep` 文件能力，与 `cap_drop: ALL` 组合触发 `EPERM`。1433 是非特权端口，因此仅在派生镜像中移除该多余文件能力，未增加任何容器权限。
2. 修正后引擎报告 `Invalid mapping of address`。输出只保留固定错误码 `unsupported_virtual_address_layout`；原始日志仅暂存容器内临时文件，最多读取末尾 64 KiB 作固定分类，不返回路径或日志正文。
3. 容器栈软限制为 8 MiB，`legacy_va_layout=0`、`randomize_va_space=2`，未修改主机或容器内核参数。
4. 当前 Docker context 是 **Colima**，不是 Docker Desktop。Colima 0.10.3 使用 `vmType: vz`、`arch: aarch64`，但 `rosetta: false`。下一步可按[Colima 官方配置](https://colima.run/docs/configuration/)启用 Rosetta 后重新验收；这只是下一项兼容性尝试，并不保证 SQL Server 一定成功启动。

启用 Rosetta 需要重新启动当前 Colima 实例，会暂时中断 DataSeek、MongoDB、Redis、MinIO 和 Office 服务，因此首次试验未擅自修改该设置。后续用户已授权维护窗口，执行记录如下。不另建虚拟机或第二套 Compose，不改变现有存储挂载。

所有试验容器使用 `--rm` 清理，临时系统库与日志随之删除；下载的候选镜像保留。SQL Server 2019／2025、Oracle、WiredTiger、LMDB 不因本入口而成为已支持格式。

### 授权维护与 Rosetta 复测

2026-09-14 08:55–08:57（Asia/Shanghai），用户明确允许重启后执行：

- 重启前确认无活动分析任务，记录业务摘要，备份原 Colima 配置至本机临时目录 `dataseek-rosetta-config.jZHqkV`（目录 0700、文件 0600）。
- 使用既有 `run.sh` 平稳停止服务，再停止现有 Colima；使用 `colima start --vz-rosetta` 启用 Rosetta，沿用原实例和磁盘。
- 确认 `rosetta: true`；原有 6 CPU、12 GiB 内存、80 GiB 磁盘、aarch64 和 vz 保持不变。
- 使用 `run.sh --profile office start` 恢复原容器，没有重建应用镜像、删除容器或数据卷。
- SQL Server 隔离入口复测返回 `ready:true`、`version:16.0.4265.3`、`experimental:true`、`file_preview_available:false`、`user_files_read:0`、`restore_calls:0`，退出码 0。临时容器已自动删除，没有新增常驻 SQL Server 服务。
- DataSeek API 恢复，仍由 Cordis 返回 83 个已注册插件；前端仅绑定 `127.0.0.1:7001`。ONLYOFFICE `/healthcheck` 返回 `true`。
- 维护前后业务摘要完全相同，已有文件、数据集、会话、事件、分析作业、产物、模型轨迹与 token 记录未变。

本次完成的是运行环境兼容性验证，不等于 `.bak` 可视化已上线；后续仍需实现备份目录读取、统一协议适配与真实文件验收。

官方依据：[固定镜像目录](https://mcr.microsoft.com/en-us/product/mssql/server)、[环境变量](https://learn.microsoft.com/en-us/sql/linux/sql-server-linux-configure-environment-variables?view=sql-server-linux-ver17)、[CPU 与仿真支持限制](https://learn.microsoft.com/en-us/sql/linux/containers/deploy?view=sql-server-ver17)。本实验不改变微软不支持 ARM 仿真的事实。
