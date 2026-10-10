# 持久化兼容性门禁

`history.json` 顺序引用不可改写的已接受快照。快照覆盖 AgentEvent、Plan、Step、Memory、InputAdmission、ExecutionHistory 的读写 JSON Schema、实际写入版本和摘要。Plan/Step 使用所属事件版本；Memory 当前未显式版本化，记为 0。快照不包含用户数据、密钥或真实数据路径。

2026-09-29 起还独立登记被通用字典隐藏的 MCPImageRef、SpillArtifactRef、SpillArtifactNotice、SpillArtifactRecord、AnswerReviewMarker，以及私有 ModelTraceRecord、TokenUsageRecord。`owners` 记录实际持久化入口/所属类型，改变归属也触发检查。MCP 引用版本来自实际 envelope key 的共享常量；spill 引用、notice 和 trace 使用 `schema_version`；answer-review 使用 `version`。SpillArtifactRecord、TokenUsageRecord 目前未版本化，诚实记录为 0，不能只修改快照数字冒充版本升级。

AnswerReviewMarker 使用生产写入/读取模型，保留旧记录未知诊断字段，且序列化不填充原本缺失的可选字段。该根覆盖 review 信封的版本、状态、作用域和收尾标记；内部可扩展诊断字典的每个字段尚不由结构门禁逐一约束，仍需要科学审查/历史恢复的行为测试。结构合法不代表科学结论正确，更不授予执行、文件或数据集权限。

从 backend 运行 `uv run python scripts/check_persistence_contracts.py --check`。该检查也在 pytest 和统一回归入口内执行；任何未登记的结构变化都会失败。只调换字段顺序或改说明文字不会失败。

升级步骤：

1. 保留旧快照和旧数据测试样本，不覆盖初始基线。
2. 新增或修改行为/迁移测试，证明旧数据仍可读；重建型 ExecutionHistory 缓存应测试失效后从不可变事件重建，不能把旧版本强行当新版本读取。
3. 运行 `uv run python scripts/check_persistence_contracts.py --candidate ../docs/persistence/YYYY-MM-DD-change.json` 生成一个**新文件**。生成不表示接受兼容性，也不更新 history。
4. 给 history 的 snapshots 追加记录，包含 file，以及 changes 下每个受影响 root 的 reason、compatibility、test（例如 backend/tests/test_persistence_contracts.py）。重新运行检查会给出未确认差异。增加必填字段、删字段、改类型/默认值等按破坏性变化处理，必须先增加实际写入版本并实现迁移/兼容读取；未版本化结构须先引入版本机制。
5. 跑旧样本回读、事件回放、隐私投影及相关业务测试，再审查提交。修改共同类型会同时影响多个 root，不能只登记其中一个。

2026-10-10 起，新增兼容记录还必须在每个受影响 root 下提供 `readers` 对象：

- `new_reads_old`：新读者如何保持旧记录的含义，或如何迁移／重建。
- `old_reads_new`：旧读者如何处理新记录；是无损保留、安全忽略，还是在执行、写入和恢复前明确拒绝。未知版本不能被当作损坏数据截断或触发任务重跑。

两项均须写明实际行为及验证依据，不以“结构兼容”代替验证。既有四份变更记录按其完整记录摘要保留豁免，不重写旧历史；新增记录不能靠省略版本或沿用旧日期绕过要求。原有必填兼容说明、测试路径、破坏性变化必须提升真实 writer version 的规则不变。

独立审查两个**可信本地提交**的实际源代码，可运行：

```bash
python scripts/check_persistence_contracts.py --review --before-ref BASE --after-ref HEAD
```

该入口解析提交 ID，通过 Git 源码归档在临时目录中分别生成 schema，不切换工作树、不读取已登记的快照代替源码，也不读取仓库 `.env` 或向子进程传递部署密钥。不联网获取缺失提交。输出包含两个准确提交 ID、全部结构变化及反向读者风险；即使变化已登记，仍会显示。退出码 0 表示比较成功，不表示兼容性已获认证，也不能替代 `--check`。

提取会执行所选提交的 Python schema 代码，使用当前解释器的已安装依赖；它不是不可信代码沙箱。仅选择已审查的本仓库提交，历史依赖不兼容时明确失败，不能退回快照掩盖提取失败。结构分类是保守提示，两方向的语义、权限和安全拒绝仍需人工审查及行为测试。

分类器故意保守：不认识的 Schema 变化按破坏性处理。它只能检查结构，无法证明语义、权限或科学证据兼容。说明和迁移测试必须由维护者实际审查；测试文件存在不等于测试通过。基线文件与记录同样需要代码审查，不能通过删历史或覆盖快照消除告警。
