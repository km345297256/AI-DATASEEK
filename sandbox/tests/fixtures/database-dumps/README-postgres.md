# PostgreSQL 原生原创归档样本

这些文件全部来自本项目自创的两张小表，不包含用户数据，不是从网络下载的业务数据库。独立 oracle 是官方 `pg_dump` 写入器和 `pg_restore --list`，不是我们的 TOC 解析器自己生成的数据。

## 固定环境

- 生成日期：2026-09-11。
- 工具：`pg_dump (PostgreSQL) 18.6 (Debian 18.6-1.pgdg13+2)`。
- 官方镜像：`postgres:18.6`，本次 Linux ARM64 实际镜像／仓库 digest：`sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280`。
- 格式：PostgreSQL archive **1.16.0**，4 字节整数、8 字节 offset、UTF8。
- 原创生成器：[generate-postgres-fixtures.sh](generate-postgres-fixtures.sh)。不传入外部 SQL，不读取用户数据，不恢复输入归档。
- 公开工具来源：[PostgreSQL 18.6 发布源码](https://www.postgresql.org/ftp/source/v18.6/)、[官方容器定义](https://github.com/docker-library/postgres)。使用 PostgreSQL 工具作测试生成器不等于给生产安装 PostgreSQL 服务。

本次通过 `./run.sh -f <临时测试 override> run --rm --no-deps pg-dump-fixture` 运行。override 只添加无业务卷的测试服务：`network_mode:none`、只读根文件系统、640 MiB 内存、1 CPU、96 进程、180 秒期限、256 MiB `/tmp` tmpfs；仅挂载生成器只读与本目录作为样本输出。服务初始化新临时 PGDATA，禁用 TCP，只使用新建目录的 Unix socket。容器退出后删除，不更改生产镜像 tag、现有数据库或正常 Compose 服务。

生成器拒绝覆盖已存在样本。时间戳、OID 分配和 PG18 `\restrict` 随机标记会导致重生成的字节 hash 不同；“可重现”指步骤和结构／数据语义可复核，不承诺 bit-for-bit 相同。

## 文件与 oracle

| 文件 | 实际内容 | SHA-256 |
| --- | --- | --- |
| `synthetic-postgres-none.dump` | 未压缩 custom | `b2e2db08384a26283e98d0e51aca30f8a945d43f9c43426b39bf52a4a1b8261f` |
| `synthetic-postgres-gzip.dump` | gzip custom | `453fec696badec68d8f63565989de63397c359ce03ac7a85d2811ecc34d62d8f` |
| `synthetic-postgres-lz4.dump` | LZ4 custom | `f6d40797c8663c60e10d7f08fca2e08467d99d78670bcf0991485a171846f83c` |
| `synthetic-postgres-zstd.dump` | Zstandard custom | `410ac9f2bdc33c1dda9cd724da8ce4e60139b05900073c2bbfab3f964bf2a91b` |
| `synthetic-postgres.tar` | 官方普通 tar | `6d519a3b1d183310211536386cbfcd83d4e37f3af4f74ec10d5095c023579b82` |
| `synthetic-postgres-identifiers.dump` | 中文／空格／危险路径名及测试注释 | `aea0db5e697a80228841ab3b79fdd6b6b819a6ebbe55a098a0e6fbd8365029f4` |
| `synthetic-postgres.sql` | 相同基础表的官方 plain dump | `88c5df89c0f365df872baca3491b8d31f0f387ca2578f1f9bd7e300a3bd90b20` |
| `synthetic-postgres-rows.json` | 原生查询，Int64／定点数显式转文本 | `b12328721ed65697f6def6d1a75549e8ec8d5638aa38a2a462af54f8b7e78a75` |

配套 `.list` 文件均为未修改的官方目录输出，只含本次测试的数据库名和用户 `postgres`，不能直接返回前端。浏览器 payload 来自经过隐藏／约束的 reader 字段，生成脚本见 [generate_pg_dump_browser_fixture.py](../../generate_pg_dump_browser_fixture.py)。

基础归档完整 TOC 有 8 条记录：2 条 TABLE、2 条 TABLE DATA，其他为配置／数据库元数据。`pg_restore --list` 默认只列出本次需要恢复的 4 条表相关条目；测试同时核对头部 TOC 总数和这些明确的原生对象。含空格的列表不能一般性拆词，因此中文／空格名字还与生成器明确创建的结构对照。数据库及 COMMENT 等复合 tag 固定隐藏。

## 验证范围

实际原生验证：**PG18.6 写出的 1.16.0**，上述 4 种 custom 压缩标识与 tar。读取器不解压数据段，仅读未压缩 TOC，通过目录测试不证明压缩数据完整或可恢复。

1.14.0 和 1.15.0 按固定官方源码格式顺序做了结构／畸形输入测试，但本轮没有用对应历史 `pg_dump` 再生成原生样本。这是实现支持、原生兼容性证据较窄的子集；不能描述为 PG12～PG18 所有版本及选项都已验证。

`test_pg_dump_reader.py` 覆盖每个 TOC 截断点、错误版本／编码、字段／对象／依赖上限、重复 ID、offset、tar checksum／路径别名／链接／特殊成员／尾部字节、标签隐藏和随机扰动。`test_pg_dump_native_fixtures.py` 强制校验样本 hash、原生目录 oracle、前端 fixture 与 reader 输出一致性；无需用户安装 PostgreSQL。
