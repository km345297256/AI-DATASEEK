from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

import app.application.services.data_center_dataset_service as service_module
import app.interfaces.api.dataset_routes as dataset_routes
from app.application.errors.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.domain.models.dataset import (
    CuratedDatasetFile,
    CuratedDatasetSeed,
    DataCenterDataset,
    DatasetStorageType,
)
from app.domain.models.user import User, UserRole
from app.domain.services.domain_presets import get_domain_preset, list_domain_presets
from app.infrastructure.external.sandbox.dataset_mount_validator import (
    DatasetDirectoryFile,
    DatasetDirectoryInventory,
)
from app.interfaces.schemas.dataset import (
    DatasetMetadataUpdateRequest,
    DatasetRegistrationRequest,
    dataset_response,
)


class FakeDatasetDocument:
    updated_at = 1
    records: dict[str, "FakeDatasetDocument"] = {}

    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)
        self.saved = 0

    async def insert(self):
        self.records[self.dataset_id] = self
        return self

    async def save(self):
        self.saved += 1
        self.records[self.dataset_id] = self
        return self

    def to_domain(self):
        fields = DataCenterDataset.model_fields
        return DataCenterDataset.model_validate({
            key: getattr(self, key)
            for key in fields
            if hasattr(self, key)
        })

    @classmethod
    async def find_one(cls, query):
        return cls.records.get(query.get("dataset_id"))

    @classmethod
    def find(cls, query):
        class Cursor:
            def project(self, model):
                self.model = model
                return self

            async def to_list(self):
                return [
                    self.model(dataset_id=dataset_id)
                    for dataset_id in query["dataset_id"]["$in"]
                    if dataset_id in cls.records
                ]

        return Cursor()


def _service(*, storage_root=None, seed_root=None):
    service = object.__new__(DataCenterDatasetService)
    service._settings = SimpleNamespace(
        dataset_host_path_allowlist="/srv/datasets",
        dataset_managed_volume="managed-volume",
    )
    service._storage_root = storage_root
    service._seed_root = seed_root
    # Filesystem/catalog unit fixtures do not allocate real Docker helpers.
    # Dedicated readability tests exercise this boundary and its real identity.
    service._prepare_managed_execution_view = AsyncMock()
    return service


def _user(role=UserRole.USER, user_id="owner-a"):
    return User(
        id=user_id,
        fullname="Owner A",
        email="owner@example.com",
        role=role,
    )


def test_management_requests_are_trimmed_bounded_and_storage_immutable():
    request = DatasetRegistrationRequest(
        name=" Dataset ",
        description=" Description ",
        domain=" tabular ",
        storage_directory=" /srv/datasets/example ",
    )
    assert request.model_dump() == {
        "name": "Dataset",
        "description": "Description",
        "domain": "tabular",
        "storage_directory": "/srv/datasets/example",
    }
    assert DatasetMetadataUpdateRequest(description=" ").description == ""
    with pytest.raises(ValidationError):
        DatasetMetadataUpdateRequest()
    with pytest.raises(ValidationError):
        DatasetRegistrationRequest(
            name="Dataset", description="", domain=" ",
            storage_directory="/srv/datasets/example",
        )
    with pytest.raises(ValidationError):
        DatasetRegistrationRequest(
            name="Dataset", description="", domain="invented-domain",
            storage_directory="/srv/datasets/example",
        )


@pytest.mark.asyncio
async def test_registration_is_persistent_owner_scoped_and_publicly_path_safe(monkeypatch):
    FakeDatasetDocument.records = {}
    inventory = DatasetDirectoryInventory(
        canonical_source_directory="/srv/datasets/private-a",
        files=(DatasetDirectoryFile(relative_path="nested/data.csv", size=42),),
    )
    monkeypatch.setattr(service_module, "DataCenterDatasetDocument", FakeDatasetDocument)
    monkeypatch.setattr(service_module, "_new_registered_dataset_id", lambda: "dsr_" + "a" * 32)
    monkeypatch.setattr(
        service_module,
        "ensure_local_default_node",
        AsyncMock(return_value=SimpleNamespace(runtime_config={"dataset_allowed_roots": ["/srv/datasets"]})),
    )
    inspect = Mock(return_value=inventory)
    monkeypatch.setattr(service_module, "inspect_local_dataset_directory", inspect)
    service = _service()

    dataset = await service.create_registration(
        name="Private table", description="Owner data", domain="tabular",
        storage_directory=" /srv/datasets/private-a ", created_by="owner-a",
    )

    inspect.assert_called_once_with(
        "/srv/datasets/private-a", configured_roots=["/srv/datasets"],
    )
    assert dataset.dataset_id == "dsr_" + "a" * 32
    assert dataset.is_submission and dataset.created_by == "owner-a" and dataset.enabled
    assert dataset.domain == "tabular"
    assert dataset.locations[0].read_only is True
    assert FakeDatasetDocument.records[dataset.dataset_id].name_key == f"registration:{dataset.dataset_id}"
    public = dataset_response(dataset).model_dump_json()
    assert "/srv/datasets/private-a" not in public
    assert "source_path" not in public and "mount_name" not in public
    assert "nested/data.csv" in public


@pytest.mark.asyncio
async def test_management_list_encodes_public_plus_owner_visibility(monkeypatch):
    captured = []

    class Cursor:
        async def count(self): return 0
        def sort(self, *_): return self
        def skip(self, *_): return self
        def limit(self, *_): return self
        async def to_list(self): return []

    class Documents:
        updated_at = 1
        @staticmethod
        def find(*conditions):
            captured.append(conditions)
            return Cursor()

    monkeypatch.setattr(service_module, "DataCenterDatasetDocument", Documents)
    service = _service()
    service.ensure_seed_data = AsyncMock()

    await service.list_managed_datasets(
        actor_id="owner-a", is_admin=False, include_archived=True,
    )
    visibility = captured[-1][0]["$or"]
    assert visibility == [
        {"is_submission": {"$ne": True}, "enabled": True},
        {"is_submission": True, "created_by": "owner-a"},
    ]

    await service.list_managed_datasets(actor_id="admin", is_admin=True)
    assert captured[-1] == ({"enabled": True},)


@pytest.mark.asyncio
async def test_update_and_archive_enforce_owner_without_deleting_source(monkeypatch):
    FakeDatasetDocument.records = {}
    owner = FakeDatasetDocument(
        dataset_id="dsr_owner", data_center_id="owner-registration",
        data_center_name="Registered datasets", name="Before", name_key="registration:dsr_owner",
        description="Before", domain="tabular", enabled=True, is_submission=True,
        created_by="owner-a", locations=[], files=[], metadata={}, tags=[],
    )
    public = FakeDatasetDocument(
        dataset_id="seed_public", data_center_id="center", data_center_name="Center",
        name="Public", name_key="public", description="", domain="geoscience",
        enabled=True, is_submission=False, created_by=None, locations=[], files=[], metadata={}, tags=[],
    )
    FakeDatasetDocument.records = {owner.dataset_id: owner, public.dataset_id: public}
    monkeypatch.setattr(service_module, "DataCenterDatasetDocument", FakeDatasetDocument)
    service = _service()

    updated = await service.update_registration(
        owner.dataset_id, actor_id="owner-a", is_admin=False,
        name="After", description="Updated", domain="spectroscopy",
    )
    assert (updated.name, updated.description, updated.domain) == (
        "After", "Updated", "spectroscopy",
    )
    assert owner.name_key == "registration:dsr_owner" and owner.saved == 1

    service.ensure_seed_data = AsyncMock()
    archived_owner = await service.archive_dataset(
        owner.dataset_id, actor_id="owner-a", is_admin=False,
    )
    assert archived_owner.enabled is False and owner.locations == []
    with pytest.raises(NotFoundError):
        await service.get_dataset(owner.dataset_id, user_id="owner-a")
    assert (
        await service.get_dataset(
            owner.dataset_id, include_disabled=True, user_id="owner-a",
        )
    ).dataset_id == owner.dataset_id
    with pytest.raises(NotFoundError):
        await service.get_dataset(
            owner.dataset_id, include_disabled=True, user_id="owner-b",
        )

    with pytest.raises(NotFoundError):
        await service.archive_dataset(public.dataset_id, actor_id="owner-a", is_admin=False)

    archived = await service.archive_dataset(public.dataset_id, actor_id="admin", is_admin=True)
    assert archived.enabled is False and public.saved == 1
    assert public.locations == []  # Archiving did not mutate or delete storage identity.
    with pytest.raises(NotFoundError):
        await service.update_registration("tds_legacy", actor_id="owner-a", is_admin=False, name="No")


def _seed_payload(file_bytes: bytes) -> dict:
    return {
        "dataset_id": "seed_climate",
        "external_id": "external-climate",
        "data_center_id": "international-center",
        "data_center_name": "International Center",
        "name": "Climate seed",
        "description": "A bundled sample",
        "domain": "geoscience",
        "data_type": "CSV",
        "tags": ["climate"],
        "metadata": {"license": "CC-BY-4.0"},
        "files": [{
            "path": "observations.csv", "role": "data",
            "size": len(file_bytes), "sha256": hashlib.sha256(file_bytes).hexdigest(),
        }],
    }


@pytest.mark.asyncio
async def test_bundled_seed_is_all_or_nothing_checksum_verified_and_managed(monkeypatch, tmp_path):
    FakeDatasetDocument.records = {}
    seed_root = tmp_path / "seeds"
    source = seed_root / "seed_climate"
    source.mkdir(parents=True)
    file_bytes = b"year,value\n2024,1\n"
    (source / "observations.csv").write_bytes(file_bytes)
    (source / "manifest.json").write_text(json.dumps(_seed_payload(file_bytes)), encoding="utf-8")
    storage_root = tmp_path / "managed"
    storage_root.mkdir()
    monkeypatch.setattr(service_module, "DataCenterDatasetDocument", FakeDatasetDocument)
    service = _service(storage_root=storage_root, seed_root=seed_root)

    await service.ensure_seed_data()

    stored = FakeDatasetDocument.records["seed_climate"].to_domain()
    assert stored.domain == "geoscience" and stored.is_submission is False
    assert len(stored.locations) == 1
    assert stored.locations[0].node_id == "local-default"
    assert stored.locations[0].storage_type == DatasetStorageType.MANAGED_UPLOAD
    assert stored.locations[0].source_path == "seed_climate"
    assert stored.locations[0].verified is True
    assert (storage_root / "seed_climate" / "observations.csv").read_bytes() == file_bytes

    FakeDatasetDocument.records = {}
    bad = _seed_payload(file_bytes)
    bad["files"][0]["sha256"] = "0" * 64
    (source / "manifest.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(BadRequestError, match="checksum"):
        await service.ensure_seed_data()
    assert FakeDatasetDocument.records == {}


@pytest.mark.parametrize("unsafe_path", ["../outside.csv", "/outside.csv", "nested\\file.csv"])
def test_curated_seed_file_paths_fail_closed_before_copy(tmp_path, unsafe_path):
    source = tmp_path / "seed"
    source.mkdir()
    (tmp_path / "outside.csv").write_text("private", encoding="utf-8")
    declaration = CuratedDatasetFile(
        path=unsafe_path,
        size=7,
        sha256=hashlib.sha256(b"private").hexdigest(),
    )

    with pytest.raises(BadRequestError, match="declaration"):
        DataCenterDatasetService._verified_managed_files(source, [declaration])


@pytest.mark.asyncio
async def test_curated_seed_rejects_symlinked_target_parent_before_copy(monkeypatch, tmp_path):
    FakeDatasetDocument.records = {}
    seed_root = tmp_path / "seeds"
    source = seed_root / "seed_symlink"
    (source / "nested").mkdir(parents=True)
    file_bytes = b"value\n1\n"
    (source / "nested" / "observations.csv").write_bytes(file_bytes)
    payload = _seed_payload(file_bytes)
    payload["dataset_id"] = "seed_symlink"
    payload["files"][0]["path"] = "nested/observations.csv"
    (source / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    storage_root = tmp_path / "managed"
    managed_dataset = storage_root / "seed_symlink"
    managed_dataset.mkdir(parents=True)
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (managed_dataset / "nested").symlink_to(escaped, target_is_directory=True)
    monkeypatch.setattr(service_module, "DataCenterDatasetDocument", FakeDatasetDocument)
    service = _service(storage_root=storage_root, seed_root=seed_root)

    with pytest.raises(BadRequestError, match="symbolic links"):
        await service.ensure_seed_data()
    assert not (escaped / "observations.csv").exists()
    assert FakeDatasetDocument.records == {}


def test_repository_bundled_manifests_match_strict_schema_files_and_hashes():
    manifest_paths = sorted(Path(service_module.DATASET_SEED_ROOT).glob("*/manifest.json"))
    assert len(manifest_paths) == 18
    observed_domains = set()
    for manifest_path in manifest_paths:
        seed = CuratedDatasetSeed.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        assert seed.dataset_id == manifest_path.parent.name
        assert seed.metadata.get("curated") is True
        get_domain_preset(seed.domain)
        observed_domains.add(seed.domain)
        verified = DataCenterDatasetService._verified_managed_files(
            manifest_path.parent, seed.files,
        )
        assert [item.path for item in verified] == [item.path for item in seed.files]
        assert all(item.size is not None and item.sha256 is not None for item in seed.files)
        public = dataset_response(DataCenterDataset(
            **seed.model_dump(exclude={"files"}),
            files=verified,
        ))
        assert {
            "curated", "publisher", "source_url", "license", "license_url",
            "sample_scope", "provenance",
        } <= set(public.metadata)
    assert observed_domains == {preset.id for preset in list_domain_presets()}


@pytest.mark.asyncio
async def test_management_routes_forward_role_and_return_sanitized_models(monkeypatch):
    dataset = DataCenterDataset(
        dataset_id="dsr_route", data_center_id="owner-registration",
        data_center_name="Registered datasets", name="Route", domain="tabular",
        is_submission=True, created_by="owner-a",
    )
    service = AsyncMock()
    service.create_registration.return_value = dataset
    service.update_registration.return_value = dataset
    service.archive_dataset.return_value = dataset.model_copy(update={"enabled": False})
    service.list_managed_datasets.return_value = ([dataset], 1)
    monkeypatch.setattr(dataset_routes, "DataCenterDatasetService", lambda: service)

    registration = DatasetRegistrationRequest(
        name="Route", description="", domain="tabular",
        storage_directory="/srv/datasets/route",
    )
    with pytest.raises(ForbiddenError):
        await dataset_routes.create_dataset_registration(registration, current_user=_user())
    await dataset_routes.create_dataset_registration(
        registration, current_user=_user(UserRole.ADMIN),
    )
    service.create_registration.assert_awaited_once_with(
        name="Route", description="", domain="tabular",
        storage_directory="/srv/datasets/route", created_by="owner-a",
    )

    await dataset_routes.list_managed_datasets(
        query=None, include_archived=False, limit=100, offset=0,
        current_user=_user(UserRole.ADMIN),
    )
    service.list_managed_datasets.assert_awaited_once_with(
        actor_id="owner-a", is_admin=True, query=None,
        include_archived=False, limit=100, offset=0,
    )

    await dataset_routes.update_dataset_registration(
        dataset.dataset_id, DatasetMetadataUpdateRequest(domain="spectroscopy"),
        current_user=_user(),
    )
    service.update_registration.assert_awaited_once_with(
        dataset.dataset_id, actor_id="owner-a", is_admin=False, domain="spectroscopy",
    )
    response = await dataset_routes.archive_dataset_registration(
        dataset.dataset_id, current_user=_user(),
    )
    assert response.data.enabled is False
