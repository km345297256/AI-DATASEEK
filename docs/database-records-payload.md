# BSON / Redis RDB 只读记录预览载荷

本文冻结新增 `database-records` 适配器的内部载荷；公共可视化协议仍为 v2，两个独立 reader 为 `bson` 与 `redis-rdb`。不复用关系表的 SQL / 引擎原生标量语义。

## 请求

- `tree`：`options={}`，只返回分组与完整扫描后的类型计数，不返回键名或文档字段。
- `table`：`options={group_id,offset,limit}`，`offset` 为 0–100000，`limit` 为 1–50。由主机继续强制绑定文件版本；只接受当前目录中的 opaque 分组 ID。
- 输入为单个未压缩文件，最多 16 MiB，输出最多 2 MiB。不是服务器连接、备份恢复或数据库管理接口。

## 顶层固定结构

```text
{contract_version:2, type:reader, reader:"bson"|"redis-rdb", kind:"tree"|"table",
 media_type:"application/json", choices:{groups:[Group]}, selected:Options,
 metadata:Metadata, warnings:[固定说明], sampled:boolean, tree:[...] | table:Page}
Group = {id:"g-"+24位hex, label:"Documents"|"Redis DB n",
         database:null|整数, record_count:整数, counts:{类型名:正整数}}
tree = [{path:"/"+group.id, node_type:"group",
         attributes:{label:group.label,records:group.record_count}}]
Page = {group_id,offset,limit,has_more:boolean,records:[Record]}
Record = {index:十进制序号文本,key:Cell|null,expires_at_ms:有符号int64文本|null,
          nodes:[Node],truncated:boolean}
Node = {id:连续整数,parent:前置父索引|null,key:字符串|null,key_omitted:boolean,cell:Cell}
```

`Node` 是前序扁平树，根节点 `id=0,parent=null,key=null`；其他节点的 parent 必须早于自己且必须是容器。BSON 字段名和 Redis hash 字段名仅做惯性文本显示，不是对象属性或路径。超长、不安全字段名显示“字段名已省略”并令 `key_omitted=true`。数组/集合位置使用十进制字符串。容器实际直属子节点数必须等于 `cell.count`；超过结构预算的文件整体拒绝，不能悄悄丢失子节点。

节点必须保持真正前序，不能返回已经结束的分支追加子节点。BSON 仅允许 document/array 容器，数组索引从 0 连续；Redis list/set/hash 只允许字节字符串叶节点，zset 只允许固定的 entry → member/score 结构，不接受嵌套 BSON 对象、伪造整数或 NaN score。可见字段名必须唯一，但不同被省略的原始字段可共享明确的显示占位词，不因脱敏而误报重名。Decimal128 的 `value` 会根据原始 BID 独立推导并精确比对，拒绝不一致的数值、符号或尾零；验证器不依赖浮点数。

## Cell 精确联合类型

| type | 其余字段 | 说明 |
|---|---|---|
| null | value:null | 与空文本不同 |
| boolean | value:boolean | 不做数值转换 |
| integer | value:十进制字符串 | BSON int32/int64、Redis 整数编码仍保持精确 |
| real | value:有限 JSON number | 源文件 binary64 数值 |
| nonfinite | value:"NaN"\|"Infinity"\|"-Infinity" | 不伪装为空值 |
| decimal128 | value:官方 Decimal128 文本,bid:32位小写hex | 保留十进制语义和原始 128 位表示 |
| text | value:字符串 | ≤512 UTF-8 字节，纯文本呈现 |
| date-ms | value:有符号int64十进制字符串 | 原始 UTC Unix 毫秒，前端不可先转 Date 再覆盖它 |
| timestamp | seconds:uint32文本,increment:uint32文本 | MongoDB 逻辑时间戳，不是普通日期 |
| objectid | value:24位小写hex | 不推断成业务时间 |
| binary | bytes:整数,subtype:null\|两位小写hex | 不发送或解码二进制内容；Redis 非 UTF-8 字节也是 binary |
| omitted | bytes:整数,reason:"text-budget"\|"unsafe-text" | 明确标识未显示的文本 |
| unsupported | name:"regex"\|"javascript"\|"javascript-scope"\|"dbpointer"\|"symbol"\|"undefined"\|"min-key"\|"max-key" | 不执行，也不把源表达式传给浏览器 |
| document/array/hash/list/set/zset/entry | count:非负整数 | 容器；`entry` 用于 zset 成员及 score |

Redis 原始字符串即使由 RDB 整数压缩保存也仍是 `text`，不猜测业务数据类型；仅 zset score 为真实浮点数。不会按当前时钟过滤过期键，`expires_at_ms` 始终保存文件中的时刻。

## Metadata 与预算

```text
{engine:reader,format:"bson"|"rdb",container:"BSON document stream"|"Redis RDB",
 input_mode:"whole",source_bytes,format_version:null|整数,
 ordering:"bson-document-order"|"rdb-file-order",
 total_records,groups_count,records_returned,nodes_returned,
 omitted_values,unsupported_values,checksum:"not-applicable"|"verified",
 limits:{max_groups:32,max_records:100000,max_rows:50,max_offset:100000,
         max_depth:8,max_record_nodes:128,max_page_nodes:4096,max_scan_nodes:100000,
         max_text_bytes:512,max_document_bytes:1048576,
         max_decompressed_block_bytes:1048576,max_decompressed_bytes:33554432}}
```

完整扫描验证所有记录，即便分页只返回一部分；只保留当前页的节点，不整文件构造 JSON 文档树。`sampled` 当分页不包含完整分组，或有记录 `truncated=true` 时为真；记录中出现 omitted 字符串或省略字段名即置 `truncated=true`，unsupported 独立标明，不冒充已完整解释。

## 首版格式边界

- BSON：MongoDB `mongodump` 单 collection `.bson` 连续文档流；不是 `.archive`、`.bson.gz` 或 WiredTiger `.wt` 存储页。结构先行检查长度、嵌套、重复字段、数组索引；只用官方 PyMongo 4.17.0 的 Decimal128 值类型解码固定 16 字节，不创建 MongoClient。
- Redis：固定 RDB 11 子集，string/list/set/hash/zset，原始值、整数编码、LZF、intset/listpack/quicklist2；旧版压缩编码或 streams/modules/functions 出现时明确拒绝整个文件。先限制声明尺寸再解压，校验全部结构、末尾和 CRC64。零校验和文件也拒绝，不把未验证称作通过。
- 验证样本为原创、固定版本原生工具生成；不恢复用户备份，不读取真实服务文件，不启动长期服务。
