import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.data_connector_service import (
    DataConnectorService, RegisteredConnector, ncbi_registration,
)
from app.domain.models.data_connector import ConnectorManifest
from app.domain.external.data_source import SourceConnectorError
from app.domain.services.tools.data_discovery import DataDiscoveryToolkit


def fixture_service():
    return SimpleNamespace(search=AsyncMock(return_value=SimpleNamespace(
        candidates=[], next_page_token='next-page', total_count=0)), describe=AsyncMock())


@pytest.mark.asyncio
async def test_standard_dispatch_scopes_validated_input_and_preserves_metadata_semantics():
    upstream = fixture_service()
    service = DataConnectorService([ncbi_registration(upstream)])
    result = await service.call('owner-a', 'session-a', 'ncbi-datasets-v2', 'search', {'taxon': '1386'})
    owner, session, args = upstream.search.call_args.args
    assert (owner, session) == ('owner-a', 'session-a')
    assert args.taxon == '1386' and args.page_size == 5
    assert result.kind == 'metadata' and result.files_downloaded is False
    assert result.next_cursor == 'next-page'


@pytest.mark.asyncio
@pytest.mark.parametrize('connector,operation,args,exception', [
    ('unknown', 'search', {'taxon': '1386'}, NotFoundError),
    ('ncbi-datasets-v2', 'acquire', {'taxon': '1386'}, BadRequestError),
    ('ncbi-datasets-v2', '__class__', {}, BadRequestError),
    ('ncbi-datasets-v2', 'search', {'taxon': '1386', 'owner': 'other'}, BadRequestError),
    ('ncbi-datasets-v2', 'search', {'taxon': '1386', 'url': 'http://localhost'}, BadRequestError),
    ('ncbi-datasets-v2', 'search', {'taxon': '1386', 'page_size': 10000}, BadRequestError),
])
async def test_unregistered_operations_and_invalid_arguments_never_reach_provider(connector, operation, args, exception):
    upstream = fixture_service()
    service = DataConnectorService([ncbi_registration(upstream)])
    with pytest.raises(exception):
        await service.call('owner', 'session', connector, operation, args)
    upstream.search.assert_not_called()
    upstream.describe.assert_not_called()


def test_manifest_cannot_claim_unimplemented_capabilities_or_replace_runtime_schema():
    registered = ncbi_registration(fixture_service())
    with pytest.raises(ValueError):
        DataConnectorService([registered, registered])
    with pytest.raises(ValueError):
        DataConnectorService([RegisteredConnector(registered.manifest, {})])
    changed = registered.manifest.model_copy(deep=True)
    changed.operations[0].input_schema['properties'] = {}
    with pytest.raises(ValueError):
        DataConnectorService([RegisteredConnector(changed, registered.operations)])


@pytest.mark.asyncio
async def test_agent_uses_standard_dispatch_without_caller_supplied_session():
    upstream = fixture_service()
    toolkit = DataDiscoveryToolkit(user_id='owner', session_id='session', service=upstream)
    catalog = await toolkit.get_tool('data_connector_list')._arun()
    assert catalog.success and catalog.data['connectors'][0]['schema_version'] == 'data-connector-v1'
    tool = toolkit.get_tool('data_connector_call')
    result = await tool._arun(connector_id='ncbi-datasets-v2', operation='search', arguments={'taxon': '1386'})
    assert result.success and result.data['files_downloaded'] is False
    assert upstream.search.call_args.args[:2] == ('owner', 'session')
    assert tool.execution_contract['effects'] == ['network']
    failed = await tool._arun(connector_id='ncbi-datasets-v2', operation='acquire', arguments={})
    assert not failed.success


@pytest.mark.asyncio
async def test_upstream_failure_is_not_an_empty_success():
    upstream = fixture_service()
    upstream.search.side_effect = SourceConnectorError('rate_limited', retry_after_seconds=8)
    toolkit = DataDiscoveryToolkit(user_id='owner', session_id='session', service=upstream)
    result = await toolkit.get_tool('data_connector_call')._arun(
        connector_id='ncbi-datasets-v2', operation='search', arguments={'taxon': '1386'})
    assert not result.success
    assert result.data['reason'] == 'rate_limited'
    assert result.data['retry_after_seconds'] == 8


def test_published_schema_and_example_match_executable_contract():
    root = Path(__file__).resolve().parents[2] / 'data-connectors'
    assert json.loads((root / 'schema/connector-v1.schema.json').read_text()) == ConnectorManifest.model_json_schema()
    assert json.loads((root / 'packages/ncbi-datasets/connector.json').read_text()) == ncbi_registration(None).manifest.model_dump(mode='json')
