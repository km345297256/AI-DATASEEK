# 原创物理数据库样本

生成日期：2026-09-13。这里没有用户文件或网络下载的数据库。

- `mysql-8.0.46.ibd`：Ubuntu 固定 MySQL 8.0.46 原生创建 `measurements` 表，包含 BIGINT 主键、VARCHAR、DECIMAL 和二级索引。生成服务器只在一次性测试容器中运行，禁止 TCP、X 协议和文件导入导出；正常关闭后复制独立表空间。`mysql-sdi-oracle.json` 是同版本官方 `ibd2sdi` 的直接输出，不是插件生成的数据。
- `rocksdb-6.11.4.sst`：官方 `SstFileWriter` 写入值、删除标记和含 NUL 的二进制值。`rocksdb-range.sst` 另含范围删除，必须拒绝预览，不能只返回其点记录。`.oracle.txt` 为官方 `sst_dump --command=scan --output_hex --verify_checksum --show_properties` 输出，仅将临时文件路径替换为 `FIXTURE`。
- `leveldb-1.23.ldb`：官方 LevelDB 1.23 `TableBuilder` 写入的无压缩、无过滤块表，物理键由测试代码构造标准 8 字节序号／类型后缀。不是完整数据库恢复样本；用独立 RocksDB 工具回读，得到序号 1、2、3、一个删除标记和含 NUL 的值。

原始哈希保存在 `manifest.json`。MySQL 表空间含引擎分配信息，重新生成可以得到不同文件哈希；更新样本必须同时核对语义 oracle，不能只刷新哈希。

生成器：`../../generate_physical_database_fixtures.py`、`../../generate_leveldb_fixture.cc`。主生成器仅接受空输出目录；LevelDB 样本另外生成。固定测试依赖来自签名 Ubuntu Jammy 仓库：`mysql-server-core-8.0` / `mysql-client-core-8.0` 为 `8.0.46-0ubuntu0.22.04.4`，`rocksdb-tools` / `librocksdb-dev` 为 `6.11.4-3`，`libleveldb-dev` 为 `1.23-3build1`。这些服务器／开发工具不进入生产运行层；生产只复制 `ibd2sdi`、`sst_dump` 两个离线程序及其版权说明。

这些证据不涵盖 MySQL 8.4、加密／压缩／非 16 KiB 表空间、完整 RocksDB/LevelDB 目录、Snappy LevelDB 或任意厂商备份。
