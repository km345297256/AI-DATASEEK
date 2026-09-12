# SQL 文本转储只读预览：设计与专项验收

本文件记录受限文件阅读器的设计依据与专项测试，不表示 SQL 能执行或数据库能恢复。目标是识别转储中的表和字段，按原始语句顺序分页显示明确支持的字面量记录。整个过程不连接数据库、不执行 SQL、不调用模型。整体部署与跨插件验收以本轮汇总记录为准。

## 解析工具选择

- SQLGlot `30.18.0`，MIT，纯 Python、无必需第三方依赖。仅用于有界的 `CREATE TABLE` 结构分析，不引入其执行器、优化器或自动方言探测。SQLGlot 官方明确说明解析器宽松，解析成功不等于语句有效，因此还必须做本项目的允许列表检查。[版本与许可](https://pypi.org/project/sqlglot/30.18.0/) · [解析说明](https://sqlglot.com/sqlglot.html)
- 不选 sqlparse 作为语法验证器：它明确是非验证型解析器，适合分词和格式化。其 `0.6.0` 更新还记录了词法、分组、注释输入等拒绝服务修复；本项目不直接把整份大转储交给通用递归 AST 分组。[说明](https://sqlparse.readthedocs.io/en/stable/intro.html) · [更新](https://sqlparse.readthedocs.io/en/stable/changes.html)
- INSERT 元组和 PostgreSQL COPY 文本使用专门的有界线性扫描器。语句、注释、引号和深度先受预算约束，再对明确的语法子集解释；不通过数据库导入实现“预览”。

## 方言必须明确

初始范围为 PostgreSQL、MySQL、SQLite，由用户在预览器中选择；选择后还需点击“读取 SQL 转储目录”，不会自动读取。不能从 `.sql` 扩展名可靠推断 SQL 方言。MySQL 的字符串转义受 SQL mode 影响，PostgreSQL 的普通字符串又受 `standard_conforming_strings` 影响。因此首版对 MySQL/PostgreSQL 普通单引号字符串中的反斜杠明确拒绝；SQLite 普通字符串保留反斜杠原样，PostgreSQL COPY 文本按独立格式解码。首版不支持 `E'...'`、Unicode 转义和字符串拼接，不猜测会话模式。[MySQL 字符串](https://dev.mysql.com/doc/refman/8.4/en/string-literals.html) · [PostgreSQL 词法](https://www.postgresql.org/docs/current/sql-syntax-lexical.html)

已实现的受限子集：

| 输入 | 预览能力 | 不做的事 |
| --- | --- | --- |
| `CREATE TABLE` 显式字段列表 | 表目录、字段名称和规范化的大写类型声明，允许简单空值/主键/唯一键/字面量默认值和部分 MySQL 表属性 | 不计算默认值；GENERATED、CHECK、外键、分区、函数默认值等复杂 AST 拒绝 |
| `INSERT INTO ... VALUES (...)` | 明确字面量元组，按文件中出现顺序分页 | 不计算函数、子查询、表达式、类型转换、冲突更新或默认值 |
| PostgreSQL `COPY ... FROM stdin` 默认文本格式 | 制表符分列，识别 `\N` 与行终止 `\.`，有限转义解码 | 不访问 COPY 文件路径，不执行 PROGRAM，不支持 binary/csv/custom options |
| 已列入跳过范围的管理语句与 psql 控制行 | 计数说明其未执行，引用体内 SQL 不会成为表行 | 不应用 SET/ALTER/权限、删除或事务状态，不把片段当最终数据库快照 |

PostgreSQL `COPY` 的文本字段由类型输出函数生成，读入数据库时也可能调用类型输入函数；本插件只显示文本，不能据字段声明冒充“已恢复后的数据库值”。[COPY 官方格式](https://www.postgresql.org/docs/current/sql-copy.html)

MySQL 常用多行 INSERT 是 mysqldump 的正常形式，因此必须支持有界的多元组读取，而不是只识别第一条记录。[mysqldump](https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html)

SQLite `.dump` 是 SQL 导出，不是另一个数据库文件容器。本插件不将其交给 SQLite 执行；原有 SQL 文本阅读器继续保留。[SQLite CLI](https://sqlite.org/cli.html)

非 TABLE 的 CREATE 语句（如索引、序列、视图、函数、触发器）首版会拒绝，而不是静默漏掉可能影响解释的结构。常规语句必须以分号结束；COPY 块必须包含独立的 `\.` 终止行及换行。只接受 UTF-8（允许 BOM），不自动猜编码。普通 MySQL `--` 注释必须后接空白；PostgreSQL 嵌套块注释有独立深度上限。SQLGlot 的原始错误上下文和日志不会转交给用户。

## 实施预算与数据语义

- 整文件最多 16 MiB，单条语句最多 1 MiB，最多 4,096 条语句，嵌套括号/注释最多 32 层。
- 最多 32 张表、每表 128 个字段，累计表结构预算 128 KiB；单页最多 200 行 × 16 列，偏移最多 100,000。
- 总词法 token 最多 524,288，单语句最多 65,536，单个建表 AST 前最多 8,192；COPY 物理行最多 64 KiB，支持字面量行最多 1,000,000，解析进程内部有 15 秒期限，外层继续使用系统原有隔离 worker 的资源限制。
- 数字字面量不经过浮点数转换：保留正负号、前导零、小数位数和科学计数法文本。这里表示源文件字面量，而非执行后数据库类型。
- NULL、空字符串、布尔值必须区分；INSERT 缺少字段不能补成 NULL。本轮仅支持全部字段元组（允许显式字段重排）。
- 不回显原始 SQL、注释、错误上下文或宿主路径；所有异常返回固定提示。超长或含不安全路径的文本标注省略。
- 首次目录和每次分页都在既有只读隔离 worker 中重新受限读取；预算不是大数据库随机访问承诺。

## 已完成专项测试（2026-09-11）

- Sandbox SQL 阅读器 **91 项通过**：原生 SQLite 3.51.0 `.iterdump`、原生 PostgreSQL 18.6 `pg_dump` 普通 SQL/COPY 文件与独立服务器读回 oracle、手工原创 MySQL 多行 INSERT；分页、重排、NULL/空值、中文、超大整数/精确小数和 BLOB 长度均有断言。MySQL 目前仅有语法样本，未声称通过所有版本原生导出。
- 后端纯载荷协议 **48 项通过**：未知字段、私有路径、改写版号绑定参数、超预算、数字被转为浮点数、源格式/方言/行次序变化等拒绝；后端与 Sandbox 的纯校验文件逐字节相同。
- 前端 SQL 专项 **60 项通过**：明确方言、不自动请求、手动目录与分页、精度和 NULL 分离、切换文件/方言/列/偏移/限额或停用插件后取消旧响应。前端类型检查通过。
- 增加 3 个浏览器场景：分页/空表/方言切换清理，经过公开 `database-dump` 适配器的实际组件分发；浏览器运行结果由共同构建后的总体验收补充，不能把“场景已写好”算作执行通过。

固定原创样本：

- `sandbox/tests/fixtures/database-dumps/synthetic-sqlite.sql`，SQLite 3.51.0；SHA-256 `4f4bc5f40bb539de48932bf1d1c5c1940f8222e44431d0cfe5e73e30ead28700`。生成定义在 `sandbox/tests/sql_dump_fixtures.py`，不读取用户数据。
- `sandbox/tests/fixtures/database-dumps/synthetic-postgres.sql`，PostgreSQL 18.6；SHA-256 `88c5df89c0f365df872baca3491b8d31f0f387ca2578f1f9bd7e300a3bd90b20`。生成过程与独立 oracle 在同目录；服务仅用于生成原创测试数据，预览器不会连接该服务。
- `frontend/tests/browser/sql-dump-data.json` 包含由上述 SQLite 导出文本经真正阅读器产生的目录、首/末页、空表和选列载荷，不是手写模拟数值结果。

## 后续验收/扩展边界

已有专项测试覆盖引号内分号、注释内伪 SQL、引用体内伪 INSERT；恶意 COPY PROGRAM/文件路径、psql 控制行、表达式、模式歧义；截断、未闭合引号、超深嵌套与预算输入。共同构建后仍需浏览器与 HTTP 链路验收。未通过原生生成对照的子集不宣称全面兼容；大规模转储、MySQL 特殊模式、完整 DDL 和恢复后的查询应另行设计，不能通过开放 SQL 执行快捷绕过。
