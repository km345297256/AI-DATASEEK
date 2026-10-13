import asyncio
from unittest.mock import AsyncMock
import httpx
import pytest
from pydantic import ValidationError
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.source_connector import NcbiSearch
from app.domain.models.data_candidate import CandidateInput, DataCandidate
from app.infrastructure.external.data_sources.ncbi import NcbiDatasetsConnector, MAX_RESPONSE_BYTES
from app.application.services.ncbi_discovery_service import NcbiDiscoveryService
from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.domain.services.tools.data_discovery import DataDiscoveryToolkit


def report(accession='GCF_000009045.1', reference=False):
    return {'accession': accession, 'current_accession': accession, 'source_database': 'SOURCE_DATABASE_REFSEQ',
            'organism': {'tax_id': 224308, 'organism_name': 'Bacillus subtilis'},
            'assembly_info': {'assembly_level': 'Complete Genome', 'assembly_status': 'current',
                              'refseq_category': 'reference genome' if reference else 'na'},
            'assembly_stats': {'total_sequence_length': '4215606', 'gc_percent': 43.5},
            'biosample': {'unselected': 'never expose raw provider fields'}}


def connector(handler):
    return NcbiDatasetsConnector(transport=httpx.MockTransport(handler), limiter=AsyncMock())


class Repo:
    def __init__(self): self.rows = {}
    async def put(self, owner, session, kind, key, payload):
        return self.rows.setdefault((owner, session, kind, key), payload)
    async def get(self, owner, session, kind, key):
        return self.rows.get((owner, session, kind, key))


@pytest.mark.asyncio
async def test_filters_pagination_fixed_origin_and_safe_projection():
    observed = []
    def response(request):
        observed.append(request)
        if '/taxonomy/' in request.url.path:
            return httpx.Response(200, json={'reports': [{'taxonomy': {'tax_id': 1423, 'current_scientific_name': {'name': 'Bacillus subtilis'}}}]})
        return httpx.Response(200, json={'reports': [report()], 'total_count': 100, 'next_page_token': 'next-token'})
    client = connector(response)
    page = await client.search(NcbiSearch(taxon='Bacillus subtilis', page_token='previous-token'))
    request = observed[1]
    assert request.url.host == 'api.ncbi.nlm.nih.gov' and request.url.scheme == 'https'
    assert request.url.path.endswith('/1423/dataset_report')
    assert request.url.params['filters.assembly_source'] == 'refseq'
    assert request.url.params['filters.reference_only'] == 'false'
    assert request.url.params['filters.assembly_level'] == 'complete_genome'
    assert request.url.params['page_token'] == 'previous-token'
    assert page.total_count == 100 and page.next_page_token == 'next-token'
    record = page.observations[0]
    assert not record.assembly.reference_genome
    assert record.assembly.total_sequence_length == 4215606
    assert record.resolved_taxon_id == 1423 and record.resolved_taxon_name == 'Bacillus subtilis'
    assert record.taxonomy_report_sha256
    assert record.query.page_token == '' and record.page_token_sha256
    assert 'biosample' not in record.model_dump_json() and 'previous-token' not in record.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason', [(429, 'rate_limited'), (500, 'upstream_unavailable'), (302, 'upstream_unavailable'), (404, 'query_not_found_or_invalid')])
async def test_provider_failures_are_not_empty_success_or_body_leaks(status, reason):
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(status, text='private secret', headers={'Retry-After': '3', 'Location': 'http://127.0.0.1/'})
    with pytest.raises(SourceConnectorError) as error:
        await connector(response).search(NcbiSearch(taxon='1386'))
    assert error.value.reason == reason and 'private' not in str(error.value)
    assert len(calls) == 1  # No redirects or unbounded retries.
    if status == 429: assert error.value.retry_after_seconds == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'reports': 'bad'}, {'reports': [], 'messages': [{'text': 'error'}]}, {'reports': [], 'total_count': -1}])
async def test_invalid_or_ambiguous_payload_fails_closed(payload):
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        await connector(lambda r: httpx.Response(200, json=payload)).search(NcbiSearch(taxon='1386'))


@pytest.mark.asyncio
async def test_explicit_empty_response_and_reference_filter_enforcement():
    empty = await connector(lambda r: httpx.Response(200, json={'total_count': 0})).search(NcbiSearch(taxon='1386'))
    assert empty.observations == [] and empty.total_count == 0
    client = connector(lambda r: httpx.Response(200, json={'reports': [report()]}))
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        await client.search(NcbiSearch(taxon='1386', reference_only=True))
    reference = await connector(lambda r: httpx.Response(200, json={'reports': [report(reference=True)]})).search(NcbiSearch(taxon='1386', reference_only=True))
    assert reference.observations[0].assembly.reference_genome


@pytest.mark.asyncio
async def test_bounded_response_timeout_and_cancellation():
    with pytest.raises(SourceConnectorError, match='response_too_large'):
        await connector(lambda r: httpx.Response(200, content=b'x' * (MAX_RESPONSE_BYTES + 1), headers={'Content-Type': 'application/json'})).search(NcbiSearch(taxon='1386'))
    def timeout(request): raise httpx.ReadTimeout('private upstream body')
    with pytest.raises(SourceConnectorError, match='upstream_unavailable'):
        await connector(timeout).search(NcbiSearch(taxon='1386'))
    async def cancel(request): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await connector(cancel).search(NcbiSearch(taxon='1386'))


@pytest.mark.asyncio
async def test_describe_never_substitutes_a_different_version():
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={'reports': [report('GCF_000009045.2')]})
    with pytest.raises(SourceConnectorError, match='exact_version_unavailable'):
        await connector(response).describe('GCF_000009045.1')
    assert calls[0].url.params['filters.assembly_version'] == 'all_assemblies'
    assert calls[0].url.params['page_size'] == '2'
    with pytest.raises(ValidationError):
        await connector(response).describe('GCF_000009045')
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_page_size_duplicates_and_repeat_token_fail_closed():
    for payload, request in [({'reports': [report(), report()]}, NcbiSearch(taxon='1')),
                             ({'reports': [report(), report('GCF_000009046.1')]}, NcbiSearch(taxon='1', page_size=1)),
                             ({'reports': [report()], 'next_page_token': 'same'}, NcbiSearch(taxon='1', page_token='same'))]:
        with pytest.raises(SourceConnectorError):
            await connector(lambda r: httpx.Response(200, json=payload)).search(request)


@pytest.mark.asyncio
async def test_discovery_saves_authoritative_observation_and_separate_plan():
    raw = report()
    service = NcbiDiscoveryService(Repo(), connector(lambda r: httpx.Response(200, json={'reports': [raw]})))
    request = NcbiSearch(taxon='1386')
    first = (await service.search('owner', 's', request)).candidates[0]
    repeat = (await service.search('owner', 's', request)).candidates[0]
    assert first == repeat and first.provenance == 'ncbi_datasets'
    assert 'unselected' not in first.model_dump_json()
    plan = await service.plan_acquisition('owner', 's', first.candidate_id)
    assert plan.status == 'not_started' and not plan.execution_available
    assert plan.download_size_bytes is None and plan.reported_sequence_bases == 4215606
    assert len(service.repository.rows) == 1  # No file/version/download was fabricated.
    with pytest.raises(NotFoundError): await service.plan_acquisition('other', 's', first.candidate_id)
    with pytest.raises(NotFoundError): await service.plan_acquisition('owner', 'other', first.candidate_id)
    raw['assembly_stats']['total_sequence_length'] = '4215607'
    changed = (await service.search('owner', 's', request)).candidates[0]
    assert changed.candidate_id != first.candidate_id and changed.asset_id == first.asset_id
    stored = await service.candidates.candidate('owner', 's', first.candidate_id)
    assert stored.discovery.assembly.total_sequence_length == 4215606


@pytest.mark.asyncio
async def test_manual_reference_cannot_claim_official_evidence():
    service = NcbiDiscoveryService(Repo(), None)
    manual = await service.candidates.create('owner', 's', CandidateInput(title='NCBI', source_url='https://www.ncbi.nlm.nih.gov/', external_id='GCF_000009045.1'))
    with pytest.raises(BadRequestError): await service.plan_acquisition('owner', 's', manual.candidate_id)
    with pytest.raises(ValidationError):
        DataCandidate(**{**manual.model_dump(), 'provenance': 'ncbi_datasets'})


@pytest.mark.asyncio
async def test_real_tool_wrapper_preserves_owner_and_declares_network_contract():
    client = connector(lambda r: httpx.Response(200, json={'reports': [report()]}))
    service = NcbiDiscoveryService(Repo(), client)
    toolkit = DataDiscoveryToolkit(user_id='owner', session_id='s', service=service)
    tool = toolkit.get_tool('ncbi_search_genomes')
    assert tool.execution_contract['effects'] == ['network']
    assert 'self' not in tool.args_schema.model_fields
    result = await tool.ainvoke({'type': 'tool_call', 'id': 'search', 'name': tool.name, 'args': {'taxon': '1386'}})
    assert result.artifact.success is True
    assert result.artifact.data['files_downloaded'] is False
    assert all(key[:2] == ('owner', 's') for key in service.repository.rows)
    failed = await tool.ainvoke({'type': 'tool_call', 'id': 'search2', 'name': tool.name, 'args': {'taxon': 'https://evil.test'}})
    assert failed.artifact.success is False


@pytest.mark.asyncio
async def test_tool_reports_rate_limit_as_failure_with_retry_hint():
    client = connector(lambda r: httpx.Response(429, headers={'Retry-After': '8'}))
    service = NcbiDiscoveryService(Repo(), client)
    toolkit = DataDiscoveryToolkit(user_id='owner', session_id='s', service=service)
    result = await toolkit.get_tool('ncbi_search_genomes')._arun(taxon='1386')
    assert result.success is False and result.data == {'reason': 'rate_limited', 'retry_after_seconds': 8}
    assert service.repository.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('nodes', [[], [{'tax_id': 1, 'current_scientific_name': {'name': 'Different'}}],
    [{'tax_id': 1, 'current_scientific_name': {'name': 'Bacillus'}}, {'tax_id': 2, 'current_scientific_name': {'name': 'Bacillus'}}]])
async def test_taxonomy_resolution_never_guesses_an_ambiguous_or_different_name(nodes):
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={'reports': [{'taxonomy': node} for node in nodes]})
    with pytest.raises(SourceConnectorError, match='taxon_ambiguous'):
        await connector(response).search(NcbiSearch(taxon='Bacillus'))
    assert len(calls) == 1 and '/taxonomy/' in calls[0].url.path


@pytest.mark.asyncio
async def test_ambiguous_taxon_exposes_bounded_classification_choices_without_searching():
    nodes = [{'taxonomy': {'tax_id': taxid, 'rank': 'GENUS', 'current_scientific_name': {'name': 'Bacillus'},
                           'classification': {'domain': {'name': domain}}}}
             for taxid, domain in [(1386, 'Bacteria'), (55087, 'Eukaryota')]]
    service = NcbiDiscoveryService(Repo(), connector(lambda r: httpx.Response(200, json={'reports': nodes})))
    tool = DataDiscoveryToolkit(user_id='owner', session_id='s', service=service).get_tool('ncbi_search_genomes')
    result = await tool._arun(taxon='Bacillus')
    assert not result.success and result.data['reason'] == 'taxon_ambiguous'
    assert [(x['tax_id'], x['domain']) for x in result.data['taxon_candidates']] == [(1386, 'Bacteria'), (55087, 'Eukaryota')]
    assert not service.repository.rows
