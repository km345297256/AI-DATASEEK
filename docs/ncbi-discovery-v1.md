# NCBI 基因组元数据发现 v1

这批交付 LAB-04 的首个来源连接器：NCBI Datasets v2 的分类解析、组装检索、精确版本元数据和获取计划。对话执行器可调用工具，官方观察保存在上一批会话数据线索集合，前端展示来源凭据。未交付 genomic FASTA 下载、持久获取队列、重启恢复、独立 GC 计算或完整分析技能。

## 对话能力

- `ncbi_search_genomes`：按 NCBI taxonomy ID 或当前学名检索；可选择 RefSeq / GenBank / all、组装层级、reference_only。每页 1–20 条，默认 5 条。分页时保持原查询和筛选条件，传回 next_page_token。每页内部按 accession 排序，跨页顺序由供应方决定，不宣称全局固定排序或完整样本。
- `ncbi_describe_genome`：接受完整 GCF_/GCA_ accession.version，只返回该精确版本；不自动替换成 current_accession 或配对组装。
- `ncbi_plan_acquisition`：读取当前会话的官方候选，生成将来获取 genomic FASTA 所需的身份和预算信息。status=not_started、execution_available=false，不启动任务、不创建文件版本。

示例：“查询 NCBI 分类 ID 1386 下的两个 RefSeq 完整基因组，列出 accession 版本、官方报告长度和 GC，并保存来源。先不要下载。”

查询名称时先调用 NCBI Taxonomy，再用明确的数字 ID 查询组装。名称可能同名：2026-10-13 联网验证中，Bacillus 对应 Bacteria 下的 1386 与 Eukaryota 下的 55087。连接器返回带 domain/rank 的候选而不猜选；模型仅在用户意图明确时选用对应 ID，否则询问。分类解析最多接受一页 20 条，更多分页或无法确认名称时失败，不将部分匹配当唯一答案。数字 ID 表达用户明确选择，不再由模型猜测名称对应关系。

RefSeq 来源不等于 reference genome 标记。上游记录若不满足筛选条件，整页失败，不悄悄放宽条件凑数量。不同组装或菌株 tax_id 不等于不同物种；当前不自动进行物种级去重。

## 来源记录

NCBI 候选使用 provenance=ncbi_datasets 和 evidence=official_metadata_only。保存连接器版本、抓取时间、规范化 JSON 报告 SHA-256、解析后的分类 ID/名称及分类报告摘要、请求条件、分页 token 的哈希，以及有界组装字段。服务端构造 NCBI 来源链接；客户端不能通过人工登记接口声明官方证据。

原始报告可能包含大量附属字段；当前只保留摘要和投影，不保存原始完整报告文件。摘要计算基于排序 JSON，而不是 HTTP 字节。观察身份包含 accession.version、报告摘要和查询证据，忽略抓取时间以保证相同内容的幂等重试。供应方报告或查询变化会产生新观察，旧观察不覆盖；同一组装版本链共享 asset_id。重新读取旧观察显示的是首次登记时间，不冒充当前在线状态。

官方报告长度和 GC 只作供应方元数据展示。bp 不是下载字节数；获取计划的 download_size_bytes=null、transfer_limit_bytes=100 MiB。用户手动关联到官方候选的文件，依旧保持 acquisition.source_verified=false，不能继承元数据的来源身份而冒充官方下载。

## 接口与执行边界

会话所有权保护下新增：

- POST `/api/v1/sessions/{session_id}/data-candidates/ncbi/search`
- POST `/api/v1/sessions/{session_id}/data-candidates/ncbi/describe`
- GET `/api/v1/sessions/{session_id}/data-candidates/{candidate_id}/acquisition-plan`

对话的工具模式加入这三个核心工具，前置分类器明确将互联网数据发现交给工具执行器。网络工具声明 network effect，进入现有超时、审计、策略与按调用授权链；没有新增外部凭据或绕过权限。领域子代理不自动继承网络发现工具。

请求固定到 `https://api.ncbi.nlm.nih.gov/datasets/v2`，不接收下载 URL，不跟随重定向，不采用环境代理，TLS 校验保持开启。用户输入只进入经过验证/编码的路径和查询参数。每次响应解码后上限 2 MiB，每次网络操作总时限 25 秒；单次工具上限 60 秒（名称解析和组装检索可能各需一次请求）。每个进程事件循环间隔至少 0.4 秒发起请求；这不是跨实例全局限流，多进程仍需遵守供应方 429。

429 提供有界 Retry-After；超时、异常响应、上游错误、版本不符和分类歧义作为明确失败返回。不自动重试、不把失败转为空结果。空 JSON 对象无法证明没有匹配项，保守视为 invalid_response。分页中断可以使用已返回 token 再查，但当前没有持久下载检查点或断点续传。

## 验证

离线回归覆盖：分类歧义与拒绝猜选、来源/层级/reference 筛选、精确版本、分页、响应限额、429/超时/重定向、取消、不完整响应、会话隔离、观察幂等与旧记录保留、实际工具包装及执行器注册、前端获取计划的会话隔离。后端 5,370 项通过、32 跳过、190 子测试通过，继续排除原有包含导入时数据库操作的 test_admin_skill_delete.py。前端 2,670 项、类型检查和生产构建通过；Compose 配置通过。沙箱仍有 PyMuPDF、python-pptx、GRIB 原生依赖引起的三项收集错误。

已实际联网验证分类歧义、细菌节点 1386 的 RefSeq 完整基因组检索、精确 GCF_000009045.1 元数据及下一页。在线数据会变化，测试不锁定未来结果数量和顺序，也不将在线元数据等同于独立序列分析。

## 官方依据

- [NCBI Datasets v2 REST API](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/api/rest-api/)
- [官方 OpenAPI 定义](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/openapi3/openapi3.docs.yaml)
- [获取基因组元数据](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/get-genome-metadata/)

后续顺序：持久获取任务与预算 → FASTA 原文件保管及版本登记 → 将输入版本绑定 Run → 发布基因组特征比较技能并验证完整闭环。

新后端镜像中进一步通过真实 NCBI 请求、真实 MongoDB 临时数据库、实际工具包装和 ASGI 路由验证了持久化重读、跨用户隔离及未下载状态，临时数据库已清理。执行器回归同时覆盖已挂载数据会话中的工具可见性；未将工具级/API 联调宣称为完整下载分析场景或真实模型整段对话验收。
