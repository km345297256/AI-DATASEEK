"""Explicit trusted registrations; never execute code, URLs or tool names from a manifest."""
import asyncio
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from pydantic import BaseModel, ValidationError
from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.domain.models.data_connector import ConnectorManifest, ConnectorOperation, ConnectorResource, ConnectorResult
from app.domain.models.source_connector import SourceRecord, Accession, NcbiSearch
from app.domain.external.data_source import SourceConnectorError


class DescribeInput(SourceRecord):
    accession: Accession


@dataclass(frozen=True)
class RegisteredOperation:
    descriptor: ConnectorOperation
    arguments_model: type[BaseModel]
    invoke: Callable[[str, str, BaseModel], Awaitable[ConnectorResult]]


@dataclass(frozen=True)
class RegisteredConnector:
    manifest: ConnectorManifest
    operations: dict[str, RegisteredOperation]


class DataConnectorService:
    def __init__(self, connectors: list[RegisteredConnector]):
        self._connectors = {}
        for connector in connectors:
            manifest = connector.manifest
            if manifest.id in self._connectors:
                raise ValueError('Duplicate connector registration')
            declared = {op.name: op for op in manifest.operations}
            if len(declared) != len(manifest.operations) or set(declared) != set(connector.operations):
                raise ValueError('Manifest and runtime operations differ')
            for name, operation in connector.operations.items():
                if (declared[name] != operation.descriptor or
                        operation.arguments_model.model_json_schema() != operation.descriptor.input_schema or
                        operation.descriptor.output_schema != ConnectorResult.model_json_schema()):
                    raise ValueError('Connector schema does not match runtime')
            self._connectors[manifest.id] = connector

    def catalog(self) -> list[ConnectorManifest]:
        return [entry.manifest.model_copy(deep=True) for entry in self._connectors.values()]

    async def call(self, owner: str, session: str, connector_id: str, operation: str, arguments: dict):
        entry = self._connectors.get(connector_id)
        if entry is None:
            raise NotFoundError('连接器不存在')
        registered = entry.operations.get(operation)
        if registered is None:
            raise BadRequestError('连接器未提供此操作')
        try:
            validated = registered.arguments_model.model_validate(arguments)
        except ValidationError:
            raise BadRequestError('连接器参数不符合声明的格式') from None
        try:
            async with asyncio.timeout(registered.descriptor.timeout_seconds):
                raw = await registered.invoke(owner, session, validated)
            result = ConnectorResult.model_validate(raw)
            if (result.connector_id != entry.manifest.id or result.connector_version != entry.manifest.version
                    or result.operation != operation):
                raise ValueError('Mismatched connector result')
            return result
        except TimeoutError:
            raise SourceConnectorError('timeout') from None
        except (ValidationError, ValueError):
            raise SourceConnectorError('invalid_connector_result') from None


def ncbi_registration(service) -> RegisteredConnector:
    connector_id, version = 'ncbi-datasets-v2', '1.1.0'

    def resource(candidate):
        return ConnectorResource(id=candidate.external_id, title=candidate.title,
            external_id=candidate.external_id, source_url=candidate.source_url,
            candidate_id=candidate.candidate_id, retrieved_at=candidate.created_at,
            metadata=candidate.discovery.model_dump(mode='json'))

    async def search(owner, session, args):
        page = await service.search(owner, session, args)
        return ConnectorResult(connector_id=connector_id, connector_version=version, operation='search',
            resources=[resource(candidate) for candidate in page.candidates],
            next_cursor=page.next_page_token, total_count=page.total_count)

    async def describe(owner, session, args):
        candidate = await service.describe(owner, session, args.accession)
        return ConnectorResult(connector_id=connector_id, connector_version=version, operation='describe',
            resources=[resource(candidate)])

    async def acquire(owner, session, args):
        receipt = await service.acquire(owner, session, args.accession)
        return ConnectorResult(connector_id=connector_id, connector_version=version, operation='acquire',
            kind='files', resources=[], files=[receipt], files_downloaded=True)

    operations = {}
    for name, title, description, model, invoke in [
        ('search', '检索数据', '按分类与筛选条件检索基因组组装元数据，每页最多 20 条；翻页保持筛选条件。', NcbiSearch, search),
        ('acquire', '下载数据', '下载指定版本的 genomic FASTA ZIP 数据包；最多 32 MiB，超时或校验失败不会标记成功。', DescribeInput, acquire),
        ('describe', '读取元数据', '读取完整 accession.version 指定的组装元数据，不替换为最新版。', DescribeInput, describe),
    ]:
        descriptor = ConnectorOperation(name=name, title=title, description=description,
            input_schema=model.model_json_schema(), output_schema=ConnectorResult.model_json_schema(),
            effects=['network', 'session_metadata'] + (['file_write'] if name == 'acquire' else []), timeout_seconds=60)
        operations[name] = RegisteredOperation(descriptor, model, invoke)
    return RegisteredConnector(ConnectorManifest(id=connector_id, version=version, title='NCBI Datasets',
        description='检索官方元数据，下载指定版本的基因组 FASTA 数据包。', transport='native', auth='none',
        domains=['www.ncbi.nlm.nih.gov', 'api.ncbi.nlm.nih.gov'],
        operations=[op.descriptor for op in operations.values()],
        limitations=['支持单个组装的 genomic FASTA ZIP 下载，压缩包最多 32 MiB、解压总量最多 64 MiB；暂不支持大文件断点续传。', '公开数据源，无需账户授权；来源记录保存于当前任务。']), operations)


def topic_registration(client, database):
    from app.infrastructure.external.data_sources.entrez import TopicSearch
    connector_id, title = ('ncbi-geo', 'NCBI GEO') if database == 'gds' else ('ncbi-sra', 'NCBI SRA')

    async def search(owner, session, args):
        return await client.search(database, args)

    descriptor = ConnectorOperation(name='search', title='主题检索',
        description='按主题关键词检索公开研究元数据，支持衰老等主题；每页最多 20 条。',
        input_schema=TopicSearch.model_json_schema(), output_schema=ConnectorResult.model_json_schema(),
        effects=['network'], timeout_seconds=60)
    return RegisteredConnector(ConnectorManifest(id=connector_id, version='1.0.0', title=title,
        description=('检索 GEO 表达与功能基因组研究系列（GSE）。' if database == 'gds'
                     else '检索 SRA 测序实验及其关联研究、Run 编号。'),
        transport='native', auth='none', domains=['eutils.ncbi.nlm.nih.gov', 'www.ncbi.nlm.nih.gov'],
        operations=[descriptor], limitations=['仅检索元数据，不下载原始文件；结果为单页关键词匹配，不代表全面筛选。',
        '衰老展开为 aging OR senescence；其他主题可提供英文关键词或 Entrez 检索式。',
        'SRA 按实验返回，同一研究可能包含多个实验；不可将实验数量当作研究数量。']),
        {'search': RegisteredOperation(descriptor, TopicSearch, search)})


def build_data_connector_service(ncbi_service, topic_client=None):
    from app.infrastructure.external.data_sources.entrez import EntrezTopicConnector
    client = topic_client or EntrezTopicConnector()
    return DataConnectorService([ncbi_registration(ncbi_service),
        topic_registration(client, 'gds'), topic_registration(client, 'sra')])


def get_data_connector_service():
    from app.application.services.ncbi_discovery_service import get_ncbi_discovery_service
    return build_data_connector_service(get_ncbi_discovery_service())
