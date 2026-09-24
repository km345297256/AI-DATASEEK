# Shell 输出：生产端限内存、私有日志与分页

普通 shell 的 stdout/stderr 不再随运行时间无限累积到两份字符串。
当前命令保留最多 32 KiB UTF-8 首尾预览；旧 console 仍保留原字段和展示方式，
但总文本限制为 128 KiB、最多 64 条。单段预览的正文带省略标记，sandbox
接口另外返回 `console_truncated` / `output_truncated`；旧前端响应映射不展示
这些新标志，所以不能把 UI 中的 console 历史视为全部历史。程序执行已有的
`program_execution`、32,000 字符预览、退出码和 execution receipt 语义不变。

每次命令启动生成独立的 `output_metadata.output_id`。沙箱把解码后的原始文本
和去 ANSI 的显示文本分别流式写入 `/tmp` 下匿名、0600 的私有临时文件。
文件名/文件描述符不返回浏览器或模型，不写数据集挂载，不进入分析产物列表。
单份日志上限为 32 MiB；超过上限或存储失败，输出仍继续被读取，预览仍保持有界，
对应 `raw_log_status` / `log_status` 明确为 `limit_exceeded` / `unavailable`。
这些是单份输出存储保护，不是分析任务累计时长、调用量或 Token 配额。

日志只在当前沙箱进程与命令输出代次内可用：替换命令、release shell 或沙箱重启
会使旧日志失效；这不是跨重启的持久日志服务。取消/kill 保留已经捕获的日志供检查，
release 才关闭文件。清理先执行既有进程树终止和有界最后 drain，然后关闭日志并
取消仍被继承管道挂住的 reader。`stream_complete` 只由 stdout 的实际 EOF 确认，
不能由 shell leader 退出推断。

## 兼容分页接口

原 `POST /api/v1/shell/view` 的 `id`、`console` 和旧响应字段保留。
新增可选 `output_id` + `cursor`（必须同时提供）、`max_bytes`（4–16384，默认8192）。
模型的 `shell_view` 也提供相同参数。先读取预览中的输出标识，再从 cursor=0 开始，
后续使用 `output_page.next_cursor`。游标是去 ANSI 显示文本的绝对 UTF-8 字节偏移；
每个调用者自持游标，模型读取不消耗 UI 或另一个观察者的位置。

- `output_page.lossy` 表示所请求的前缀已不可恢复或游标位于字符内部。
- 存储失效只能退回有界尾部，明确标记 `source=tail`，不能假装完整。
- `eof=true` 同时要求实际读取到了流结束和当前页到达全部已捕获文本末尾。
- 旧命令的 `output_id` 不能读取新命令的日志；既有 operation receipt 检查仍生效。
- Unicode 与跨块 ANSI 控制序列在生产端处理；不会靠逐页清理产生破碎字符或颜色码。

固定科学工具和 Cordis 插件的 stdout 是 JSON 协议，不允许把展示预览当完整返回值。
这些确定性消费者会在既有 2 MiB 契约上限内只读分页补全，逐页检查身份、总字节数、
进度、缺口和 EOF，然后才校验 JSON。普通 `shell_run` / `program_run` 不自动展开大日志。
任何恢复失败都明确失败，不重新执行原命令；凭据仍在进入模型/事件前隔离。

测试使用合成日志，覆盖内存边界、完整分页恢复、独立游标、Unicode/ANSI、最后 drain、
存储失败与上限、旧代次拒绝、关闭与取消、HTTP兼容、插件大 JSON 以及执行凭据回归。

2026-09-24 验证：后端 Shell/插件/程序执行相关回归 137 项通过；Linux sandbox
全量 5954 项通过、87 项跳过、563 项子测试通过。Linux 使用已有 sandbox 镜像的
无网络、只读临时容器，源码只读挂载，测试与第三方 oracle 依赖仅安装到容器临时目录，
没有更新运行中服务或生产依赖。
