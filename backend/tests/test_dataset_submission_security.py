from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

import app.application.services.data_center_dataset_service as dataset_service_module
from app.application.errors.exceptions import NotFoundError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.application.services.temporary_dataset_registry import TemporaryDatasetRegistry
from app.domain.models.dataset import (
    DataCenterDataset,
    DatasetFile,
    DatasetLocation,
    DatasetStorageType,
)
from app.infrastructure.external.sandbox.dataset_mount_validator import (
    DatasetDirectoryFile,
    DatasetDirectoryInventory,
)
from app.interfaces.schemas.dataset import DatasetSubmissionRequest, dataset_response


def _submission_dataset(
    *,
    dataset_id: str = "tds_private",
    created_by: str = "owner-a",
    files: list[DatasetFile] | None = None,
    locations: list[DatasetLocation] | None = None,
) -> DataCenterDataset:
    return DataCenterDataset(
        dataset_id=dataset_id,
        external_id="external-1",
        data_center_id="dataset-chat-demo",
        data_center_name="Test datasets",
        name="Private dataset",
        description="Dataset submitted for an analysis session.",
        files=files or [],
        locations=locations or [],
        enabled=True,
        is_submission=True,
        created_by=created_by,
    )


def _directory_inventory() -> DatasetDirectoryInventory:
    return DatasetDirectoryInventory(
        canonical_source_directory="/srv/datasets/center-a",
        files=(
            DatasetDirectoryFile(relative_path="metadata.json", size=42),
            DatasetDirectoryFile(relative_path="rasters/tile-01.tif", size=1_024),
            DatasetDirectoryFile(relative_path="rasters/nested/tile-02.tif", size=2_048),
        ),
    )


def _install_submission_dependencies(monkeypatch):
    registry = TemporaryDatasetRegistry()
    inventory = _directory_inventory()
    inspect_directory = Mock(return_value=inventory)
    ensure_node = AsyncMock(
        return_value=SimpleNamespace(
            runtime_config={"dataset_allowed_roots": ["/srv/datasets"]},
        )
    )
    forbidden_insert = AsyncMock(
        side_effect=AssertionError("temporary submissions must never be inserted into MongoDB"),
    )

    monkeypatch.setattr(
        dataset_service_module,
        "get_temporary_dataset_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        dataset_service_module,
        "inspect_local_dataset_directory",
        inspect_directory,
    )
    monkeypatch.setattr(dataset_service_module, "ensure_local_default_node", ensure_node)
    monkeypatch.setattr(
        dataset_service_module.DataCenterDatasetDocument,
        "insert",
        forbidden_insert,
    )

    service = object.__new__(DataCenterDatasetService)
    service._settings = SimpleNamespace(
        dataset_host_path_allowlist="/fallback-not-used",
        dataset_managed_volume="unused-volume",
    )
    return service, registry, inventory, inspect_directory, ensure_node, forbidden_insert


async def _create_submission(service: DataCenterDatasetService) -> DataCenterDataset:
    return await service.create_submission(
        external_id="external-1",
        name="Submitted dataset",
        summary="Summary",
        keywords=["raster", "raster", "science"],
        storage_directory=" /srv/datasets/center-a ",
        created_by="owner-a",
    )


def test_public_dataset_response_hides_locations_and_real_storage_paths():
    source_path = "/srv/private/tenant-a/report.tif"
    dataset = _submission_dataset(
        files=[
            DatasetFile(
                path="sources/dsl_report/nested/report.tif",
                size=42,
                role="data",
            )
        ],
        locations=[
            DatasetLocation(
                location_id="dsl_report",
                node_id="local-docker",
                storage_type=DatasetStorageType.HOST_PATH,
                source_path=source_path,
                mount_name="tenant-a",
                verified=True,
            )
        ],
    )

    response = dataset_response(dataset)
    payload = response.model_dump(mode="json")
    serialized = response.model_dump_json()

    assert payload["locations"] == []
    assert payload["files"][0]["name"] == "report.tif"
    assert payload["files"][0]["path"] == "report.tif"
    assert "nested" not in payload["files"][0]["path"]
    assert "source_path" not in serialized
    assert source_path not in serialized
    assert "/srv/private" not in serialized


def test_submission_schema_uses_one_normalized_storage_directory():
    request = DatasetSubmissionRequest(
        external_id="external-1",
        name="Dataset",
        summary="Summary",
        keywords=[" raster ", "raster", "science"],
        storage_directory=" /srv/datasets/example ",
    )

    assert request.keywords == ["raster", "science"]
    assert request.storage_directory == "/srv/datasets/example"

    with pytest.raises(ValidationError):
        DatasetSubmissionRequest(
            external_id="external-1",
            name="Dataset",
            summary="Summary",
            keywords=["  "],
            storage_directory="/srv/datasets/example",
        )


@pytest.mark.asyncio
async def test_submission_builds_recursive_files_in_memory_and_never_inserts_mongo(monkeypatch):
    (
        service,
        registry,
        inventory,
        inspect_directory,
        ensure_node,
        forbidden_insert,
    ) = _install_submission_dependencies(monkeypatch)

    dataset = await _create_submission(service)

    ensure_node.assert_awaited_once_with()
    inspect_directory.assert_called_once_with(
        "/srv/datasets/center-a",
        configured_roots=["/srv/datasets"],
    )
    forbidden_insert.assert_not_awaited()
    assert dataset.dataset_id.startswith("tds_")
    assert dataset.created_by == "owner-a"
    assert dataset.is_submission is True
    assert dataset.tags == ["raster", "science"]
    assert dataset.metadata == {
        "temporary": True,
        "recursive_file_count": 3,
        "total_size_bytes": inventory.total_size,
    }
    assert len(dataset.locations) == 1
    assert dataset.locations[0].source_path == inventory.canonical_source_directory
    assert dataset.locations[0].mount_name == "center-a"
    assert dataset.locations[0].read_only is True

    prefix = (
        f"sources/{dataset.locations[0].location_id}/"
        f"{dataset.locations[0].mount_name}/"
    )
    assert [item.path.removeprefix(prefix) for item in dataset.files] == [
        "metadata.json",
        "rasters/tile-01.tif",
        "rasters/nested/tile-02.tif",
    ]
    assert [item.size for item in dataset.files] == [42, 1_024, 2_048]

    public_payload = dataset_response(dataset).model_dump(mode="json")
    assert [item["name"] for item in public_payload["files"]] == [
        "metadata.json",
        "tile-01.tif",
        "tile-02.tif",
    ]
    assert [item["path"] for item in public_payload["files"]] == [
        "metadata.json",
        "tile-01.tif",
        "tile-02.tif",
    ]
    assert inventory.canonical_source_directory not in dataset_response(dataset).model_dump_json()

    stored = await registry.get(dataset.dataset_id)
    assert stored is not None
    assert stored.owner_id == "owner-a"
    assert stored.dataset.files == dataset.files


@pytest.mark.asyncio
async def test_repeating_the_same_submission_returns_distinct_temporary_ids(monkeypatch):
    service, registry, _, inspect_directory, _, forbidden_insert = (
        _install_submission_dependencies(monkeypatch)
    )

    first = await _create_submission(service)
    second = await _create_submission(service)

    assert first.dataset_id.startswith("tds_")
    assert second.dataset_id.startswith("tds_")
    assert first.dataset_id != second.dataset_id
    assert inspect_directory.call_count == 2
    assert await registry.size() == 2
    forbidden_insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_temporary_submission_lookup_is_scoped_to_registry_owner(monkeypatch):
    service, _, _, _, _, forbidden_insert = _install_submission_dependencies(monkeypatch)
    forbidden_find_one = AsyncMock(
        side_effect=AssertionError("temporary lookup must not fall through to MongoDB"),
    )
    monkeypatch.setattr(
        dataset_service_module.DataCenterDatasetDocument,
        "find_one",
        forbidden_find_one,
    )
    dataset = await _create_submission(service)

    owned = await service.get_dataset(dataset.dataset_id, user_id="owner-a")

    assert owned.dataset_id == dataset.dataset_id
    with pytest.raises(NotFoundError):
        await service.get_dataset(dataset.dataset_id, user_id="intruder")
    with pytest.raises(NotFoundError):
        await service.get_dataset(
            dataset.dataset_id,
            include_disabled=True,
            user_id="intruder",
        )
    with pytest.raises(NotFoundError):
        await service.get_dataset(dataset.dataset_id, user_id=None)
    forbidden_insert.assert_not_awaited()
    forbidden_find_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_sources_resolve_to_unique_nested_read_only_targets():
    locations = [
        DatasetLocation(
            location_id="dsl_report_a",
            node_id="node-a",
            storage_type=DatasetStorageType.HOST_PATH,
            source_path="/srv/center-a/report.tif",
            verified=True,
        ),
        DatasetLocation(
            location_id="dsl_report_b",
            node_id="node-a",
            storage_type=DatasetStorageType.HOST_PATH,
            source_path="/srv/center-b/report.tif",
            verified=True,
        ),
        DatasetLocation(
            location_id="dsl_metadata",
            node_id="node-a",
            storage_type=DatasetStorageType.HOST_PATH,
            source_path="/srv/center-c/metadata.json",
            verified=True,
        ),
    ]
    dataset = _submission_dataset(locations=locations)
    service = object.__new__(DataCenterDatasetService)
    service._settings = SimpleNamespace(dataset_managed_volume="unused-volume")
    service.get_dataset = AsyncMock(return_value=dataset)

    mounts = await service.resolve_mounts(
        [dataset.dataset_id],
        "node-a",
        user_id="owner-a",
    )

    service.get_dataset.assert_awaited_once_with(dataset.dataset_id, user_id="owner-a")
    assert [mount.display_name for mount in mounts] == [
        "report.tif",
        "report-2.tif",
        "metadata.json",
    ]
    assert len({mount.target for mount in mounts}) == len(locations)
    assert all(mount.read_only is True for mount in mounts)

    dataset_root = PurePosixPath("/home/ubuntu/datasets") / dataset.dataset_id
    for mount in mounts:
        relative_target = PurePosixPath(mount.target).relative_to(dataset_root)
        assert relative_target.parts == ("sources", mount.source_id, mount.display_name)
        assert mount.source not in mount.target

