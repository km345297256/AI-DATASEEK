# 文档 / 键值数据库离线预览评估

核对时间：2026-09-11。目标是 Cordis 可独立启停的科学数据文件预览，不引入数据库恢复、管理或在线连接能力。

| 工具/格式 | 选择与边界 |
|---|---|
| MongoDB BSON / PyMongo 4.17.0 | 使用官方 PyMongo 的 `Decimal128.from_bid()` 固定 16 字节解码；文档结构由受限扫描器先检查，不调用整文档 `decode_all()`，不创建 MongoClient。Apache-2.0。单 collection BSON 文档流可以预览；`.archive`、`.bson.gz`、WiredTiger `.wt` 不等同于 BSON。 |
| Redis RDB / Redis 7.2.7 原生样本 | 首版严格支持 RDB 11 的 string/list/set/hash/zset，以及 raw/integer/LZF/intset/listpack/quicklist2 编码。自有受限 Python 适配器不安装 Redis 服务器、不发出 RESTORE/RESP、不通过当前时钟丢弃过期键。Redis 7.2.7 原生工具仅在断网一次性测试容器内生成原创样本；其代码许可为 BSD-3-Clause。 |
| redis-rdb-tools | 采用 MIT 的事件回调思路可参考，但不直接采用其完整 JSON 输出或默认解压路径。其显示层的 bytes/Unicode 转换与本项目精确、明示省略的协议不同，压缩尺寸/总展开量也必须由适配器先限定。 |
| redis/librdb | 官方 SAX/reader/handler 分层和可替换 allocator 值得后续参考。该项目同时提供 RESP 和 Redis 重新导入扩展；本轮不引入这些能力，不将通用 CLI 暴露给预览。若未来支持 stream/module 等格式，应编写只读专属回调和内存预算适配，而不是直接运行 `rdb-cli ... redis`。 |
| WiredTiger / RocksDB / LevelDB 原始目录 | 不是单一通用文档格式；常依赖 MANIFEST、多个 SST/日志、一致性快照和引擎版本。此轮不把文件扩展名当成兼容证明。建议用户先导出 BSON/JSON/关系表；未来可按引擎独立增加“目录元信息”插件，但不能承诺孤立存储页可完整恢复逻辑文档。 |

## 安全与精度落地

内部载荷详见 [database-records-payload.md](database-records-payload.md)：公共协议 v2 不变，BSON 和 Redis 各有独立 reader/plugin，首次只返回目录计数，手动分页才返回扁平 typed 树。整数、Decimal128、UTC 毫秒日期与 MongoDB 逻辑时间独立表达，二进制只发送长度及 subtype，不转换成浏览器浮点数或执行对象。

扫描最多 16 MiB；单 BSON 文档 / RDB 字符串块最多 1 MiB；LZF 解压前先验证长度，单块最多 1 MiB，总展开预算 32 MiB；每条最多 128 个节点、深度 8、全扫描最多 100000 个节点、每页最多 50 条且 4096 个节点，输出 2 MiB。结构超额、重复字段/键、非法数组索引、损坏长度/回引用/CRC64、未知 Redis 编码或 module/stream/function，均明确拒绝整个文件，不返回“部分成功”的假完整结果。过长或不安全显示文本才使用明确 omitted 节点。

RDB 检查必需非零 CRC64、EOF 与末尾长度；此轮原生 Redis 7.2.7 SAVE/redis-check-rdb 样本已通过五类对象、分组、LZF 和精度验证。具体生成方式、数据来源和哈希见 [样本说明](../sandbox/tests/fixtures/database-records/README.md)。这不是对所有 Redis 版本/编码或 MongoDB 存储文件的支持承诺。

## 一手参考

- [MongoDB BSON specification](https://bsonspec.org/spec.html)
- [MongoDB BSON types](https://www.mongodb.com/docs/manual/reference/bson-types/)
- [MongoDB Decimal128 specification](https://github.com/mongodb/specifications/blob/master/source/bson-decimal128/decimal128.md)
- [MongoDB Python driver](https://github.com/mongodb/mongo-python-driver)
- [Redis 7.2.7 release](https://github.com/redis/redis/releases/tag/7.2.7)
- [Redis 7.2 RDB format constants](https://github.com/redis/redis/blob/7.2/src/rdb.h)
- [Redis listpack encoding](https://github.com/redis/redis/blob/7.2/src/listpack.c)
- [Redis CRC64 implementation and check vector](https://github.com/redis/redis/blob/7.2/src/crc64.c)
- [redis-rdb-tools source and MIT license](https://github.com/sripathikrishnan/redis-rdb-tools)
- [redis/librdb official parser](https://github.com/redis/librdb)
