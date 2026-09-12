# 原创 BSON / Redis RDB 预览样本

所有数据均为本项目测试生成，不包含用户数据库或第三方下载的数据集。

- `synthetic-pymongo-4.17.0.bson`、`empty-document.bson`：官方 PyMongo 4.17.0 BSON encoder 原生生成；涵盖 int64 极值、Decimal128 精度/量级/尾零、超范围毫秒日期、逻辑时间戳、ObjectId、null/空字符串、二进制、嵌套对象/数组、JS/regex 安全标记。生成器为 `sandbox/tests/generate_database_records_fixtures.py`。
- `synthetic-rdb11-wire.rdb`、`empty-rdb11-wire.rdb`：项目自建 wire-format 单元测试数据，不作为原生 Redis 兼容性的独立证据。
- `native-redis-7.2.7.rdb`：Redis 7.2.7 原生 SAVE 生成，346 字节，SHA256 `d7853cacc4bbee20f5d632226ac309ce3b913da47a669f2bfa373426431fed69`。镜像为 `redis@sha256:96af50b9ce0cd7f44f73266ce90bb63a6d6d5655adb8854d2723a0382524c8f3`，RDB 11。含 string/list/set/hash/zset、DB 0/2、int64 级精确文本、长期过期时刻、原生 LZF 压缩、空字符串、Unicode；已由原生 `redis-check-rdb` 和本项目 reader 分别验证。

原生 Redis 生成器 `generate-native-redis.sh` **只能**运行在明确授权的一次性测试容器内。通过项目 `./run.sh` 附加独立临时测试 service，`network_mode:none`、`--port 0`、私有 Unix socket、只读根文件系统、仅 `/tmp` tmpfs、256 MiB 内存、CPU 1、64 PID、60 秒超时，只挂载生成器单文件，不继承业务 Redis 的数据卷、网络、环境或密钥。生成器自行停止其 Redis；`run --rm` 删除测试容器。此轮已确认精确测试容器 `dataseek-records-fixture-20260911` 不再存在。

Redis SAVE 包含生成时刻及引擎选择的记录顺序，重生成不保证文件哈希不变，应以逻辑值、类型、分组、过期时刻和 CRC64 作为兼容性验收；更新固定 fixture 时需同步测试中明确记录的哈希。无需将 Redis 服务或其服务器二进制安装到预览 sandbox。

许可参考：PyMongo [Apache-2.0](https://github.com/mongodb/mongo-python-driver)；Redis 7.2.7 [BSD-3-Clause](https://github.com/redis/redis/blob/7.2.7/COPYING)。样本本身是原创测试数据，不是复制上游数据库文件。
