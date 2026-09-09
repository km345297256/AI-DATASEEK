import asyncio
import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.application.services import scientific_visualization as module
from app.application.services.file_service import FileService
from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.visualization_catalog import VisualizationCatalogService, VisualizationDisabledError
from app.domain.models.file import FileInfo
from app.domain.models.visualization import VisualizationSnapshot


class Preferences:
    def __init__(self):
        self.states = {}

    async def get_states(self, user_id):
        return self.states.get(user_id, {})

    async def set_state(self, user_id, plugin_id, enabled):
        self.states.setdefault(user_id, {})[plugin_id] = enabled


class Runtime:
    def __init__(self):
        root = Path(__file__).resolve().parents[2] / "plugin-host" / "visualizations"
        self.snapshot = VisualizationSnapshot(engine="cordis", revision="a" * 64,
            plugins=[json.loads(path.read_text()) for path in sorted(root.glob("*.json"))])

    async def visualization_snapshot(self):
        return self.snapshot


class Storage:
    def __init__(self, data=b"example", filename="example.nc"):
        self.data = data
        self.info = FileInfo(file_id="opaque", filename=filename, size=len(data), user_id="owner")
        self.reads = []

    async def get_file_info(self, file_id, user_id):
        return self.info if file_id == "opaque" and user_id == "owner" else None

    async def download_file_range(self, file_id, user_id, *, offset, length):
        self.reads.append((offset, length))
        return self.data[offset:offset+length], self.info

    async def download_file(self, *args, **kwargs):
        raise AssertionError("No full download fallback")


def series(reader="netcdf", kind="series"):
    return {"ok": True, "data": dict(contract_version=1, reader=reader, kind=kind,
        variables=[], selected_variable=None, x_label="index", y_label="value", x=[0, 1], y=[2.5, None],
        width=0, height=0, values=[], extent=None, metadata={}, warnings=[], sampled=False)}


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(module, "_PREVIEW_SLOTS", asyncio.Semaphore(2))
    runtime, preferences, storage = Runtime(), Preferences(), Storage()
    return VisualizationCatalogService(runtime, preferences), storage


async def invoke(environment, *, request=None, worker=None, user="owner"):
    catalog, storage = environment
    return await module.scientific_visualization(FileService(storage), catalog, "sandbox:test", "opaque", user,
        request or module.ScientificVisualizationRequest(plugin_id="netcdf-series"),
        worker=worker or (lambda *args, **kwargs: series()))


@pytest.mark.asyncio
async def test_authorized_range_and_versioned_checked_envelope(environment):
    result = await invoke(environment)
    assert result.plugin_id == "netcdf-series"
    assert result.revision == "a" * 64 and len(result.version) == 64
    assert result.y == [2.5, None]
    assert environment[1].reads == [(0, 7)]


@pytest.mark.asyncio
async def test_disabled_owner_does_not_affect_other_owner(environment):
    catalog, storage = environment
    await catalog.set_state("other", "netcdf-series", False)
    await invoke(environment)
    await catalog.set_state("owner", "netcdf-series", False)
    storage.reads.clear()
    with pytest.raises(VisualizationDisabledError):
        await invoke(environment)
    assert storage.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_ownership_and_spill_guard_before_read(environment, private):
    if private:
        environment[1].info.metadata = {"source": "tool_output_spill"}
    with pytest.raises(FileNotFoundError):
        await invoke(environment, user="owner" if private else "foreign")
    assert environment[1].reads == []


@pytest.mark.asyncio
async def test_binary_oversize_and_mismatch_rejected_before_read(environment):
    environment[1].info.size = module.MAX_INPUT_BYTES + 1
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment)
    environment[1].info.filename = "example.html"
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment)
    assert environment[1].reads == []


@pytest.mark.asyncio
async def test_version_mismatch_and_range_metadata_drift(environment):
    with pytest.raises(PreviewVersionChanged):
        await invoke(environment, request=module.ScientificVisualizationRequest(plugin_id="netcdf-series", version="f"*64))
    assert environment[1].reads == []
    async def changed(*args, **kwargs):
        return b"example", environment[1].info.model_copy(update={"size": 77})
    environment[1].download_file_range = changed
    with pytest.raises(PreviewVersionChanged):
        await invoke(environment)


@pytest.mark.asyncio
async def test_fastq_only_reads_bounded_prefix(environment):
    storage = environment[1]
    storage.info.filename = "large.fastq"
    storage.info.size = 100 * 1024 * 1024
    storage.data = b"x" * module.MAX_FASTQ_BYTES
    def worker(image, data, **kwargs):
        assert len(data) == module.MAX_FASTQ_BYTES
        assert kwargs["truncated"] and kwargs["reader"] == "fastq" and kwargs["kind"] == "quality"
        return series("fastq", "quality")
    await invoke(environment, request=module.ScientificVisualizationRequest(plugin_id="fastq-quality"), worker=worker)
    assert storage.reads == [(0, module.MAX_FASTQ_BYTES)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reader", "shape", "nan", "path", "huge", "unknown"])
async def test_untrusted_worker_output_fails_protocol(environment, change):
    def worker(*args, **kwargs):
        result = series()
        data = result["data"]
        if change == "reader": data["reader"] = "fits"
        if change == "shape": data["x"] = [0]
        if change == "nan": data["y"] = [float("nan"), 1]
        if change == "path": data["metadata"] = {"source": "/Users/private/data.nc"}
        if change == "huge": data["x_label"] = "x" * 10000
        if change == "unknown": data["javascript"] = "alert(1)"
        return result
    with pytest.raises(module.VisualizationWorkerError):
        await invoke(environment, worker=worker)


@pytest.mark.asyncio
async def test_disable_while_worker_runs_rejects_late_output(environment):
    catalog, _ = environment
    started, release = threading.Event(), threading.Event()
    def worker(*args, **kwargs):
        started.set()
        release.wait(2)
        return series()
    task = asyncio.create_task(invoke(environment, worker=worker))
    await asyncio.to_thread(started.wait, 2)
    await catalog.set_state("owner", "netcdf-series", False)
    release.set()
    with pytest.raises(VisualizationDisabledError):
        await task


@pytest.mark.asyncio
async def test_cancel_signals_worker_and_keeps_slot_until_cleanup(environment):
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def worker(*args, **kwargs):
        started.set()
        kwargs["cancelled"].wait(2)
        stopped.set()
        release.wait(2)
        return series()
    task = asyncio.create_task(invoke(environment, worker=worker))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(stopped.wait, 2)
    assert module._PREVIEW_SLOTS._value == 1
    release.set()
    for _ in range(100):
        if module._PREVIEW_SLOTS._value == 2:
            break
        await asyncio.sleep(0.005)
    assert module._PREVIEW_SLOTS._value == 2


@pytest.mark.parametrize("values", [{"indices": {"time": True}}, {"indices": {"time": -1}}, {"variable": "../../private"},
    {"hdu": True}, {"javascript": "bad"}, {"plugin_id": "../bad"}])
def test_request_contract_rejects_coercion_and_unsafe_options(values):
    with pytest.raises(ValidationError):
        module.ScientificVisualizationRequest(**{"plugin_id": "netcdf-series", **values})


def test_map_shape_coordinate_bounds_and_order():
    payload = series()["data"]
    payload.update(kind="map", width=2, height=2, x=[0, 1], y=[1, 0], values=[0, 1, 2, None], extent=[0, 0, 1, 1])
    module.ScientificVisualizationResult.model_validate(payload)
    payload["y"] = [0, 1]
    with pytest.raises(ValidationError):
        module.ScientificVisualizationResult.model_validate(payload)


@pytest.mark.asyncio
async def test_lower_plugin_output_budget_is_enforced(environment):
    catalog, _ = environment
    original = catalog.runtime.snapshot
    catalog.runtime.snapshot = original.model_copy(update={"plugins": [
        plugin.model_copy(update={"limits": plugin.limits.model_copy(update={"max_output_bytes": 32})})
        if plugin.id == "netcdf-series" else plugin for plugin in original.plugins
    ]})
    with pytest.raises(module.VisualizationWorkerError):
        await invoke(environment)


@pytest.mark.asyncio
async def test_cancel_during_native_range_read_retains_slot(environment):
    storage = environment[1]
    started, release = threading.Event(), threading.Event()
    def read():
        started.set()
        release.wait(2)
        return storage.data, storage.info
    async def slow_range(*args, **kwargs):
        return await asyncio.to_thread(read)
    storage.download_file_range = slow_range
    task = asyncio.create_task(invoke(environment))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert module._PREVIEW_SLOTS._value == 1
    release.set()
    for _ in range(100):
        if module._PREVIEW_SLOTS._value == 2:
            break
        await asyncio.sleep(0.005)
    assert module._PREVIEW_SLOTS._value == 2
