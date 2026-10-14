# DataSeek 数据连接器

此目录保存数据连接器的标准与可核对示例。连接器负责访问数据源；Skill 负责处理方法；MCP / HTTP / native 是适配方式。

- [产品与调用标准](../docs/data-connector-standard.md)
- [声明 JSON Schema](schema/connector-v1.schema.json)
- [NCBI 示例声明](packages/ncbi-datasets/connector.json)

当前为服务端显式注册的公开数据访问能力。JSON 文件不携带凭据或可执行代码，不支持上传即安装。NCBI 1.1.0 提供 search / describe / acquire，可下载单个明确版本的 genomic FASTA ZIP（最多 32 MiB），校验后保存为任务文件。标准来源为 `backend/app/domain/models/data_connector.py`；修改模型与样例后须同步 JSON，并运行 `tests/test_data_connector_contract.py`。
