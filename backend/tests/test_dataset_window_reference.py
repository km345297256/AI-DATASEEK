"""Large synthetic EDF/BDF sources through real opaque dataset references.

Only stat dictionaries and tiny selected byte ranges exist: no large files,
real datasets, Docker workers, model calls, or persistent records are needed.
The injected worker tests the storage boundary, not the EDF binary decoder.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.application.errors.exceptions import NotFoundError
from app.application.services import window_visualization as window
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import FileService
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.dataset import DatasetFile, DatasetStorageType
from app.infrastructure.external.file.datasetfile import DatasetPreviewFileStorage
from app.interfaces.schemas.dataset import dataset_response
from test_dataset_file_preview import env, prepare  # noqa: F401: shared fixture
from test_window_visualization import payload


SOURCE_BYTES = 2_500_000_000
PLUGIN = "viz-edf-signals"
SELECTED_RANGES = [(2_000_000_000, 32), (SOURCE_BYTES - 16, 16)]


def synthetic_bytes(offset, length):
    # All test ranges are tiny, regardless of the declared multi-GB source.
    assert 0 <= length <= 1024
    return bytes((offset + index) % 256 for index in range(length))


def configure(env, extension, *, host=True):
    # These legacy cases isolate the physiological EDF/BDF capability. The
    # independently enabled ESRF image reader also matches .edf; coexistence is
    # covered separately below without changing the EDF worker's own limits.
    env.preferences.states.setdefault("owner", {})["viz-instrument-images"] = False
    path = f"folder/record.{extension}"
    location = env.datasets.dataset.locations[0]
    if host:
        location.storage_type = DatasetStorageType.HOST_PATH
        location.source_path = "/allowed/private-source"
        location.mount_name = "data"
        inventory_path = f"sources/{location.location_id}/data/{path}"
    else:
        inventory_path = path
    env.datasets.dataset.files = [DatasetFile(path=inventory_path, size=SOURCE_BYTES)]
    env.state.update(size=SOURCE_BYTES, on_data=None)

    def reader(source, relative, *, offset, length, max_bytes):
        env.state["reads"].append((source, relative, offset, length, max_bytes))
        if length is None:
            raise ValueError("Whole-source reads exceed the preview budget")
        assert source.read_only and source.verified
        assert relative == path and 0 <= offset <= SOURCE_BYTES
        assert offset + length <= SOURCE_BYTES and length <= max_bytes
        data = synthetic_bytes(offset, length)
        if length and env.state["on_data"]:
            env.state["on_data"]()
        return data, {
            "size": SOURCE_BYTES, "mtime_ns": env.state["mtime_ns"],
            "ctime_ns": 1, "inode": 2, "device": 3,
        }

    env.service.reader = reader
    public = dataset_response(env.datasets.dataset)
    assert public.locations == [] and public.files[0].path == path
    return public.files[0].path


def data_reads(env):
    return [(read[2], read[3]) for read in env.state["reads"] if read[3] != 0]


def assert_private_location_hidden(env, info):
    serialized = info.model_dump_json()
    assert info.filename.startswith("record.") and info.size == SOURCE_BYTES
    assert info.file_id.startswith("dataset-preview:") and len(info.file_id) == 80
    assert info.file_path is None and info.file_url is None
    assert set(info.metadata) == {"source", "logical_path", "dataset_file_version"}
    assert info.metadata["source"] == "dataset_preview"
    for secret in ("/allowed", "private-source", "/data/datasets", "sources/",
                   env.datasets.dataset.locations[0].location_id):
        assert secret not in serialized


def selected_worker(_image, **kwargs):
    assert kwargs["size"] == SOURCE_BYTES and kwargs["reader"] == "edf"
    assert kwargs["kind"] == "series" and kwargs["format"] in {"edf", "bdf"}
    assert not {"path", "file_id", "user_id", "filename", "data", "credentials"} & kwargs.keys()
    assert kwargs["limits"] == {
        "max_read_bytes": 1024**2, "max_total_bytes": 8 * 1024**2, "max_reads": 128,
    }
    for offset, length in SELECTED_RANGES:
        assert kwargs["read_range"](offset, length) == synthetic_bytes(offset, length)
    result = payload(size=SOURCE_BYTES, read_bytes=48, read_requests=2)
    result["metadata"].update(format=kwargs["format"], variant=kwargs["format"].upper() + "+C")
    return {"ok": True, "data": result}


async def invoke(env, info, *, worker=selected_worker, owner="owner", version=None):
    # SimpleNamespace has no ordinary-storage methods; any wrong reference
    # routing or fallback to full-source download therefore fails the test.
    storage = DatasetPreviewFileStorage(SimpleNamespace(), env.service)
    return await window.window_visualization(
        FileService(storage), env.catalog, "sandbox:test", info.file_id, owner,
        SimpleNamespace(plugin_id=PLUGIN, kind="series", options={
            "channels": [0], "start_seconds": 0, "duration_seconds": 1,
        }, version=preview_version(info) if version is None else version),
        worker=worker,
    )


@pytest.fixture(autouse=True)
def independent_window_slots(monkeypatch):
    monkeypatch.setattr(window, "_SLOTS", asyncio.Semaphore(2))


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
@pytest.mark.parametrize("host", [True, False], ids=["host-readonly", "managed-readonly"])
async def test_prepare_large_reference_is_stat_only_opaque_and_idempotent(env, extension, host):
    path = configure(env, extension, host=host)
    first = await prepare(env, path=path, plugin=PLUGIN)
    second = await prepare(env, path=path, plugin=PLUGIN)
    assert first == second and first.related_files == [first.file]
    assert_private_location_hidden(env, first.file)
    assert len(env.repository.rows) == 1 and len(env.state["reads"]) == 2
    assert [(read[2], read[3]) for read in env.state["reads"]] == [(0, 0), (0, 0)]
    assert all(read[4] == 8 * 1024**2 for read in env.state["reads"])
    assert all(owner == "owner" for _, owner in env.datasets.reads)
    assert "/allowed" not in json.dumps(env.repository.rows)
    assert all(set(row) == {"_id", "owner_id", "dataset_id", "path", "anchor"}
               for row in env.repository.rows.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
async def test_download_range_does_not_materialize_large_reference(env, extension):
    path = configure(env, extension)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    storage = DatasetPreviewFileStorage(SimpleNamespace(), env.service)
    offset, length = SELECTED_RANGES[0]
    data, ranged = await storage.download_file_range(info.file_id, "owner", offset=offset, length=length)
    assert data == synthetic_bytes(offset, length)
    assert data_reads(env) == [(offset, length)]
    assert_private_location_hidden(env, ranged)
    assert preview_version(ranged) == preview_version(info)


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
async def test_reference_full_download_and_over_budget_range_are_rejected(env, extension):
    path = configure(env, extension)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    with pytest.raises(ValueError, match="resource budget"):
        await env.service.download_file_range(info.file_id, "owner", offset=0, length=8 * 1024**2 + 1)
    assert data_reads(env) == []
    with pytest.raises(ValueError, match="Whole-source"):
        await env.service.download_file(info.file_id, "owner")
    assert data_reads(env) == [(0, None)]  # The synthetic reader rejects before allocation.


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
async def test_reference_and_window_reject_foreign_owner_without_reads(env, extension):
    path = configure(env, extension)
    with pytest.raises(NotFoundError):
        await prepare(env, path=path, plugin=PLUGIN, owner="foreign")
    assert not env.repository.rows and not env.state["reads"]
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    env.state["reads"].clear()
    with pytest.raises(FileNotFoundError):
        await env.service.download_file_range(info.file_id, "foreign", offset=2_000_000_000, length=32)
    with pytest.raises(FileNotFoundError):
        await invoke(env, info, owner="foreign")
    assert await env.service.get_file_info(info.file_id, "foreign") is None
    assert not env.state["reads"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
async def test_stopped_edf_plugin_revokes_prepare_reference_and_window(env, extension):
    path = configure(env, extension)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    await env.catalog.set_state("owner", PLUGIN, False)
    env.state["reads"].clear()
    with pytest.raises(VisualizationDisabledError):
        await prepare(env, path=path, plugin=PLUGIN)
    with pytest.raises(VisualizationDisabledError):
        await env.service.download_file_range(info.file_id, "owner", offset=0, length=1)
    with pytest.raises(VisualizationDisabledError):
        await invoke(env, info)
    assert await env.service.get_file_info(info.file_id, "owner") is None
    assert not env.state["reads"]


@pytest.mark.asyncio
async def test_two_edf_views_share_reference_but_not_signal_worker_budgets(env):
    path = configure(env, "edf")
    await env.catalog.set_state("owner", "viz-instrument-images", True)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    assert all(read[4] == 32 * 1024**2 for read in env.state["reads"])
    # selected_worker asserts the actual signal invocation still has the exact
    # 1 MiB/read, 8 MiB total and 128-request capability, despite the reusable
    # storage reference being shared with the 32 MiB image reader.
    result = await invoke(env, info)
    assert result["metadata"]["read_bytes"] == 48
    assert data_reads(env) == SELECTED_RANGES
    assert all(read[4] == 32 * 1024**2 for read in env.state["reads"])
    env.state["reads"].clear()
    def oversized_signal_read(_image, **kwargs):
        assert kwargs["limits"]["max_total_bytes"] == 8 * 1024**2
        kwargs["read_range"](0, 8 * 1024**2 + 1)
        raise AssertionError("Oversized signal read must have been rejected")
    with pytest.raises(window.ScientificPreviewRejected):
        await invoke(env, info, worker=oversized_signal_read)
    assert data_reads(env) == []


@pytest.mark.asyncio
async def test_stopping_signal_preserves_image_reference_until_all_edf_views_stop(env):
    path = configure(env, "edf")
    await env.catalog.set_state("owner", "viz-instrument-images", True)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    await env.catalog.set_state("owner", PLUGIN, False)
    assert await env.service.get_file_info(info.file_id, "owner") is not None
    data, reference = await env.service.download_file_range(info.file_id, "owner", offset=2, length=4)
    assert data == synthetic_bytes(2, 4) and reference.file_id == info.file_id
    assert (await prepare(env, path=path, plugin="viz-instrument-images")).file.file_id == info.file_id
    with pytest.raises(VisualizationDisabledError):
        await invoke(env, info)
    await env.catalog.set_state("owner", "viz-instrument-images", False)
    assert await env.service.get_file_info(info.file_id, "owner") is None
    with pytest.raises(VisualizationDisabledError):
        await env.service.download_file_range(info.file_id, "owner", offset=0, length=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
async def test_window_broker_uses_only_selected_ranges_and_keeps_source_private(env, extension):
    path = configure(env, extension)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    result = await invoke(env, info)
    assert data_reads(env) == SELECTED_RANGES
    assert all(read[3] is not None for read in env.state["reads"])
    assert sum(read[3] for read in env.state["reads"]) == 48
    assert all((read[2], read[3]) == (0, 0) for read in env.state["reads"] if not read[3])
    assert result["metadata"]["source_bytes"] == SOURCE_BYTES
    assert result["metadata"]["read_bytes"] == 48 and result["metadata"]["read_requests"] == 2
    assert result["metadata"]["format"] == extension
    assert result["version"] == preview_version(info) and result["plugin_id"] == PLUGIN
    assert result["series"][0]["y"][1] == 1.0000000000000002
    assert "/allowed" not in json.dumps(result) and info.file_id not in json.dumps(result)
    assert window._SLOTS._value == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
async def test_pinned_file_version_rejects_same_size_change_before_window_read(env, extension):
    path = configure(env, extension)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file
    env.state["mtime_ns"] += 1
    worker_calls = []

    def forbidden(*args, **kwargs):
        worker_calls.append(True)
        raise AssertionError("A stale version must never reach the worker")

    with pytest.raises(PreviewVersionChanged):
        await invoke(env, info, worker=forbidden)
    assert not worker_calls and not data_reads(env)
    fresh = await env.service.get_file_info(info.file_id, "owner")
    assert fresh.file_id == info.file_id and preview_version(fresh) != preview_version(info)
    assert_private_location_hidden(env, fresh)


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["edf", "bdf"])
@pytest.mark.parametrize("change", ["version", "disabled", "catalog", "archived"])
async def test_inflight_dataset_changes_revoke_read_and_result(env, extension, change):
    path = configure(env, extension)
    info = (await prepare(env, path=path, plugin=PLUGIN)).file

    def revoke():
        if change == "version":
            env.state["mtime_ns"] += 1
        elif change == "disabled":
            env.preferences.states.setdefault("owner", {})[PLUGIN] = False
        elif change == "catalog":
            env.runtime.snapshot = env.runtime.snapshot.model_copy(update={"revision": "b" * 64})
        else:
            env.datasets.archived = True

    env.state["on_data"] = revoke
    expected = {
        "version": PreviewVersionChanged, "catalog": PreviewVersionChanged,
        "disabled": VisualizationDisabledError, "archived": FileNotFoundError,
    }[change]
    with pytest.raises(expected):
        await invoke(env, info)
    assert data_reads(env) == SELECTED_RANGES[:1]
    assert window._SLOTS._value == 2
