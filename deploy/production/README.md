# 生产发布入口

服务器默认使用 `https://dataseek.4fair.cn/` 和前端端口 `8888`。发布统一从当前版本进入：

```bash
cd /data2/current
./run.sh production
./run.sh production --apply
```

第一条只检查配置及本地镜像；`--apply` 更新前后端。仅需更新前端时使用 `--apply --services frontend`。发布不会构建或拉取镜像，常规更新不重建数据库、缓存、存储及动态分析容器。迁移全部持久数据后首次启动，使用 `./run.sh production --apply --stack`；此模式固定重新创建 MongoDB、Redis、MinIO、两个分析镜像准备服务、后端及前端的七个容器，以更新迁移后的挂载与版本目录标签，不启用可选 Office 服务，不可与 `--services` 或 `--repair-frontend` 混用。它不复制数据，也不修改卷定义。

生产容器可在私有 `.env` 中设置以下显示名称，Compose 项目仍为 `ai-dataseek`，服务名、网络和数据卷保持原身份：

```dotenv
FRONTEND_CONTAINER_NAME=DataSeek-v2
BACKEND_CONTAINER_NAME=DataSeek-v2-backend
MONGODB_CONTAINER_NAME=DataSeek-v2-mongodb
REDIS_CONTAINER_NAME=DataSeek-v2-redis
MINIO_CONTAINER_NAME=DataSeek-v2-minio
SANDBOX_IMAGE_CONTAINER_NAME=DataSeek-v2-sandbox-image
JUPYTER_IMAGE_CONTAINER_NAME=DataSeek-v2-jupyter-image
```

首次更名使用 `./run.sh production --apply --stack`，由 Compose 按现有项目和服务标签替换原来的七个固定容器。未配置时仍使用 `ai-dataseek-<service>-1` 名称；动态分析沙箱和笔记本按会话命名。发布验收核对配置后的实际名称，停服识别新旧固定名称。如果目标名称被其他项目占用，须先调查冲突，不能删除第三方容器。

迁移前可执行 `./run.sh production --stop`。它不能与发布、服务选择或修复参数混用：先停止前后端并核验，再将运行中的动态沙箱文件系统归档到 `/data2/backups/sandboxes/`，全部归档成功后停止这些沙箱，最后停止并核验其余固定服务。固定服务容器和卷不会删除；启用了自动删除的动态沙箱在停止后由 Docker 回收，因此归档必须先完成。新持久卷及完整发布镜像清单尚未准备好时仍可停服；有动态沙箱需要归档时，原清单必须提供匹配的 sandbox 镜像 ID 来核实目标。整个过程持有同一发布锁，归档失败会停止迁移，不继续停止基础存储服务。

生产状态目录固定在 `/data2/state/`：MongoDB 数据及配置、Redis、MinIO、数据集、技能、Jupyter 工作区分别使用 `mongodb`、`mongodb-config`、`redis`、`minio`、`datasets`、`skills`、`jupyter` 子目录。预先创建的外部卷名为 `ai-dataseek-data2-` 加 Compose 卷键；卷使用 local 驱动、`type=none,o=bind`，设备路径只允许 `/data2/state/<目录>` 或 Snap 的 `/var/lib/snapd/hostfs/data2/state/<目录>`。发布前后都核对实际卷配置，并核对服务的实际持久挂载。旧卷保留作为迁移回退来源，发布入口不会删除或自动搬运它们。Jupyter 父卷挂到后端 `/data/jupyter-workspaces`，后端的 `JUPYTER_WORKSPACES_VOLUME`、`JUPYTER_WORKSPACES_HOST_ROOT` 和 `JUPYTER_WORKSPACES_STORAGE_ROOT` 必须对应上述布局。

主机登记数据放在 `/data2/host-datasets/`，允许目录也限定于此；迁移时只更新登记位置，不修改历史分析文本。本站 Nginx 配置放在 `/data2/config/nginx/dataseek.4fair.cn.conf`，系统加载位置 `/etc/nginx/conf.d/dataseek.4fair.cn.conf` 链接到此文件。修改后须先验证配置再重新加载。备份和迁移核验记录放在 `/data2/backups/`。Docker 引擎、镜像缓存以及系统管理的 TLS 证书保持系统原有位置，避免影响共用服务器的其他应用；这些不属于 DataSeek 业务持久数据。

当前入口必须是指向 `/data2/releases/` 下一个版本目录的符号链接。该目录须有 `.env`、`run.sh` 和 `release-images.json`。环境设置必须满足 `FRONTEND_PORT=8888`、`SERVER_HOST=https://dataseek.4fair.cn`、`AUTH_PROVIDER=none`。镜像清单包含 `frontend`、`backend`、`sandbox`、`jupyter` 四项，每项记录明确版本的 `reference` 和完整 `sha256:…` 镜像 `id`；禁止使用 `latest` 或省略标签。整栈启动还要求清单包含 `mongodb`、`redis`、`minio` 三项的明确 `reference` 和完整镜像 `id`，并在启动后核验这些服务的实际镜像、运行状态及 Compose 项目标签。所有七个镜像必须已在服务器本地。更换版本时应在构建完成后明确更新清单与环境配置。

共享非阻塞锁覆盖配置检查、发布和验收。验收核对实际镜像、Compose 标签、端口绑定，以及 IP 和 HTTPS 域名的页面和 API；任何跳转均视为失败。失败后保留现场，不自动回滚或反复重建。锁是维护者共同遵守的约定，无法阻止拥有 root 权限的人直接调用 Docker。

旧 `/root/AI-DataSeek`、`/root/ai-dataseek-current`、`/root/ai-dataseek-releases` 入口已经停用：拒绝发布及所有容器变更，即使继承发布锁也不能调用；只保留状态与配置查询。新的生产发布目录的普通 `run.sh` 入口仅允许 `config`、`ps`、`logs`、`images`、`version` 查询（禁止配置输出写文件）。其他操作必须通过 `production` 入口。内部 Compose 调用继承已持有的共享锁描述符，并核对锁文件身份及独占状态；单独设置环境标记不能绕过。此限制不影响本地普通 Compose 用法。

服务器使用 Snap Docker 时，Snap 启动器无法保留 `/data2` 工作目录。生产入口在权限检查后优先使用同一已安装 Snap 包里的固定 Compose 客户端路径，并对 Docker 查询使用同包直接客户端；不会更改或重启 Docker 服务。本地普通 Compose 行为保持不变。

若指定前端被直接 Docker 操作重建，导致正常 Compose 更新出现同名冲突，可明确执行：

```bash
./run.sh production --apply --services frontend --repair-frontend
```

此操作只允许处理配置的前端名称（`DataSeek-v2` 或默认 `ai-dataseek-frontend-1`，同时兼容旧默认名称）、项目与服务标签匹配、镜像 ID 与清单一致、没有任何挂载且缺少 Compose 管理标记的容器。删除前将脱敏运行信息保存到当前版本的 `.local/production-repairs/`，随后停止并删除该确切容器，再由正常入口创建前端。不会强制删除、删除卷或触碰其他容器。

离线验证：

```bash
python3 -m unittest discover -s deploy/production -v
```
