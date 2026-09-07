from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.application.services.data_center_dataset_service as module
import app.infrastructure.models.documents as documents
from app.application.errors.exceptions import BadRequestError
from app.domain.models.dataset import CuratedDatasetSeed, DatasetStorageType
from app.infrastructure.external.sandbox.dataset_mount_validator import (
    DatasetDirectoryFile, DatasetDirectoryInventory, DatasetDirectoryInspectionError,
)
from app.interfaces.schemas.dataset import dataset_response
from test_dataset_management import FakeDatasetDocument, _seed_payload, _service


@pytest.fixture
def local_import(monkeypatch, tmp_path):
    payload = b"year,value\n2024,1\n"
    (tmp_path / "observations.csv").write_bytes(payload)
    values = _seed_payload(payload)
    values["metadata"].update(curated=True, source_catalog="tpdc", source_url="https://data.tpdc.ac.cn/")
    seed = CuratedDatasetSeed.model_validate(values)
    inventory = DatasetDirectoryInventory(
        canonical_source_directory="/srv/datasets/catalog/example",
        files=(DatasetDirectoryFile(relative_path="observations.csv", size=len(payload)),),
    )
    monkeypatch.setattr(FakeDatasetDocument, "records", {})
    monkeypatch.setattr(module, "DataCenterDatasetDocument", FakeDatasetDocument)
    monkeypatch.setattr(module, "ensure_local_default_node", AsyncMock(return_value=SimpleNamespace(runtime_config={"dataset_allowed_roots": ["/srv/datasets"]})))
    monkeypatch.setattr(documents, "ExecutionNodeDocument", SimpleNamespace(find_one=AsyncMock(return_value=SimpleNamespace(runtime_config={"dataset_allowed_roots": ["/srv/datasets"]}))))
    inspector = Mock(return_value=inventory)
    monkeypatch.setattr(module, "inspect_local_dataset_directory", inspector)
    service = _service(storage_root=tmp_path, seed_root=tmp_path)
    return service, seed, tmp_path, inspector


async def perform(fixture, **kwargs):
    service, seed, view, _ = fixture
    return await service.register_curated_host_directory(seed, storage_directory="/srv/datasets/catalog/example", inspection_directory=view, **kwargs)


@pytest.mark.asyncio
async def test_local_curated_import_is_persistent_read_only_and_path_safe(local_import):
    service, seed, view, inspector = local_import
    item = await perform(local_import)
    inspector.assert_called_once_with("/srv/datasets/catalog/example", configured_roots=["/srv/datasets"])
    assert item.dataset_id == seed.dataset_id and item.created_by is None and not item.is_submission
    assert item.locations[0].storage_type == DatasetStorageType.HOST_PATH
    assert item.locations[0].read_only and item.locations[0].mount_name == "data"
    assert item.files[0].path.endswith("/data/observations.csv")
    public = dataset_response(item).model_dump_json()
    assert "/srv/datasets" not in public and str(view) not in public
    assert "source_path" not in public
    fresh = _service(storage_root=view, seed_root=view)
    fresh.ensure_seed_data = AsyncMock()
    assert (await fresh.get_dataset(seed.dataset_id)).dataset_id == item.dataset_id
    mounts = await fresh.resolve_mounts([item.dataset_id], "local-default")
    assert len(mounts) == 1 and mounts[0].read_only
    assert mounts[0].target.endswith("/data")


@pytest.mark.asyncio
async def test_reimport_preserves_archived_state_and_user_metadata_edits(local_import):
    await perform(local_import)
    document = next(iter(FakeDatasetDocument.records.values()))
    document.enabled = False
    document.name = "Locally edited title"
    again = await perform(local_import)
    assert len(FakeDatasetDocument.records) == 1
    assert not again.enabled and again.name == "Locally edited title"


@pytest.mark.asyncio
async def test_dry_run_checks_bytes_without_inserting_dataset(local_import):
    item = await perform(local_import, dry_run=True)
    assert item.files and FakeDatasetDocument.records == {}
    module.ensure_local_default_node.assert_not_awaited()
    documents.ExecutionNodeDocument.find_one.assert_awaited_once_with({"node_id": "local-default"})


@pytest.mark.asyncio
async def test_dry_run_with_missing_node_uses_settings_without_creating_node(local_import):
    documents.ExecutionNodeDocument.find_one.return_value = None
    await perform(local_import, dry_run=True)
    assert documents.ExecutionNodeDocument.find_one.await_count == 2
    local_import[3].assert_called_once_with("/srv/datasets/catalog/example", configured_roots="/srv/datasets")
    module.ensure_local_default_node.assert_not_awaited()
    assert FakeDatasetDocument.records == {}


@pytest.mark.asyncio
async def test_dry_run_preserves_deleted_node_and_legacy_node_allowlist(local_import):
    node = SimpleNamespace(
        node_id="legacy-id", name="local-default", status="deleted", enabled=False,
        runtime_config={"dataset_allowed_roots": ["/srv/datasets/catalog"]},
        save=AsyncMock(), insert=AsyncMock(),
    )
    documents.ExecutionNodeDocument.find_one.side_effect = [None, node]
    await perform(local_import, dry_run=True)
    local_import[3].assert_called_once_with("/srv/datasets/catalog/example", configured_roots=["/srv/datasets/catalog"])
    assert node.node_id == "legacy-id" and node.status == "deleted" and not node.enabled
    node.save.assert_not_awaited()
    node.insert.assert_not_awaited()
    module.ensure_local_default_node.assert_not_awaited()
    assert FakeDatasetDocument.records == {}


@pytest.mark.asyncio
async def test_import_rejects_checksum_mismatch_before_persistence(local_import):
    (local_import[2] / "observations.csv").write_bytes(b"year,value\n2024,2\n")
    with pytest.raises(BadRequestError, match="checksum"):
        await perform(local_import)
    assert FakeDatasetDocument.records == {}


@pytest.mark.asyncio
async def test_import_requires_complete_host_inventory(local_import):
    inventory = local_import[3].return_value
    local_import[3].return_value = DatasetDirectoryInventory(
        canonical_source_directory=inventory.canonical_source_directory,
        files=(*inventory.files, DatasetDirectoryFile(relative_path="unreviewed.csv", size=1)),
    )
    with pytest.raises(BadRequestError, match="complete host inventory"):
        await perform(local_import)
    assert FakeDatasetDocument.records == {}


@pytest.mark.asyncio
async def test_import_still_requires_allowlisted_real_host_path(local_import):
    local_import[3].side_effect = DatasetDirectoryInspectionError("outside_allowlist", "Directory is outside the allowlist")
    with pytest.raises(BadRequestError, match="allowlist"):
        await perform(local_import)
    assert FakeDatasetDocument.records == {}


@pytest.mark.asyncio
async def test_import_requires_hash_and_size_for_each_file(local_import):
    service, seed, view, inspector = local_import
    seed = seed.model_copy(update={"files": [seed.files[0].model_copy(update={"sha256": None})]})
    with pytest.raises(BadRequestError, match="SHA256"):
        await perform((service, seed, view, inspector))
    inspector.assert_not_called()


@pytest.mark.asyncio
async def test_same_id_cannot_replace_another_manifest(local_import):
    await perform(local_import)
    service, seed, view, inspector = local_import
    other = seed.model_copy(update={"external_id": "another-upstream-dataset"})
    with pytest.raises(BadRequestError, match="conflicts"):
        await perform((service, other, view, inspector))
    assert len(FakeDatasetDocument.records) == 1
