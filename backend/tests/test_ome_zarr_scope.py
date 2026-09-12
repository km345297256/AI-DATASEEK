"""Exact registered local Zarr scope; fake storage, never production data/API."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from app.application.services import ome_zarr_scope as module
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.dataset import DatasetFile, DatasetStorageType
from app.domain.models.visualization import VisualizationPlugin
from pydantic import ValidationError
from pathlib import Path
from test_dataset_file_preview import env, prepare  # noqa: F401: existing isolated fixtures


PLUGIN = "viz-ome-zarr"
KEYS = [".zattrs", ".zgroup", "0/.zarray", "0/0.0", "0/0.1"]


def configure(env, *, host=False):
    location = env.datasets.dataset.locations[0]
    prefix = ""
    if host:
        location.storage_type = DatasetStorageType.HOST_PATH
        location.source_path = "/allowed/private-source"
        location.mount_name = "data"
        prefix = f"sources/{location.location_id}/data/"
    # Arbitrary JSON, labels, nested groups and sibling roots are not capabilities.
    extra = ["labels/.zattrs", "private.json", "notes.txt"]
    paths = [f"image.zarr/{key}" for key in KEYS + extra] + ["other.zarr/.zattrs"]
    env.datasets.dataset.files = [DatasetFile(path=prefix + path, size=8) for path in paths]
    env.state.update(objects={path: bytes([i + 1]) * 8 for i, path in enumerate(paths)}, versions={path: 1 for path in paths}, on_data=None)

    def reader(source, relative, *, offset, length, max_bytes):
        env.state["reads"].append((source, relative, offset, length, max_bytes))
        assert length is not None, "Scope must never download a complete object without a range"
        data = env.state["objects"][relative][offset:offset + length]
        if length and env.state["on_data"]:
            env.state["on_data"]()
        return data, {"size": len(env.state["objects"][relative]), "mtime_ns": env.state["versions"][relative], "ctime_ns": 1, "inode": paths.index(relative) + 1, "device": 3}

    env.service.reader = reader
    return prefix


async def setup_scope(env, **kwargs):
    configure(env, **kwargs)
    info = (await prepare(env, path="image.zarr/.zattrs", plugin=PLUGIN)).file
    env.state["reads"].clear()
    scope = await module.DatasetObjectScope(env.service, info.file_id, "owner").initialize()
    return scope


@pytest.fixture(autouse=True)
def fresh_slots(monkeypatch):
    monkeypatch.setattr(module, "_SCOPE_SLOTS", asyncio.Semaphore(2))


@pytest.mark.parametrize("filename", [".env", "../.zattrs", "a/.zattrs", ".zgroup", ".zattrs/", ".zattrs\0", "..zattrs"])
def test_only_approved_hidden_zattrs_filename_enters_contract(filename):
    manifest = json.loads((Path(__file__).resolve().parents[2] / "plugin-host/visualizations/ome-zarr.json").read_text())
    accepted = VisualizationPlugin.model_validate(manifest)
    assert accepted.matches_filename("folder/image.zarr/.zattrs")
    manifest["filenames"] = [filename]
    with pytest.raises(ValidationError): VisualizationPlugin.model_validate(manifest)


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [False, True])
async def test_registered_scope_is_stat_only_and_contains_exact_inert_keys(env, host):
    scope = await setup_scope(env, host=host)
    assert [item["key"] for item in scope.resources] == sorted(KEYS)
    assert all(set(item) == {"key", "offset", "size"} for item in scope.resources)
    assert [(r["offset"], r["size"]) for r in scope.resources] == [(i * 8, 8) for i in range(5)]
    assert all(read[3] == 0 for read in env.state["reads"])
    info = await scope.get_file_info(scope.source_id, "owner")
    assert info.size == 40 and info.file_path is None and info.file_url is None
    serialized = info.model_dump_json()
    for secret in ("/allowed", "/data/datasets", "private-source", "sources/", "labels", "image.zarr"):
        assert secret not in serialized
    assert set(info.metadata) == {"source", "dataset_file_version"}


@pytest.mark.asyncio
async def test_exact_member_range_cannot_cross_boundary_or_read_ignored_siblings(env):
    scope = await setup_scope(env)
    resource = next(r for r in scope.resources if r["key"] == "0/0.1")
    env.state["reads"].clear()
    data, info = await scope.download_file_range(scope.source_id, "owner", offset=resource["offset"] + 2, length=3)
    assert data == b"\5" * 3 and info is scope.info
    assert len(env.state["reads"]) == 1 and env.state["reads"][0][1:4] == ("image.zarr/0/0.1", 2, 3)
    for offset, length in [(7, 2), (-1, 1), (40, 1), (True, 1), (0, True), (0, 1024**2 + 1)]:
        with pytest.raises(ValueError):
            await scope.download_file_range(scope.source_id, "owner", offset=offset, length=length)
    assert len(env.state["reads"]) == 1


@pytest.mark.asyncio
async def test_foreign_owner_or_reference_and_mismatched_anchor_never_read(env):
    scope = await setup_scope(env)
    env.state["reads"].clear()
    for source, owner in [(scope.source_id, "foreign"), ("dataset-preview:" + "0" * 64, "owner")]:
        with pytest.raises(FileNotFoundError): await scope.download_file_range(source, owner, offset=0, length=1)
    with pytest.raises(FileNotFoundError): await module.DatasetObjectScope(env.service, scope.source_id, "foreign").initialize()
    env.repository.rows[scope.source_id]["anchor"] = "other.zarr/.zattrs"
    with pytest.raises(FileNotFoundError): await scope.check_live()
    assert not env.state["reads"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["archive", "member_removed", "member_added", "location", "verified", "declaration_size", "duplicate"])
async def test_inventory_and_location_changes_revoke_entire_scope(env, mutation):
    scope = await setup_scope(env); env.state["reads"].clear()
    if mutation == "archive": env.datasets.archived = True
    elif mutation == "member_removed": env.datasets.dataset.files = env.datasets.dataset.files[1:]
    elif mutation == "member_added": env.datasets.dataset.files.append(DatasetFile(path="image.zarr/0/0.2", size=8))
    elif mutation == "location": env.datasets.dataset.locations[0].version = "changed"
    elif mutation == "verified": env.datasets.dataset.locations[0].verified = False
    elif mutation == "declaration_size": env.datasets.dataset.files[3].size = 99
    else: env.datasets.dataset.files.append(env.datasets.dataset.files[2].model_copy())
    with pytest.raises((ValueError, FileNotFoundError)):
        await scope.download_file_range(scope.source_id, "owner", offset=0, length=1)
    assert not env.state["reads"]


@pytest.mark.asyncio
async def test_unread_member_version_is_in_scope_pin_and_final_snapshot(env):
    scope = await setup_scope(env); old_version = preview_version(scope.info)
    env.state["versions"]["image.zarr/0/0.1"] += 1
    with pytest.raises(PreviewVersionChanged): await scope.verify_snapshot()
    fresh = await module.DatasetObjectScope(env.service, scope.source_id, "owner").initialize()
    assert preview_version(fresh.info) != old_version


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "member_removed", "selected_stat"])
async def test_during_byte_read_revocation_never_returns_data(env, change):
    scope = await setup_scope(env)
    def mutate():
        if change == "archive": env.datasets.archived = True
        elif change == "member_removed": env.datasets.dataset.files = env.datasets.dataset.files[1:]
        else: env.state["versions"]["image.zarr/.zattrs"] += 1
    env.state["on_data"] = mutate
    with pytest.raises((ValueError, FileNotFoundError)):
        await scope.download_file_range(scope.source_id, "owner", offset=0, length=1)


@pytest.mark.asyncio
async def test_host_stats_and_ranges_receive_current_allowlist(env, monkeypatch):
    scope = await setup_scope(env, host=True)
    expected = copy.deepcopy(scope.stats)
    env.service.reader = None
    seen = []
    def stats(source, paths, *, configured_roots):
        seen.append(("stats", source, paths, configured_roots)); return expected
    def reader(source, relative, *, configured_roots, offset, length, max_bytes):
        seen.append(("read", source, relative, configured_roots)); return b"\1", expected[0]
    monkeypatch.setattr(module, "stat_dataset_host_files", stats)
    monkeypatch.setattr(module, "read_dataset_host_file", reader)
    await scope.verify_snapshot()
    await scope.download_file_range(scope.source_id, "owner", offset=0, length=1)
    assert all(row[3] == "/allowed" for row in seen)
    env.service.settings.dataset_host_path_allowlist = "/new-allowlist"
    await scope.verify_snapshot()
    assert seen[-1][3] == "/new-allowlist"


@pytest.mark.asyncio
async def test_disabled_plugin_and_ordinary_upload_reject_before_scope_initialization(env):
    scope = await setup_scope(env)
    request = SimpleNamespace(plugin_id=PLUGIN, operation="preview", kind="tree", options={}, version=None)
    calls = []
    def forbidden(*args): calls.append(args); raise AssertionError("scope should not open")
    service = SimpleNamespace(_file_storage=SimpleNamespace(previews=env.service))
    await env.catalog.set_state("owner", PLUGIN, False)
    with pytest.raises(VisualizationDisabledError): await module.ome_zarr_visualization(service, env.catalog, "test", scope.source_id, "owner", request, scope_factory=forbidden)
    with pytest.raises(ValueError): await module.ome_zarr_visualization(service, env.catalog, "test", "uploaded-file", "owner", request, scope_factory=forbidden)
    assert not calls


@pytest.mark.asyncio
async def test_native_cancel_retains_slot_until_storage_work_really_finishes():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def call(): entered.set(); release.wait(5); finished.set(); return []
    task = asyncio.create_task(module._native(call))
    assert await asyncio.to_thread(entered.wait, 2)
    assert module._SCOPE_SLOTS._value == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert module._SCOPE_SLOTS._value == 1
    release.set(); assert await asyncio.to_thread(finished.wait, 2)
    for _ in range(20): await asyncio.sleep(0)
    assert module._SCOPE_SLOTS._value == 2
