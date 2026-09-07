"""Keep catalog initialization cheap without caching visibility or seed state."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.application.services.data_center_dataset_service as module
from test_dataset_management import FakeDatasetDocument, _seed_payload, _service


def _manifest(root: Path, dataset_id: str) -> None:
    import json

    directory = root / dataset_id
    directory.mkdir(parents=True)
    payload = _seed_payload(b"year,value\n2024,1\n")
    payload["dataset_id"] = dataset_id
    (directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 18, 100])
async def test_existing_seeds_use_one_identity_only_query(monkeypatch, tmp_path, count):
    seeds = tmp_path / "seeds"
    ids = [f"seed_{index:03}" for index in range(count)]
    for dataset_id in ids:
        _manifest(seeds, dataset_id)
    records = {dataset_id: SimpleNamespace(dataset_id=dataset_id, enabled=False) for dataset_id in ids}
    monkeypatch.setattr(FakeDatasetDocument, "records", records)
    find = Mock(wraps=FakeDatasetDocument.find)
    find_one = AsyncMock(side_effect=AssertionError("N+1 lookup on an existing seed"))
    monkeypatch.setattr(FakeDatasetDocument, "find", find)
    monkeypatch.setattr(FakeDatasetDocument, "find_one", find_one)
    monkeypatch.setattr(module, "DataCenterDatasetDocument", FakeDatasetDocument)
    service = _service(storage_root=tmp_path / "managed", seed_root=seeds)
    service._verified_managed_files = Mock(side_effect=AssertionError("Recopying an existing seed"))

    await service.ensure_seed_data()

    find.assert_called_once_with({"dataset_id": {"$in": ids}})
    find_one.assert_not_awaited()
    assert set(module._SeedIdentity.model_fields) == {"dataset_id"}
    assert all(record.enabled is False for record in records.values())
    assert not service._storage_root.exists()


@pytest.mark.asyncio
async def test_empty_seed_directory_does_not_query_database(monkeypatch, tmp_path):
    documents = SimpleNamespace(find=Mock(side_effect=AssertionError("Unexpected database query")))
    monkeypatch.setattr(module, "DataCenterDatasetDocument", documents)
    service = _service(seed_root=tmp_path, storage_root=tmp_path / "managed")
    await service.ensure_seed_data()
    documents.find.assert_not_called()


@pytest.mark.asyncio
async def test_presence_is_rechecked_and_removed_seed_is_restored(monkeypatch, tmp_path):
    seeds = tmp_path / "seeds"
    _manifest(seeds, "seed_climate")
    payload = b"year,value\n2024,1\n"
    (seeds / "seed_climate" / "observations.csv").write_bytes(payload)
    storage = tmp_path / "managed"
    monkeypatch.setattr(FakeDatasetDocument, "records", {})
    monkeypatch.setattr(module, "DataCenterDatasetDocument", FakeDatasetDocument)
    service = _service(seed_root=seeds, storage_root=storage)

    await service.ensure_seed_data()
    first = FakeDatasetDocument.records["seed_climate"]
    first.enabled = False
    await service.ensure_seed_data()
    assert FakeDatasetDocument.records["seed_climate"] is first
    assert first.enabled is False

    # Simulate an operator removing a record; no permanent initialization flag
    # may suppress the next idempotent recovery check.
    FakeDatasetDocument.records.pop("seed_climate")
    await service.ensure_seed_data()
    replacement = FakeDatasetDocument.records["seed_climate"]
    assert replacement is not first
    assert replacement.enabled is True
    assert (storage / "seed_climate" / "observations.csv").read_bytes() == payload
