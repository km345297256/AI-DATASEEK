import httpx
import pytest
from pydantic import ValidationError
from app.infrastructure.external.data_sources.entrez import EntrezTopicConnector, TopicSearch
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.data_candidate import CandidateInput
from app.application.services.data_connector_service import build_data_connector_service
from app.domain.services.tools.data_discovery import DataDiscoveryToolkit
from app.domain.services.tools.tool_contract import audited_public_data_read

async def no_limit(): pass


def client(database, *, malformed=False, empty=False):
    def respond(request):
        assert request.url.host == 'eutils.ncbi.nlm.nih.gov'
        assert request.url.params['db'] == database
        if request.url.path.endswith('esearch.fcgi'):
            assert '(aging OR senescence)' in request.url.params['term']
            assert ('gse[ETYP]' in request.url.params['term']) == (database == 'gds')
            return httpx.Response(200, json={'esearchresult': {'count': '0' if empty else '2', 'idlist': [] if empty else ['123']}})
        record = {'accession':'GSE123', 'title':'Aging study', 'taxon':'Homo sapiens', 'n_samples':4}
        if database == 'sra':
            record = {'expxml': '<Summary><Title>Aging</Title></Summary><Experiment acc="SRX123"/><Study acc="SRP123" name="Aging study"/><Organism ScientificName="Homo sapiens"/>', 'runs':'<Run acc="SRR123"/>'}
            if malformed: record['expxml'] = '<!DOCTYPE x [<!ENTITY x "a">]>'
        return httpx.Response(200, json={'result': {'123': record}})
    return EntrezTopicConnector(transport=httpx.MockTransport(respond), limiter=no_limit)


@pytest.mark.asyncio
@pytest.mark.parametrize('db,acc', [('gds','GSE123'),('sra','SRX123')])
async def test_topic_query_page_and_provenance(db, acc):
    result = await client(db).search(db, TopicSearch(query='衰老', page_size=1))
    assert result.total_count == 2 and result.next_cursor == '1'
    assert not result.files_downloaded and result.resources[0].id == acc
    assert result.resources[0].metadata['original_query'] == '衰老'
    assert acc in result.resources[0].source_url
    if db == 'sra': assert result.resources[0].metadata['run_accessions'] == ['SRR123']


@pytest.mark.asyncio
async def test_empty_and_malformed_are_distinct():
    result = await client('gds', empty=True).search('gds', TopicSearch(query='衰老'))
    assert result.total_count == 0 and result.resources == []
    with pytest.raises(SourceConnectorError):
        await client('sra', malformed=True).search('sra', TopicSearch(query='衰老', page_size=1))


def test_query_limits_and_public_geo_url():
    for kwargs in ({'query':''}, {'query':'aging','page_size':21}, {'query':'aging','offset':10001}, {'query':'aging','url':'http://localhost'}):
        with pytest.raises(ValidationError): TopicSearch(**kwargs)
    def url(value): return CandidateInput(title='x', external_id='x', source_url=value).source_url
    base='https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi'
    assert url(base+'?acc=GSE123') == base+'?acc=GSE123'
    assert url(base+'?acc=GSE123&key=secret') == base
    assert url('https://example.org/?acc=GSE123') == 'https://example.org/'


def test_catalog_and_trusted_read_allowlist():
    catalog = build_data_connector_service(None).catalog()
    assert [c.id for c in catalog] == ['ncbi-datasets-v2', 'ncbi-geo', 'ncbi-sra']
    # Construct without backing services: authorization identifies the actual registered tool.
    toolkit = DataDiscoveryToolkit(user_id='owner', session_id='session', service=object())
    tool = toolkit.get_tool('data_connector_call')
    assert not audited_public_data_read(tool, {'connector_id':[], 'operation':'search'})
    assert not audited_public_data_read(tool, {'connector_id':'ncbi-geo', 'operation':[]})
    for connector in ['ncbi-geo', 'ncbi-sra']:
        assert audited_public_data_read(tool, {'connector_id':connector,'operation':'search'})
        assert not audited_public_data_read(tool, {'connector_id':connector,'operation':'acquire'})


@pytest.mark.asyncio
@pytest.mark.parametrize('response,reason', [
    (httpx.Response(429), 'rate_limited'),
    (httpx.Response(302, headers={'location':'http://localhost/'}), 'upstream_unavailable'),
    (httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1)), 'response_too_large'),
    (httpx.Response(200, json={'esearchresult':{'count':'1','idlist':[]}}), 'invalid_response'),
])
async def test_failures_never_become_empty_success(response, reason):
    calls=[]
    def respond(request):
        calls.append(request)
        return response
    source=EntrezTopicConnector(transport=httpx.MockTransport(respond), limiter=no_limit)
    with pytest.raises(SourceConnectorError) as error:
        await source.search('gds', TopicSearch(query='aging'))
    assert error.value.reason == reason
    assert len(calls) == 1
