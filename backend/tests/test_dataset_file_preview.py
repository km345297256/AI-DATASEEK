from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.application.errors.exceptions import NotFoundError
from app.application.services import dataset_file_preview as module
from app.application.services.dataset_file_preview import DatasetFilePreviewRequest, DatasetFilePreviewService
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import FileService
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.domain.models.dataset import DataCenterDataset, DatasetFile, DatasetLocation, DatasetStorageType
from app.domain.models.visualization import VisualizationSnapshot
from app.infrastructure.external.file.datasetfile import DatasetPreviewFileStorage
from app.infrastructure.external.sandbox.node_health import LOCAL_DEFAULT_NODE_ID
from app.infrastructure.repositories.mongo_dataset_preview_repository import MongoDatasetPreviewRepository
from app.interfaces.api import dataset_routes
from app.interfaces.schemas.dataset import dataset_response


class Preferences:
    def __init__(self):
        self.states = {}

    async def get_states(self, owner):
        return self.states.get(owner, {})

    async def set_state(self, owner, plugin, enabled):
        self.states.setdefault(owner, {})[plugin] = enabled


class Runtime:
    def __init__(self):
        root = Path(__file__).resolve().parents[2] / "plugin-host" / "visualizations"
        self.snapshot = VisualizationSnapshot(engine="cordis", revision="a"*64,
            plugins=[json.loads(path.read_text()) for path in sorted(root.glob("*.json"))])

    async def visualization_snapshot(self):
        return self.snapshot


class References:
    def __init__(self):
        self.rows = {}

    async def put(self, file_id, row):
        self.rows[file_id] = {"_id": file_id, **row}
        return self.rows[file_id]

    async def get(self, file_id):
        return self.rows.get(file_id)


class Datasets:
    def __init__(self, dataset):
        self.dataset = dataset
        self.reads = []
        self.archived = False

    async def get_dataset(self, dataset_id, user_id=None):
        self.reads.append((dataset_id, user_id))
        if dataset_id != self.dataset.dataset_id or self.archived or user_id != "owner":
            raise NotFoundError("Dataset not found")
        return self.dataset.model_copy(deep=True)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(module, "_READ_SLOTS", asyncio.Semaphore(2))
    location = DatasetLocation(node_id=LOCAL_DEFAULT_NODE_ID, storage_type=DatasetStorageType.MANAGED_UPLOAD,
                               source_path="test-dataset", read_only=True, verified=True)
    dataset = DataCenterDataset(dataset_id="test-dataset", data_center_id="test", data_center_name="test", name="test",
                               files=[DatasetFile(path="folder/data.nc", size=7)], locations=[location])
    datasets = Datasets(dataset)
    repository, preferences, runtime = References(), Preferences(), Runtime()
    catalog = VisualizationCatalogService(runtime, preferences)
    state = {"reads": [], "size": 7, "mtime_ns": 1, "data": b"example"}
    def reader(location, relative, *, offset, length, max_bytes):
        state["reads"].append((location, relative, offset, length, max_bytes))
        if length is None and state["size"] > max_bytes:
            raise ValueError("Budget exceeded")
        data = state["data"][offset:] if length is None else state["data"][offset:offset+length]
        return data, {"size": state["size"], "mtime_ns": state["mtime_ns"], "ctime_ns": 1, "inode": 2, "device": 3}
    service = DatasetFilePreviewService(datasets=datasets, repository=repository, catalog=catalog, reader=reader,
        settings=SimpleNamespace(jwt_secret_key="test-opaque-key", dataset_storage_root="/data/datasets", dataset_host_path_allowlist="/allowed"))
    return SimpleNamespace(service=service, datasets=datasets, repository=repository, catalog=catalog,
                           runtime=runtime, preferences=preferences, state=state)


async def prepare(env, path="folder/data.nc", plugin="netcdf-map", owner="owner"):
    return await env.service.prepare("test-dataset", owner, DatasetFilePreviewRequest(path=path, plugin_id=plugin))


@pytest.mark.parametrize("path", ["", "/secret", "../a.nc", "a/../b.nc", "a//b.nc", "a/./b.nc", "a\\b.nc", "a/", ".", "C:/file.nc", "x\x00.nc", "x\n.nc", "x\x7f.nc"])
def test_request_paths_are_strict_and_never_normalized(path):
    with pytest.raises(ValidationError):
        DatasetFilePreviewRequest(path=path, plugin_id="netcdf-map")


@pytest.mark.asyncio
async def test_prepare_is_opaque_idempotent_stat_only_and_no_source_path(env):
    first = await prepare(env)
    second = await prepare(env)
    assert first == second and first.related_files == [first.file]
    assert first.file.file_id.startswith("dataset-preview:") and len(first.file.file_id) == 80
    assert len(env.repository.rows) == 1
    assert first.file.filename == "data.nc" and first.file.size == 7
    assert first.file.file_path is None and first.file.file_url is None
    assert first.file.metadata["logical_path"] == "folder/data.nc"
    assert all(read[3] == 0 for read in env.state["reads"])
    assert all(owner == "owner" for _, owner in env.datasets.reads)
    assert "/data/datasets" not in first.model_dump_json()
    assert all(set(row) == {"_id", "owner_id", "dataset_id", "path", "anchor"} for row in env.repository.rows.values())


@pytest.mark.asyncio
async def test_prepare_requires_registered_file_owner_and_enabled_matching_plugin(env):
    with pytest.raises(NotFoundError):
        await prepare(env, owner="foreign")
    with pytest.raises(FileNotFoundError):
        await prepare(env, path="folder/undeclared.nc")
    with pytest.raises(ValueError):
        await prepare(env, plugin="fits-image")
    await env.catalog.set_state("owner", "netcdf-map", False)
    with pytest.raises(VisualizationDisabledError):
        await prepare(env)
    assert env.repository.rows == {} and env.state["reads"] == []


@pytest.mark.asyncio
async def test_reference_reads_keep_owner_scope_and_allow_other_enabled_view(env):
    result = await prepare(env)
    file_id = result.file.file_id
    await env.catalog.set_state("owner", "netcdf-map", False)
    data, info = await env.service.download_file_range(file_id, "owner", offset=1, length=3)
    assert data == b"xam" and info.file_id == file_id
    # A signed, opaque reference resolves to its original owner, never a
    # caller-provided dataset owner. Every source/catalog check still runs.
    assert (await env.service.get_file_info(file_id, None)).user_id == "owner"
    assert await env.service.get_file_info(file_id, "foreign") is None
    await env.catalog.set_state("owner", "netcdf-series", False)
    # v2 H5Web is another independently enabled NetCDF4/HDF5 view. A reusable
    # file reference must stay available until every matching view is stopped.
    assert await env.service.get_file_info(file_id, "owner") is not None
    await env.catalog.set_state("owner", "viz-h5web", False)
    assert await env.service.get_file_info(file_id, "owner") is None
    with pytest.raises(VisualizationDisabledError):
        await env.service.download_file_range(file_id, "owner", offset=0, length=1)


@pytest.mark.asyncio
async def test_archive_or_removed_inventory_invalidates_existing_refs(env):
    file_id = (await prepare(env)).file.file_id
    env.datasets.archived = True
    assert await env.service.get_file_info(file_id, "owner") is None
    env.datasets.archived = False
    env.datasets.dataset.files.clear()
    assert await env.service.get_file_info(file_id, "owner") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("read_only", False), ("verified", False), ("node_id", "remote-node"), ("source_path", "other-dataset")])
async def test_unapproved_locations_never_reach_reader(env, field, value):
    setattr(env.datasets.dataset.locations[0], field, value)
    with pytest.raises(FileNotFoundError):
        await prepare(env)
    assert env.state["reads"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mount_name,inventory_prefix", [("data", "data/"), ("data", ""), ("", "private-source/"), ("", "")])
async def test_host_inventory_prefix_resolves_from_actual_public_response(env, mount_name, inventory_prefix):
    location = env.datasets.dataset.locations[0]
    location.storage_type = DatasetStorageType.HOST_PATH
    location.source_path = "/allowed/private-source"
    location.mount_name = mount_name
    path = f"sources/{location.location_id}/{inventory_prefix}folder/data.nc"
    env.datasets.dataset.files[0].path = path
    public = dataset_response(env.datasets.dataset)
    assert public.locations == [] and public.files[0].path == "folder/data.nc"
    response = await prepare(env, path=public.files[0].path)
    assert env.state["reads"][0][1] == "folder/data.nc"
    assert response.file.metadata["logical_path"] == public.files[0].path
    assert location.location_id not in response.model_dump_json()
    assert "private-source" not in response.model_dump_json()
    assert "/allowed" not in response.model_dump_json()
    assert "/allowed" not in json.dumps(env.repository.rows)
    assert list(env.repository.rows.values())[0]["path"] == path
    # An untrusted caller cannot submit the non-public implementation prefix.
    with pytest.raises(FileNotFoundError):
        await prepare(env, path=path)


@pytest.mark.asyncio
async def test_colliding_public_paths_across_host_sources_are_rejected(env):
    first = env.datasets.dataset.locations[0]
    first.storage_type = DatasetStorageType.HOST_PATH
    first.source_path, first.mount_name = "/allowed/one", "data"
    second = first.model_copy(update={"location_id": "dsl_second", "source_path": "/allowed/two"})
    env.datasets.dataset.locations = [first, second]
    env.datasets.dataset.files = [DatasetFile(path=f"sources/{location.location_id}/data/folder/data.nc") for location in [first, second]]
    public = dataset_response(env.datasets.dataset)
    assert [item.path for item in public.files] == ["folder/data.nc", "folder/data.nc"]
    with pytest.raises(FileNotFoundError):
        await prepare(env, path=public.files[0].path)
    assert not env.repository.rows and not env.state["reads"]


@pytest.mark.asyncio
async def test_new_public_path_collision_invalidates_existing_reference(env):
    first = env.datasets.dataset.locations[0]
    first.storage_type = DatasetStorageType.HOST_PATH
    first.source_path, first.mount_name = "/allowed/one", "data"
    env.datasets.dataset.files = [DatasetFile(path=f"sources/{first.location_id}/data/folder/data.nc")]
    file_id = (await prepare(env, path=dataset_response(env.datasets.dataset).files[0].path)).file.file_id
    second = first.model_copy(update={"location_id": "dsl_second", "source_path": "/allowed/two"})
    env.datasets.dataset.locations.append(second)
    env.datasets.dataset.files.append(DatasetFile(path=f"sources/{second.location_id}/data/folder/data.nc"))
    assert await env.service.get_file_info(file_id, "owner") is None


@pytest.mark.asyncio
async def test_host_shapefile_roundtrip_keeps_public_sidecar_grouping(env):
    location = env.datasets.dataset.locations[0]
    location.storage_type = DatasetStorageType.HOST_PATH
    location.source_path, location.mount_name = "/allowed/private-source", "data"
    env.datasets.dataset.files = [DatasetFile(path=f"sources/{location.location_id}/data/folder/roads.{suffix}") for suffix in ["shp", "dbf", "shx"]]
    public = dataset_response(env.datasets.dataset)
    response = await prepare(env, path=public.files[0].path, plugin="shapefile")
    assert [info.metadata["logical_path"] for info in response.related_files] == [item.path for item in public.files]
    assert location.location_id not in response.model_dump_json() and "private-source" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_ambiguous_location_and_duplicate_inventory_rejected(env):
    env.datasets.dataset.locations.append(env.datasets.dataset.locations[0].model_copy())
    with pytest.raises(FileNotFoundError):
        await prepare(env)
    env.datasets.dataset.locations.pop()
    env.datasets.dataset.files.append(env.datasets.dataset.files[0].model_copy())
    with pytest.raises(FileNotFoundError):
        await prepare(env)


@pytest.mark.asyncio
async def test_same_size_revision_change_invalidates_text_pages(env):
    env.datasets.dataset.files[0].path = "folder/data.csv"
    file_id = (await prepare(env, path="folder/data.csv", plugin="csv")).file.file_id
    file_service = FileService(DatasetPreviewFileStorage(SimpleNamespace(), env.service))
    first = await file_service.preview_file(file_id, "owner")
    env.state["mtime_ns"] += 1
    with pytest.raises(PreviewVersionChanged):
        await file_service.preview_file(file_id, "owner", version=first.version)


@pytest.mark.asyncio
async def test_text_and_fastq_large_source_stat_and_bounded_ranges(env):
    env.datasets.dataset.files[0].path = "folder/data.fastq"
    env.state["size"] = 100*1024*1024
    file_id = (await prepare(env, path="folder/data.fastq", plugin="fastq-quality")).file.file_id
    data, info = await env.service.download_file_range(file_id, "owner", offset=0, length=7)
    assert data == b"example" and info.size == 100*1024*1024
    with pytest.raises(ValueError):
        await env.service.download_file(file_id, "owner")
    with pytest.raises(ValueError):
        await env.service.download_file_range(file_id, "owner", offset=0, length=64*1024*1024+1)


@pytest.mark.asyncio
async def test_shapefile_sidecars_same_logical_stem_only(env):
    env.datasets.dataset.files = [DatasetFile(path=path) for path in [
        "one/roads.shp", "one/roads.dbf", "one/roads.prj", "two/roads.dbf", "one/roads.secret"]]
    result = await prepare(env, path="one/roads.shp", plugin="shapefile")
    assert [info.filename for info in result.related_files] == ["roads.shp", "roads.dbf", "roads.prj"]
    assert all(info.metadata["logical_path"].startswith("one/") for info in result.related_files)
    sidecar = result.related_files[1].file_id
    assert (await env.service.get_file_info(sidecar, "owner")).filename == "roads.dbf"
    await env.catalog.set_state("owner", "shapefile", False)
    assert await env.service.get_file_info(sidecar, "owner") is None


@pytest.mark.asyncio
async def test_native_read_archive_and_toggle_and_revision_races_are_fenced(env):
    file_id = (await prepare(env)).file.file_id
    original = env.service.reader
    def archive(*args, **kwargs):
        env.datasets.archived = True
        return original(*args, **kwargs)
    env.service.reader = archive
    with pytest.raises(FileNotFoundError):
        await env.service.download_file_range(file_id, "owner", offset=0, length=1)
    env.datasets.archived = False
    def revision(*args, **kwargs):
        env.runtime.snapshot = env.runtime.snapshot.model_copy(update={"revision": "b"*64})
        return original(*args, **kwargs)
    env.service.reader = revision
    with pytest.raises(PreviewVersionChanged):
        await env.service.download_file_range(file_id, "owner", offset=0, length=1)


@pytest.mark.asyncio
async def test_selected_plugin_disabled_during_prepare_never_persists_reference(env):
    original = env.service.reader
    def stop_selected(*args, **kwargs):
        env.preferences.states["owner"] = {"netcdf-map": False}
        return original(*args, **kwargs)
    env.service.reader = stop_selected
    with pytest.raises(VisualizationDisabledError):
        await prepare(env)
    # Series remains enabled, but it cannot substitute for the user's selected
    # admission plugin while this preparation is in flight.
    assert env.repository.rows == {}


@pytest.mark.asyncio
async def test_cancelled_native_read_keeps_slot_until_thread_stops(env, monkeypatch):
    file_id = (await prepare(env)).file.file_id
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(module, "_READ_SLOTS", slots)
    started, release = threading.Event(), threading.Event()
    original = env.service.reader
    def blocking(*args, **kwargs):
        started.set()
        release.wait(2)
        return original(*args, **kwargs)
    env.service.reader = blocking
    task = asyncio.create_task(env.service.download_file_range(file_id, "owner", offset=0, length=1))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.locked()
    release.set()
    await asyncio.wait_for(slots.acquire(), timeout=2)
    slots.release()


@pytest.mark.asyncio
async def test_storage_overlay_preserves_uploads_and_rejects_dataset_deletion(env):
    base = SimpleNamespace(get_file_info=AsyncMock(return_value="ordinary"), download_file=AsyncMock(),
                           download_file_range=AsyncMock(), delete_file=AsyncMock(return_value=True), upload_file=AsyncMock())
    storage = DatasetPreviewFileStorage(base, env.service)
    assert await storage.get_file_info("minio:normal", "owner") == "ordinary"
    assert storage.upload_file is base.upload_file
    assert await storage.delete_file("minio:normal", "owner") is True
    assert await storage.delete_file("dataset-preview:" + "a"*64, "owner") is False
    assert base.delete_file.await_count == 1
    with pytest.raises(NotImplementedError):
        await storage.create_presigned_url("dataset-preview:" + "a"*64, "owner")


@pytest.mark.asyncio
async def test_expired_missing_or_malformed_reference_never_reads(env):
    assert await env.service.get_file_info("dataset-preview:" + "a"*64, "owner") is None
    assert await env.service.get_file_info("dataset-preview:../../secret", "owner") is None
    assert env.state["reads"] == []


@pytest.mark.asyncio
async def test_repository_upsert_is_deduplicated_expiring_and_no_file_collection():
    collection = SimpleNamespace(create_index=AsyncMock(), update_one=AsyncMock(), find_one=AsyncMock(return_value=None))
    repo = MongoDatasetPreviewRepository(collection)
    row = {"owner_id": "owner", "dataset_id": "dataset", "path": "a.csv", "anchor": "a.csv"}
    await repo.put("dataset-preview:opaque", row)
    await repo.put("dataset-preview:opaque", row)
    assert collection.create_index.await_count == 1
    collection.create_index.assert_awaited_once_with("expires_at", expireAfterSeconds=0)
    query, update = collection.update_one.call_args.args
    assert query == {"_id": "dataset-preview:opaque"}
    assert set(update["$set"]) == {*row, "expires_at"}
    assert await repo.get("dataset-preview:opaque") is None
    assert "$gt" in collection.find_one.call_args.args[0]["expires_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,status", [(FileNotFoundError("secret"), 404), (VisualizationDisabledError("secret"), 403), (ValueError("/private/server/path"), 422)])
async def test_prepare_route_returns_safe_errors(monkeypatch, exception, status):
    service = SimpleNamespace(prepare=AsyncMock(side_effect=exception))
    monkeypatch.setattr(dataset_routes, "get_file_storage", lambda: SimpleNamespace(previews=service))
    with pytest.raises(HTTPException) as caught:
        await dataset_routes.prepare_dataset_file_preview("ds", DatasetFilePreviewRequest(path="a.nc", plugin_id="netcdf-map"), SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), SimpleNamespace(id="owner"))
    assert caught.value.status_code == status and "secret" not in caught.value.detail and "/private" not in caught.value.detail


@pytest.mark.asyncio
async def test_route_uses_public_metadata_contract_without_eager_download_urls(env, monkeypatch):
    monkeypatch.setattr(dataset_routes, "get_file_storage", lambda: SimpleNamespace(previews=env.service))
    response = await dataset_routes.prepare_dataset_file_preview("test-dataset", DatasetFilePreviewRequest(path="folder/data.nc", plugin_id="netcdf-map"),
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), SimpleNamespace(id="owner"))
    payload = response.model_dump()["data"]
    assert payload["file"]["metadata"]["logical_path"] == "folder/data.nc"
    assert "file_path" not in payload["file"] and "user_id" not in payload["file"]
    assert payload["file"]["file_url"] is None
    assert payload["related_files"] == [payload["file"]]


@pytest.mark.asyncio
async def test_disconnected_prepare_route_cancels_without_materializing_reference(monkeypatch):
    cancelled = asyncio.Event()
    async def waiting(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(dataset_routes, "get_file_storage", lambda: SimpleNamespace(previews=SimpleNamespace(prepare=waiting)))
    with pytest.raises(asyncio.CancelledError):
        await dataset_routes.prepare_dataset_file_preview("dataset", DatasetFilePreviewRequest(path="a.nc", plugin_id="netcdf-map"),
            SimpleNamespace(is_disconnected=AsyncMock(return_value=True)), SimpleNamespace(id="owner"))
    assert cancelled.is_set()
