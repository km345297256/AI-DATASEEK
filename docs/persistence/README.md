# 持久化兼容性门禁

`history.json` 顺序引用不可改写的已接受快照。快照覆盖 AgentEvent、Plan、Step、Memory、InputAdmission、ExecutionHistory 的读写 JSON Schema、实际写入版本和摘要。Plan/Step 使用所属事件版本；Memory 当前未显式版本化，记为 0。快照不包含用户数据、密钥或真实数据路径。

从 backend 运行 `uv run python scripts/check_persistence_contracts.py --check`。该检查也在 pytest 和统一回归入口内执行；任何未登记的结构变化都会失败。只调换字段顺序或改说明文字不会失败。

升级步骤：

1. 保留旧快照和旧数据测试样本，不覆盖初始基线。
2. 新增或修改行为/迁移测试，证明旧数据仍可读；重建型 ExecutionHistory 缓存应测试失效后从不可变事件重建，不能把旧版本强行当新版本读取。
3. 运行 `uv run python scripts/check_persistence_contracts.py --candidate ../docs/persistence/YYYY-MM-DD-change.json` 生成一个**新文件**。生成不表示接受兼容性，也不更新 history。
4. 给 history 的 snapshots 追加记录，包含 file，以及 changes 下每个受影响 root 的 reason、compatibility、test（例如 backend/tests/test_persistence_contracts.py）。重新运行检查会给出未确认差异。增加必填字段、删字段、改类型/默认值等按破坏性变化处理，必须先增加实际写入版本并实现迁移/兼容读取；未版本化结构须先引入版本机制。
5. 跑旧样本回读、事件回放、隐私投影及相关业务测试，再审查提交。修改共同类型会同时影响多个 root，不能只登记其中一个。

分类器故意保守：不认识的 Schema 变化按破坏性处理。它只能检查结构，无法证明语义、权限或科学证据兼容。说明和迁移测试必须由维护者实际审查；测试文件存在不等于测试通过。基线文件与记录同样需要代码审查，不能通过删历史或覆盖快照消除告警。
