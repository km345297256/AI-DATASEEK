import pytest

from app.domain.models.dataset import DatasetFile, MountedDataset
from app.domain.services.tools.dataset_catalog import DatasetCatalogToolkit


def _dataset() -> MountedDataset:
    return MountedDataset(
        dataset_id="tds_test",
        name="Climate",
        data_center_id="center",
        data_center_name="Center",
        sandbox_path="/home/ubuntu/datasets/tds_test",
        files=[
            DatasetFile(path="monthly/rain_195301.nc", size=12),
            DatasetFile(path="archive/rain_195301.nc", size=13),
            DatasetFile(path="monthly/snow_195301.nc", size=24),
        ],
        metadata={"inventory_complete": True},
    )


@pytest.mark.asyncio
async def test_catalog_tools_expose_files_and_refuse_ambiguous_reference():
    toolkit = DatasetCatalogToolkit()
    toolkit.set_datasets([_dataset()])

    listed = await toolkit.get_tool("list_dataset_files").ainvoke({
        "id": "list", "args": {"query": "rain", "limit": 10},
    })
    assert listed.artifact.data["match_count"] == 2
    assert listed.artifact.data["files"][0]["filename"] == "rain_195301.nc"

    resolved = await toolkit.get_tool("resolve_dataset_file").ainvoke({
        "id": "resolve", "args": {"reference": "monthly/rain_195301.nc"},
    })
    assert resolved.artifact.data["status"] == "resolved"

    ambiguous = await toolkit.get_tool("resolve_dataset_file").ainvoke({
        "id": "ambiguous", "args": {"reference": "rain_195301.nc"},
    })
    assert ambiguous.artifact.success is False
    assert ambiguous.artifact.data["status"] == "ambiguous"
