"""Fault-injection clients must exercise the current, real plugin inventory."""
import json
from pathlib import Path

import pytest

import test_database_records_http_safety as records
import test_database_visualization_http_safety as tables
import test_physical_database_http_safety as physical


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [tables, records, physical], ids=["tables", "records", "physical"])
async def test_http_safety_catalog_doubles_cover_real_ids_without_padding(module):
    directory = Path(__file__).resolve().parents[2] / "plugin-host/visualizations"
    expected = {json.loads(path.read_text())["id"] for path in directory.glob("*.json")}
    plugins = module.namespace()["PLUGINS"]
    client_type = physical.Client if module is physical else module.PreferenceClient
    client = client_type(tables.fake_database(), plugins)
    response = await client.request("GET", "/api/v1/visualizations")
    response.raise_for_status()
    catalog = response.json()["data"]
    ids = [plugin["id"] for plugin in catalog["plugins"]]
    assert len(ids) == len(set(ids))
    assert set(ids) == expected
    assert set(client.states) == set(plugins)
    # This read must not mutate test files/preferences or skip the fault path.
    assert client.posts == 0
    assert client.patches == []
