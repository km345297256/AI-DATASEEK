"""NCBI Datasets v2 metadata connector. Fixed origin; never follows supplied URLs."""
import asyncio
import hashlib
import json
import time
import weakref
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.data_candidate import identity
from app.domain.models.source_connector import Accession, NcbiAssembly, NcbiObservation, NcbiPage, NcbiSearch, NcbiTaxon

BASE = 'https://api.ncbi.nlm.nih.gov/datasets/v2'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_LEVELS = {'Complete Genome': 'complete_genome', 'Chromosome': 'chromosome', 'Scaffold': 'scaffold', 'Contig': 'contig'}
_gates = weakref.WeakKeyDictionary()


async def _rate_limit():
    # Shared by connectors in one event loop. Other workers still honor HTTP 429.
    loop = asyncio.get_running_loop()
    gate = _gates.setdefault(loop, [asyncio.Lock(), 0.0])
    async with gate[0]:
        await asyncio.sleep(max(0, gate[1] - time.monotonic()))
        gate[1] = time.monotonic() + 0.4


class NcbiDatasetsConnector:
    def __init__(self, *, transport=None, limiter=_rate_limit):
        self.transport = transport
        self.limiter = limiter

    async def _get(self, path: str, params: dict) -> dict:
        try:
            async with asyncio.timeout(25):
                await self.limiter()
                async with httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False,
                                             trust_env=False, headers={'Accept': 'application/json', 'User-Agent': 'AI-DataSeek/1.0 (NCBI metadata connector)'}) as client:
                    async with client.stream('GET', BASE + path, params=params) as response:
                        if response.status_code == 429:
                            retry = response.headers.get('Retry-After', '')
                            raise SourceConnectorError('rate_limited', min(int(retry), 3600) if retry.isascii() and retry.isdigit() and len(retry) < 8 else None)
                        if response.status_code in (400, 404):
                            raise SourceConnectorError('query_not_found_or_invalid')
                        if response.status_code != 200:
                            raise SourceConnectorError('upstream_unavailable')
                        if 'application/json' not in response.headers.get('content-type', '').lower():
                            raise SourceConnectorError('invalid_response')
                        data = bytearray()
                        async for block in response.aiter_bytes(chunk_size=65536):
                            if len(data) + len(block) > MAX_RESPONSE_BYTES:
                                raise SourceConnectorError('response_too_large')
                            data.extend(block)
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise SourceConnectorError('invalid_response')
            return payload
        except (httpx.HTTPError, TimeoutError):
            raise SourceConnectorError('upstream_unavailable') from None
        except (ValueError, UnicodeError):
            raise SourceConnectorError('invalid_response') from None

    @staticmethod
    def _observation(report: dict, request: NcbiSearch | None = None) -> NcbiObservation:
        organism, info = report['organism'], report['assembly_info']
        stats = report.get('assembly_stats') or {}
        accession = TypeAdapter(Accession).validate_python(report['accession'])
        database = {'SOURCE_DATABASE_REFSEQ': 'refseq', 'SOURCE_DATABASE_GENBANK': 'genbank'}[report['source_database']]
        if not accession.startswith('GCF_' if database == 'refseq' else 'GCA_'):
            raise ValueError('accession_source_mismatch')
        assembly = NcbiAssembly(accession=accession, current_accession=report.get('current_accession'),
            source_database=database, organism_name=organism['organism_name'], tax_id=organism['tax_id'],
            assembly_level=_LEVELS[info['assembly_level']], reference_genome=(info['refseq_category'] == 'reference genome') if info.get('refseq_category') else None,
            assembly_status=info.get('assembly_status', 'unknown'), total_sequence_length=stats.get('total_sequence_length'),
            gc_percent=stats.get('gc_percent'))
        if request and (request.assembly_source not in ('all', database)
                        or request.assembly_level not in ('all', assembly.assembly_level)
                        or request.reference_only and not assembly.reference_genome):
            raise ValueError('provider_filter_mismatch')
        return NcbiObservation(retrieved_at=int(time.time()), report_sha256=identity(report), assembly=assembly,
            query=request.model_copy(update={'page_token': ''}) if request else None,
            page_token_sha256=hashlib.sha256(request.page_token.encode()).hexdigest() if request and request.page_token else None)

    def _page(self, payload: dict, request: NcbiSearch | None, limit: int) -> NcbiPage:
        try:
            # Empty protobuf JSON alone is ambiguous. Never turn an unverifiable
            # response into a confident "no matching data" result.
            if 'reports' not in payload and payload.get('total_count') != 0:
                raise ValueError('missing_reports')
            reports = payload.get('reports', [])
            if not isinstance(reports, list) or len(reports) > limit or payload.get('messages'):
                raise ValueError('incomplete_provider_response')
            observations = [self._observation(report, request) for report in reports]
            ids = [item.assembly.accession for item in observations]
            if len(ids) != len(set(ids)):
                raise ValueError('duplicate_accession')
            total = payload.get('total_count')
            if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < len(ids)):
                raise ValueError('invalid_count')
            token = payload.get('next_page_token', '')
            if not observations and (token or total not in (None, 0)):
                raise ValueError('inconsistent_empty_page')
            NcbiSearch(taxon='1', page_token=token)
            if request and token and token == request.page_token:
                raise ValueError('repeated_page_token')
            return NcbiPage(observations=sorted(observations, key=lambda item: item.assembly.accession),
                            total_count=total, next_page_token=token)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise SourceConnectorError('invalid_response') from None

    async def _resolve_taxon(self, taxon: str) -> tuple[int, str | None, str | None]:
        if taxon.isdigit():
            return int(taxon), None, None
        payload = await self._get('/taxonomy/taxon/' + quote(taxon, safe='') + '/dataset_report',
                                  {'returned_content': 'METADATA', 'page_size': 20})
        try:
            reports = payload['reports']
            if not isinstance(reports, list) or not 1 <= len(reports) <= 20 or payload.get('messages'):
                raise ValueError('unconfirmed_taxon')
            matches = []
            for report in reports:
                node = report['taxonomy']
                name = node['current_scientific_name']['name']
                if name.casefold() != taxon.casefold():
                    raise ValueError('unconfirmed_taxon')
                domain = (node.get('classification', {}).get('domain') or {}).get('name')
                matches.append(NcbiTaxon(tax_id=node['tax_id'], scientific_name=name,
                                         rank=node.get('rank', 'UNKNOWN'), domain=domain))
            if payload.get('next_page_token'):
                raise SourceConnectorError('taxon_resolution_incomplete')
            if len(matches) != 1:
                raise SourceConnectorError('taxon_ambiguous', taxon_candidates=matches)
            return matches[0].tax_id, matches[0].scientific_name, identity(reports[0])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError('taxon_ambiguous_or_not_found') from None

    async def search(self, request: NcbiSearch) -> NcbiPage:
        tax_id, tax_name, taxonomy_hash = await self._resolve_taxon(request.taxon)
        params = {'filters.assembly_source': request.assembly_source, 'filters.reference_only': str(request.reference_only).lower(),
                  'filters.assembly_version': 'current', 'page_size': request.page_size}
        if request.assembly_level != 'all':
            params['filters.assembly_level'] = request.assembly_level
        if request.page_token:
            params['page_token'] = request.page_token
        payload = await self._get('/genome/taxon/' + str(tax_id) + '/dataset_report', params)
        page = self._page(payload, request, request.page_size)
        return page.model_copy(update={'observations': [NcbiObservation.model_validate({
            **item.model_dump(), 'resolved_taxon_id': tax_id, 'resolved_taxon_name': tax_name,
            'taxonomy_report_sha256': taxonomy_hash,
        }) for item in page.observations]})

    async def describe(self, accession: str) -> NcbiObservation:
        TypeAdapter(Accession).validate_python(accession)
        payload = await self._get('/genome/accession/' + accession + '/dataset_report',
                                  {'filters.assembly_version': 'all_assemblies', 'page_size': 2})
        # NCBI may emit a continuation token whenever page_size is filled,
        # including a single-result query. Request two, still require exactly one.
        page = self._page(payload, None, 2)
        if len(page.observations) != 1 or page.observations[0].assembly.accession != accession or page.next_page_token:
            raise SourceConnectorError('exact_version_unavailable')
        return page.observations[0]
