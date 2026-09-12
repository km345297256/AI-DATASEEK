# SQLite 受限只读表预览

本插件是科学数据容器的只读检查工具，不是数据库控制台。不改变 AgentLoop、PlanActFlow、模型工具目录或 SSE。通过统一 Cordis 文件预览入口，用户可单独启停 `viz-sqlite-table`；适配器／读取器均为 `sqlite-table`，不支持共享预览。

## 实际支持范围

- `.sqlite`、`.sqlite3`、`.db`，必须有真实 `SQLite format 3\0` 签名，不能只凭后缀进入解析。
- **完整单文件 ≤16 MiB**，输入先取回，再在无网络、非 root、只读根文件系统的既有隔离 worker 中复制到专属临时目录。不是 8 GiB 分块读取，也不是远程 SQLite VFS。
- UTF-8 数据库、完整页大小与文件头长度一致、现代有效页数／版本计数一致。文件头读写版本必须均为 `1`，**WAL 模式直接拒绝**；不读 `-wal`、`-shm` 或 journal。应先使用数据库工具导出／完成 checkpoint，提供静止且自包含的快照，不把正在写入的主文件当成完整事务快照。
- 普通 **rowid 表**，最多 32 表，每表 128 列；表名与列名仅接受 ASCII 标识符 `[A-Za-z_][A-Za-z0-9_]{0,63}`。
- 不提供系统表、视图、触发器、虚拟表、生成列或 WITHOUT ROWID 表。文件中存在上述非普通对象会明确拒绝，不悄悄忽略它们后称数据库已完整支持。
- 普通索引可存在；固定表查询 `NOT INDEXED` 且只按隐藏 rowid 升序，不执行表达式索引或用户指定排序。若 `rowid`、`_rowid_`、`oid` 三个名称全部被用户列遮蔽则拒绝。rowid 仅是当前不可变快照中的稳定键，不是跨导出／VACUUM 的永久身份。

## 用户流程与数据语义

首次 `kind:tree`、`options:{}` 仅查询表结构，不执行用户表的行查询，也不执行 `COUNT(*)`。由于输入是 whole 模式，**首次仍已取回整个文件**；“只查目录”不表示文件字节仅取回目录页。

用户明确选表、选列、起始行和页大小之后，发出 `kind:table`，必须带上目录返回的文件 `version`。目录／文件／插件状态变化后取消旧请求并清空旧显示。编辑选项本身不自动重读；同一目录版本的轮询不会重新挂载。没有 SQL 输入框。

```json
{
  "plugin_id": "viz-sqlite-table",
  "operation": "preview",
  "kind": "table",
  "version": "<文件版本>",
  "options": {
    "table": "<目录返回的不透明表标识>",
    "columns": [0, 2],
    "row_offset": 0,
    "row_limit": 50
  }
}
```

每页最多 200 行 ×16 列，起始偏移最多 100,000；固定查询最多取 `limit+1` 行来确认是否还有下一页。偏移可能需要顺序跳过前面的行，**不是常数时间随机分页**。不预先统计全表行数，空页明确显示为空，不偷偷改动起始行。

列目录显示 SQLite **类型亲和性**与 `table_xinfo` 的声明约束，不把列声明当作每个值的实际类型。`table.rows` 使用本读取器专用的类型化单元格：

| 存储内容 | 返回与显示 |
| --- | --- |
| INTEGER 与 rowid | 完整有符号 int64 的十进制文本，含超出 JavaScript 53 位安全范围的整数；不转为浮点 |
| REAL | 保留 SQLite 实际浮点值；非有限值专门标记，不冒充 SQL NULL |
| TEXT | 最多 512 UTF-8 字节，不转换日期、JSON、单位或数值字符串 |
| 超长／不安全 TEXT | 只返回字节数与明确的省略原因，不截断并假装完整值；路径、控制字符不透传 |
| BLOB | 只返回字节数，不返回原文、Base64，不落盘解码 |
| NULL | 独立类型标识，不混同空字符串、0 或非有限实数 |

无效 UTF-8 文本直接拒绝，不用替代字符悄悄修改原值。HTML 字样通过 Vue 文本插值展示，不能作为脚本、标签、链接或媒体执行。没有连接、聚合、统计、模型调用、归一化或自动类型转换。

## 原生解析安全预算

沙箱 Python 3.10 的标准 `sqlite3` 没有 Python 3.11 才增加的连接 `setlimit()`，因此仅在 sandbox 固定安装 **APSW 3.53.4.0**，使用公开 SQLite API，不读取 CPython 私有连接指针。后端只运行同源纯 Python 载荷验证器，不加载 SQLite 或 APSW。

- 开连接使用 `mode=ro&immutable=1&cache=private`；专属临时目录内只有本次字节生成的 `snapshot.sqlite`，正常／异常后均删除。原数据集文件不可写。
- 先设置 `DEFENSIVE=1`、`TRUSTED_SCHEMA=0`、关闭 view／trigger／扩展加载及双引号字符串兼容；移除全部虚拟表模块。
- SQLite 原生连接限额：单字符串／BLOB／编码行 `LENGTH=64 KiB`、SQL 8 KiB、128 列、表达式深度 10、VDBE 指令分配 10,000、禁止 ATTACH、最多 2 绑定变量。超过支持预算的记录可能在读取选定字段之前即被 SQLite 拒绝；不承诺选少数列就能绕过超大记录。
- schema 最多 128 个条目、单条 SQL ≤8 KiB、累计结构文本 ≤64 KiB，不返回 CREATE SQL、默认表达式或触发器源码。
- 只允许固定受控 SQL：限定主文件表／列的读取，以及 `typeof`、`length` 两个函数；拒绝写入、ATTACH、外部读文件、加载扩展和额外 PRAGMA。用户只提交不透明表 ID、列序号和两个分页整数。
- 原生 SQLite allocator 的 **32 MiB hard heap limit**、1 MiB 目标缓存、关闭 mmap、内存临时存储；不是整个 Python worker 的精确 RSS 限制。
- 约每 1,000 个 SQLite VM 指令调用一次 progress handler，累计超过 2,000 次或 15 秒中断。回调计数不冒称完整精确指令数；现有隔离 worker 另有内存／CPU／墙钟时限和取消回收。
- 类型化输出及公有封装继续受 **2 MiB** 上限，宿主校验选择、格式、源长度、表目录、行键顺序与所有单元格类型。没有扩大旧通用 table／数组／字符串的预算。

这不是全数据库一致性或完整性检查器。没有扫描验证所有表的全部数据；仅对当前结构与明确分页执行受控读取，不声称拒绝了所有历史损坏或业务约束问题。

## 验收入口

- `sandbox/tests/test_sqlite_table_reader.py`：使用系统标准库 SQLite 写入和读取的独立真实数据库，与 APSW 受限读取对照；签名／WAL／schema／巨型字段／UTF-8／权限／限额／清理测试。
- `backend/tests/test_sqlite_table_visualization.py`：纯类型化载荷、严格请求绑定、格式／长度／输出预算与旧 generic visitor 兼容。
- `frontend/tests/sqliteTable.test.mjs`：真实载荷、精度、同步取消、目录身份、分页选择和旧响应保护。
- `frontend/tests/browser/sqlite-table-fixtures.mjs`：真实 native SQLite 生成的响应驱动实际 Vue 组件，检查分页、选列、精确整数、空页、省略说明与 HTML 惰性显示。
- 合成数据入口：`sandbox/tests/sqlite_table_fixtures.py` 的 `sqlite_bytes()`、`sqlite_options()` 与 `sqlite_payloads()`；不需要真实用户文件。

主要规范依据：[SQLite 不可信数据安全建议](https://www.sqlite.org/security.html)、[运行时限额](https://www.sqlite.org/limits.html)、[文件头与 WAL 标记](https://www.sqlite.org/fileformat2.html)、[immutable URI](https://www.sqlite.org/uri.html)、[进度回调](https://www.sqlite.org/c3ref/progress_handler.html)、[APSW 连接安全 API](https://rogerbinns.github.io/apsw/connection.html)、[APSW 内存限额](https://rogerbinns.github.io/apsw/apsw.html)。
