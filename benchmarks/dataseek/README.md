# DataSeek 预实验入口

这里实现论文方案的第一阶段：以18份内置公开样例生成36道客观题，冻结数据身份和隐藏答案，运行真实分析后检查实际交付的 `answer.json`。

已实现 DataSeek 默认 API 适配器与通用 ReAct（B1）适配器。两者使用部署中的实际模型并产生 API 消耗。`prepare`、参考答案生成、测试和 `report` 都不调用模型。`run` 才启动真实分析。

2026-09-15 获批的首批六次已完成，DataSeek 2/3、通用 ReAct 3/3。结果与限制见[首批预实验报告](../../docs/experiment-pilot-2026-09-15.md)。这只是开发校准，不支持方法优劣结论。

## 目前完成的范围

- 9个领域、18个数据来源、每来源结构/分析各1题；这批样例用于开发，不能称为盲测。
- 参考答案使用独立计算和领域库，包含实际输入文件的大小与 SHA256；代码不导入 DataSeek 的分析工具生成 gold。
- 逐字段严格评分，整数准确匹配，浮点按题目容差，拒绝 NaN、重复 JSON 键与伪装数值的布尔值。FITS 微小数值使用更小的绝对容差。
- 模型可见任务由白名单投影产生，隐藏答案和宿主路径不进入请求；只下载真正交付的文件，不把聊天中的 JSON 当成文件成果。
- SSE 断开不会重复提交分析；使用稳定输入身份，记录部分结果并停止对应会话。停止未确认时中止批次。
- 费用未知时保留为空；会话用量与 ModelTrace 不能相加。

API 模式当前只施加墙钟取消，不具备每次模型请求前的累计 Token/调用数准入。B1 具有估算输入与预留输出的准入控制，默认 200,000 Token、64 次物理请求、每次最大输出 4,096 Token；估算不等于精确计费上限。两组资源控制尚未统一，适合预实验，尚不满足正式论文的全部资源控制条件。基础模型版本以配置和可用请求记录核对；供应商模型别名不保证权重版本不变。

B1 仅暴露 `shell_run` 与 `file_read`，以独立循环使用相同科学镜像，不主动安装产品规划、交付修复或科学工具目录。它仍共享部分公共工具保护，Shell 也没有从技术上隔绝镜像中的项目辅助代码。这是通用基线，不能标为“同工具 ReAct（B2）”；B2 尚未实现。

## 1. 生成参考答案

从仓库根执行。复用现有沙箱镜像，无网络，数据与脚本只读；不启动第二套服务。

```bash
mkdir -p benchmarks/dataseek/private
chmod 700 benchmarks/dataseek/private
docker run --rm --network none --read-only --tmpfs /tmp \
  -v "$PWD/backend/app/resources/datasets:/datasets:ro" \
  -v "$PWD/benchmarks/dataseek/oracles.py:/oracle.py:ro" \
  --entrypoint /app/.venv/bin/python ai-dataseek-sandbox:latest \
  /oracle.py /datasets > benchmarks/dataseek/private/pilot-gold.json
```

`private/` 与 `runs/` 均已忽略提交。不要将这两个目录、完整评测仓库或参考答案挂载进分析沙箱。原始数据继续走产品的数据集绑定与只读挂载校验。

## 2. 冻结本轮实验

下面记录2026-09-15核实到的既有本机入口和模型：唯一前端当时为 `127.0.0.1:7001`，模型为 `deepseek-flash`。这是已有运行状态记录，不是要求修改部署端口；其他环境应显式填写核实后的地址。

```bash
python3 -m benchmarks.dataseek prepare \
  --gold benchmarks/dataseek/private/pilot-gold.json \
  --experiment-dir benchmarks/dataseek/runs/pilot-next \
  --base-url http://127.0.0.1:7001 \
  --backbone deepseek-flash --methods dataseek_default,generic_react \
  --repeats 1 --wall-seconds 300 --snapshot \
  --task-ids ncbi-lambda-reference--analysis,open-uci-iris--analysis,open-noaa-air-climatology--analysis
```

该命令示范准备与首批相同三题、两种方法、各一次的六次计划，拒绝覆盖已有实验目录。省略 `--task-ids` 会使用全部 36 题；省略 `--methods` 则仅使用 DataSeek。每次 `run` 开始前核对当前模型、默认工具设置、后端源码、镜像和服务端实际数据哈希；配置改变或存在自动启用技能时停止。

现有 `pilot-20260915-six` 为已完成批次，不能覆盖或追加来改写结果。较早的 `pilot-20260915-a` 仅准备了 72 次 DataSeek 计划，没有启动分析；它不属于本批六次结果。准备更多计划不构成新的模型费用授权。

## 3. 真实模型跑测

以下是后续新批次的使用方式；现有六次授权已执行完毕。只有在相应外部模型数据发送与费用已获授权时执行：

```bash
python3 -m benchmarks.dataseek run \
  --experiment-dir benchmarks/dataseek/runs/pilot-next --limit 1
```

`--limit` 是此次最多新启动多少次，不是重试次数。已存在的 run_id 即使失败也不重放；需要新的实验身份才能重复。DataSeek 会话和下载成果会保留；B1 下载成果后删除其专属临时沙箱，保留执行和清理记录。停止或清理未确认时暂停批次。外部审批拒绝时必须停止真实调用，不能换接口或借基线绕过。

DataSeek 分析超时后，通过该会话停止接口取消；取消是尽力执行，无法保证服务端立即停止或给出精确计费上限。B1 在模型与工具循环中检查分析截止时间，再收集成果并清理沙箱。收集、取消核对和清理可能延长记录的总耗时，不能将总耗时当作严格的服务端计费界限。

## 4. 独立评分与用量核对

```bash
python3 -m benchmarks.dataseek report \
  --experiment-dir benchmarks/dataseek/runs/pilot-20260915-six
```

报告同时保留已观测运行和整个计划的分母，尚未启动的题目作为进度单列；不能把未完成批次当最终成功率。`claimed_complete` 需要共同规则的盲评，目前不由产品完成状态推断。代码收集不等于已验证重运行，预实验不报告代码可复现率。

`environment.collect_session_usage(session_ids)` 只读聚合选定会话的用量。它仍可能缺少未报告/未落库的提供方请求，不能声称等于完整账单。

## 验证

宿主标准库测试（科学库不可用时，oracle测试会明确跳过）：

```bash
python3 -m unittest discover -s benchmarks/dataseek/tests -p 'test_*.py' -v
```

在现有科学镜像中运行包括独立参考答案在内的全部评测器测试：

```bash
docker run --rm --network none -w / \
  -e DATASEEK_BENCHMARK_DATASETS=/datasets \
  -v "$PWD/backend/app/resources/datasets:/datasets:ro" \
  -v "$PWD/benchmarks:/benchmarks:ro" \
  --entrypoint python ai-dataseek-sandbox:latest \
  -m unittest discover -s /benchmarks/dataseek/tests -p 'test_*.py' -v
```

全仓库检查继续使用 `scripts/check-regressions.sh --containers`。个别科学格式的独立参考测试依赖仅安装到一次性测试容器；缺少参考库引起的跳过必须如实报告，不修改生产镜像来掩盖问题。

完整研究设计见 [论文对比实验方案](../../docs/comparative-experiment-design.md)。正式实验还需独立盲测来源、相同资源准入的全部基线、绘图语义/代码重运行评测和统计区间。
