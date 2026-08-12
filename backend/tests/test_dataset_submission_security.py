import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

import app.application.services.data_center_dataset_service as dataset_service_module
from app.application.errors.exceptions import NotFoundError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.domain.models.dataset import (
    DataCenterDataset,
    DatasetFile,
    DatasetLocation,
    DatasetStorageType,
)
from app.infrastructure.models.documents import DataCenterDatasetDocument, TemporaryDatasetDocument
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
    inventory = _directory_inventory()
    inspect_directory = Mock(return_value=inventory)
    ensure_node = AsyncMock(
        return_value=SimpleNamespace(
            runtime_config={"dataset_allowed_roots": ["/srv/datasets"]},
        )
    )
    stored_documents = {}
    inserted_documents = []
    clock = [datetime(2026, 8, 4, 12, 0, tzinfo=UTC)]

    class FakeTemporaryDatasetQuery:
        def __init__(self, query):
            self._query = query
            self._sort_field = None

        def sort(self, field):
            self._sort_field = field.lstrip("+-")
            return self

        async def to_list(self):
            documents = list(stored_documents.values())
            expires_at_query = self._query.get("expires_at", {})
            if "$gt" in expires_at_query:
                documents = [
                    document
                    for document in documents
                    if document.expires_at > expires_at_query["$gt"]
                ]
            if "$lte" in expires_at_query:
                documents = [
                    document
                    for document in documents
                    if document.expires_at <= expires_at_query["$lte"]
                ]
            if self._sort_field:
                documents.sort(key=lambda document: getattr(document, self._sort_field))
            return documents

    class FakeTemporaryDatasetDocument:
        def __init__(self, **values):
            for key, value in values.items():
                setattr(self, key, value)

        async def insert(self):
            if self.dataset_id in stored_documents or any(
                getattr(document, "global_slot", None) == self.global_slot
                or (
                    document.owner_id == self.owner_id
                    and getattr(document, "owner_slot", None) == self.owner_slot
                )
                for document in stored_documents.values()
            ):
                raise DuplicateKeyError("duplicate dataset")
            stored_documents[self.dataset_id] = self
            inserted_documents.append(self)
            return self

        async def delete(self):
            stored_documents.pop(self.dataset_id, None)

        @classmethod
        def find(cls, query):
            return FakeTemporaryDatasetQuery(query)

        @classmethod
        async def find_one(cls, query):
            document = stored_documents.get(query.get("dataset_id"))
            if document is None or document.owner_id != query.get("owner_id"):
                return None
            lower_bound = (query.get("expires_at") or {}).get("$gt")
            if lower_bound is not None and document.expires_at <= lower_bound:
                return None
            return document

        def to_domain(self):
            return self.dataset.model_copy(deep=True)

    monkeypatch.setattr(
        dataset_service_module,
        "inspect_local_dataset_directory",
        inspect_directory,
    )
    monkeypatch.setattr(dataset_service_module, "ensure_local_default_node", ensure_node)
    monkeypatch.setattr(dataset_service_module, "_utc_now", lambda: clock[0])
    monkeypatch.setattr(
        dataset_service_module,
        "TemporaryDatasetDocument",
        FakeTemporaryDatasetDocument,
    )

    service = object.__new__(DataCenterDatasetService)
    service._settings = SimpleNamespace(
        dataset_host_path_allowlist="/fallback-not-used",
        dataset_managed_volume="unused-volume",
    )
    service.ensure_seed_data = AsyncMock()
    return (
        service,
        stored_documents,
        inserted_documents,
        inventory,
        inspect_directory,
        ensure_node,
        clock,
    )


async def _create_submission(
    service: DataCenterDatasetService,
    *,
    created_by: str = "owner-a",
) -> DataCenterDataset:
    return await service.create_submission(
        external_id="external-1",
        name="Submitted dataset",
        summary="Summary",
        keywords=["raster", "raster", "science"],
        storage_directory=" /srv/datasets/center-a ",
        created_by=created_by,
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
    assert payload["files"][0]["path"] == "nested/report.tif"
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
async def test_submission_persists_recursive_inventory_and_survives_service_recreation(monkeypatch):
    (
        service,
        stored_documents,
        inserted_documents,
        inventory,
        inspect_directory,
        ensure_node,
        clock,
    ) = _install_submission_dependencies(monkeypatch)

    dataset = await _create_submission(service)

    ensure_node.assert_awaited_once_with()
    inspect_directory.assert_called_once_with(
        "/srv/datasets/center-a",
        configured_roots=["/srv/datasets"],
    )
    assert len(inserted_documents) == 1
    assert dataset.dataset_id.startswith("tds_")
    assert dataset.created_by == "owner-a"
    assert dataset.is_submission is True
    assert dataset.tags == ["raster", "science"]
    assert dataset.metadata == {
        "temporary": True,
        "inventory_complete": True,
        "inventory_source": "verified_recursive_scan",
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
        "rasters/tile-01.tif",
        "rasters/nested/tile-02.tif",
    ]
    assert inventory.canonical_source_directory not in dataset_response(dataset).model_dump_json()

    stored = stored_documents[dataset.dataset_id]
    assert stored.owner_id == "owner-a"
    assert stored.dataset.files == dataset.files
    assert stored.dataset.name_key == "temporary-submission"
    assert stored.expires_at == clock[0] + timedelta(hours=24)

    # A newly constructed service has no process-local state but can still
    # resolve the submission from MongoDB.
    restarted_service = object.__new__(DataCenterDatasetService)
    restarted_service._settings = service._settings
    restarted_service.ensure_seed_data = AsyncMock()
    restored = await restarted_service.get_dataset(dataset.dataset_id, user_id="owner-a")

    assert restored == dataset
    public_json = dataset_response(restored).model_dump_json()
    assert inventory.canonical_source_directory not in public_json
    assert "expires_at" not in public_json


@pytest.mark.asyncio
async def test_repeating_the_same_submission_returns_distinct_temporary_ids(monkeypatch):
    service, stored_documents, inserted_documents, _, inspect_directory, _, _ = (
        _install_submission_dependencies(monkeypatch)
    )
    generated_ids = iter(["tds_collision", "tds_collision", "tds_second"])
    monkeypatch.setattr(
        dataset_service_module,
        "_new_temporary_dataset_id",
        lambda: next(generated_ids),
    )

    first = await _create_submission(service)
    second = await _create_submission(service)

    assert first.dataset_id == "tds_collision"
    assert second.dataset_id == "tds_second"
    assert first.dataset_id != second.dataset_id
    assert inspect_directory.call_count == 2
    assert set(stored_documents) == {"tds_collision", "tds_second"}
    assert len(inserted_documents) == 2
    assert len({item.dataset_id for item in inserted_documents}) == 2


@pytest.mark.asyncio
async def test_persisted_submission_limits_evict_oldest_owner_and_global_entries(monkeypatch):
    service, stored_documents, _, _, _, _, clock = _install_submission_dependencies(monkeypatch)
    monkeypatch.setattr(dataset_service_module, "TEMPORARY_DATASET_MAX_ENTRIES", 3)
    monkeypatch.setattr(
        dataset_service_module,
        "TEMPORARY_DATASET_MAX_ENTRIES_PER_OWNER",
        2,
    )

    owner_first = await _create_submission(service, created_by="owner-a")
    clock[0] += timedelta(seconds=1)
    owner_second = await _create_submission(service, created_by="owner-a")
    clock[0] += timedelta(seconds=1)
    owner_third = await _create_submission(service, created_by="owner-a")

    assert owner_first.dataset_id not in stored_documents
    assert owner_second.dataset_id in stored_documents
    assert owner_third.dataset_id in stored_documents
    assert sum(
        document.owner_id == "owner-a"
        for document in stored_documents.values()
    ) == 2

    clock[0] += timedelta(seconds=1)
    owner_b = await _create_submission(service, created_by="owner-b")
    clock[0] += timedelta(seconds=1)
    owner_c = await _create_submission(service, created_by="owner-c")

    assert len(stored_documents) == 3
    assert owner_second.dataset_id not in stored_documents
    assert owner_third.dataset_id in stored_documents
    assert owner_b.dataset_id in stored_documents
    assert owner_c.dataset_id in stored_documents
    assert len({document.global_slot for document in stored_documents.values()}) == 3
    assert len({
        (document.owner_id, document.owner_slot)
        for document in stored_documents.values()
    }) == 3


@pytest.mark.asyncio
async def test_concurrent_persisted_submissions_remain_bounded(monkeypatch):
    service, stored_documents, _, _, _, _, _ = _install_submission_dependencies(monkeypatch)
    monkeypatch.setattr(dataset_service_module, "TEMPORARY_DATASET_MAX_ENTRIES", 4)
    monkeypatch.setattr(
        dataset_service_module,
        "TEMPORARY_DATASET_MAX_ENTRIES_PER_OWNER",
        2,
    )

    await asyncio.gather(*(
        _create_submission(service, created_by="owner-a")
        for _ in range(12)
    ))

    assert len(stored_documents) == 2
    assert {document.owner_id for document in stored_documents.values()} == {"owner-a"}
    assert len({document.global_slot for document in stored_documents.values()}) == 2
    assert len({document.owner_slot for document in stored_documents.values()}) == 2


@pytest.mark.asyncio
async def test_persisted_submission_lookup_is_owner_scoped_and_fail_closed_on_expiry(monkeypatch):
    service, stored_documents, _, _, _, _, clock = _install_submission_dependencies(monkeypatch)
    dataset = await _create_submission(service)
    expires_at = stored_documents[dataset.dataset_id].expires_at

    owned = await service.get_dataset(dataset.dataset_id, user_id="owner-a")

    assert owned.dataset_id == dataset.dataset_id
    assert (
        await service.get_dataset(
            dataset.dataset_id,
            include_disabled=True,
            user_id="owner-a",
        )
    ).dataset_id == dataset.dataset_id
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
    with pytest.raises(NotFoundError):
        await service.get_dataset(
            dataset.dataset_id,
            include_disabled=True,
            user_id=None,
        )

    clock[0] = expires_at - timedelta(microseconds=1)
    assert (await service.get_dataset(dataset.dataset_id, user_id="owner-a")).dataset_id == dataset.dataset_id

    # MongoDB's TTL monitor runs asynchronously, so reads must reject an
    # expired document even while it is still physically present.
    clock[0] = expires_at
    assert dataset.dataset_id in stored_documents
    with pytest.raises(NotFoundError):
        await service.get_dataset(dataset.dataset_id, user_id="owner-a")


def test_temporary_dataset_document_has_unique_id_and_absolute_ttl_indexes():
    unique_id_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("unique") is True
    ]
    ttl_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("expireAfterSeconds") == 0
    ]
    owner_slot_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("name")
        == "temporary_dataset_owner_slot_unique"
    ]
    global_slot_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("name")
        == "temporary_dataset_global_slot_unique"
    ]

    assert TemporaryDatasetDocument.Settings.name == "temporary_data_center_datasets"
    assert any(
        list(index["key"].items()) == [("dataset_id", 1)]
        for index in unique_id_indexes
    )
    assert any(
        list(index["key"].items()) == [("expires_at", 1)]
        and index.get("name") == "temporary_dataset_expiration_ttl"
        for index in ttl_indexes
    )
    assert any(
        list(index["key"].items()) == [("owner_id", 1), ("owner_slot", 1)]
        and index.get("unique") is True
        and index.get("partialFilterExpression")
        == {"owner_slot": {"$type": "int"}}
        for index in owner_slot_indexes
    )
    assert any(
        list(index["key"].items()) == [("global_slot", 1)]
        and index.get("unique") is True
        and index.get("partialFilterExpression")
        == {"global_slot": {"$type": "int"}}
        for index in global_slot_indexes
    )


@pytest.mark.asyncio
async def test_catalog_list_keeps_persisted_submissions_hidden(monkeypatch):
    catalog_dataset = DataCenterDataset(
        dataset_id="ds_catalog",
        data_center_id="catalog",
        data_center_name="Catalog",
        name="Catalog dataset",
    )
    catalog_document = SimpleNamespace(to_domain=lambda: catalog_dataset)
    captured_conditions = []

    class FakeCursor:
        async def count(self):
            return 1

        def sort(self, *args):
            return self

        def skip(self, *args):
            return self

        def limit(self, *args):
            return self

        async def to_list(self):
            return [catalog_document]

    class FakeCatalogDocumentModel:
        updated_at = 1

        @staticmethod
        def find(*conditions):
            captured_conditions.extend(conditions)
            return FakeCursor()

    temporary_find = AsyncMock(
        side_effect=AssertionError("catalog listing must not query temporary submissions"),
    )
    monkeypatch.setattr(
        dataset_service_module,
        "DataCenterDatasetDocument",
        FakeCatalogDocumentModel,
    )
    monkeypatch.setattr(TemporaryDatasetDocument, "find_one", temporary_find)
    service = object.__new__(DataCenterDatasetService)
    service.ensure_seed_data = AsyncMock()

    datasets, total = await service.list_datasets(include_disabled=True)

    assert total == 1
    assert [item.dataset_id for item in datasets] == ["ds_catalog"]
    assert {"is_submission": {"$ne": True}} in captured_conditions
    temporary_find.assert_not_awaited()


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
