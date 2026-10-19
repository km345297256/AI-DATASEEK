"""Bounded public GEO/SRA topic metadata reads through NCBI E-utilities."""
import asyncio
import json
import re
import time
from xml.etree import ElementTree as ET

import httpx
from pydantic import Field
from app.domain.models.source_connector import SourceRecord
from app.domain.models.data_connector import ConnectorResource, ConnectorResult
from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.ncbi import _rate_limit


class TopicSearch(SourceRecord):
    query: str = Field(min_length=1, max_length=500, description='主题关键词或 Entrez 检索式；衰老自动展开为 aging OR senescence')
    page_size: int = Field(default=5, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=10000, description='下一页使用 next_cursor 的整数值，保持 query 不变')


def xml_fragment(value):
    if not isinstance(value, str) or re.search(r'<!\s*(DOCTYPE|ENTITY)', value, re.I):
        raise ValueError('invalid XML fragment')
    return ET.fromstring('<root>' + value + '</root>')


class EntrezTopicConnector:
    def __init__(self, *, transport=None, limiter=_rate_limit):
        self.transport, self.limiter = transport, limiter

    async def _get(self, endpoint, params):
        try:
            async with asyncio.timeout(25):
                await self.limiter()
                async with httpx.AsyncClient(transport=self.transport, trust_env=False, timeout=20,
                                             follow_redirects=False) as client:
                    async with client.stream('GET', 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/' + endpoint,
                                             params={**params, 'retmode': 'json', 'tool': 'AI-DataSeek'}) as response:
                        if response.status_code == 429:
                            raise SourceConnectorError('rate_limited')
                        if response.status_code != 200:
                            raise SourceConnectorError('upstream_unavailable')
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 2 * 1024 * 1024:
                                raise SourceConnectorError('response_too_large')
            payload = json.loads(data)
            if not isinstance(payload, dict) or payload.get('error'):
                raise ValueError('invalid response')
            return payload
        except (httpx.HTTPError, TimeoutError):
            raise SourceConnectorError('upstream_unavailable') from None
        except (ValueError, UnicodeError):
            raise SourceConnectorError('invalid_response') from None

    async def search(self, database, args):
        if database not in ('gds', 'sra'):
            raise ValueError('unsupported database')
        query = args.query.replace('衰老', '(aging OR senescence)')
        term = '(' + query + ')' + (' AND gse[ETYP]' if database == 'gds' else '')
        connector_id = 'ncbi-geo' if database == 'gds' else 'ncbi-sra'
        try:
            search = (await self._get('esearch.fcgi', dict(db=database, term=term,
                retmax=args.page_size, retstart=args.offset)))['esearchresult']
            if search.get('ERROR') or search.get('errorlist'):
                raise ValueError('invalid search')
            count = int(search['count'])
            ids = search['idlist']
            if (count < 0 or not isinstance(ids, list) or len(ids) > args.page_size
                    or len(ids) != len(set(ids)) or any(not re.fullmatch(r'[0-9]+', uid) for uid in ids)
                    or len(ids) != min(args.page_size, max(0, count - args.offset))):
                raise ValueError('invalid page')
            resources = []
            summaries = (await self._get('esummary.fcgi', dict(db=database, id=','.join(ids))))['result'] if ids else {}
            for uid in ids:
                record = summaries[uid]
                meta = dict(original_query=args.query, query=term, query_translation=search.get('querytranslation', term),
                            database=database, uid=uid, offset=args.offset)
                if database == 'gds':
                    accession = record['accession']
                    if not re.fullmatch(r'GSE[0-9]+', accession):
                        raise ValueError('invalid GEO accession')
                    title = record['title']
                    url = 'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=' + accession
                    meta.update(summary=str(record.get('summary', ''))[:8000], organism=record.get('taxon'),
                                sample_count=record.get('n_samples'), study_type=record.get('gdstype'),
                                bioproject=record.get('bioproject'))
                else:
                    root = xml_fragment(record['expxml'])
                    accession = root.find('Experiment').attrib['acc']
                    if not re.fullmatch(r'[SED]RX[0-9]+', accession):
                        raise ValueError('invalid SRA accession')
                    study = root.find('Study')
                    title = (study.get('name') if study is not None else '') or root.findtext('Summary/Title') or accession
                    url = 'https://www.ncbi.nlm.nih.gov/sra/' + accession
                    organism = root.find('Organism')
                    runs = [n.get('acc') for n in xml_fragment(record.get('runs', '')).findall('Run')]
                    if any(not re.fullmatch(r'[SED]RR[0-9]+', acc or '') for acc in runs):
                        raise ValueError('invalid run')
                    meta.update(study_accession=study.get('acc') if study is not None else None,
                                organism=organism.get('ScientificName') if organism is not None else None,
                                experiment_title=root.findtext('Summary/Title'), run_accessions=runs[:100],
                                library_strategy=root.findtext('Library_descriptor/LIBRARY_STRATEGY'),
                                bioproject=root.findtext('Bioproject'), result_unit='experiment')
                meta['full_title'] = title
                resources.append(ConnectorResource(id=accession, external_id=accession, title=title[:200],
                    source_url=url, retrieved_at=int(time.time()), metadata=meta))
            next_offset = args.offset + len(ids)
            return ConnectorResult(connector_id=connector_id, connector_version='1.0.0', operation='search',
                resources=resources, total_count=count,
                next_cursor=str(next_offset) if ids and next_offset < count and next_offset <= 10000 else '')
        except (KeyError, TypeError, ValueError, AttributeError, ET.ParseError):
            raise SourceConnectorError('invalid_response') from None
