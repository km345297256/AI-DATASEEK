"""Authorization, real range budgets, cancellation ownership and EDF schema."""
import asyncio
import copy
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.application.services import window_visualization as module
from app.application.services.file_service import FileService
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.visualization_catalog import VisualizationDisabledError
from app.domain.models.file import FileInfo


@dataclass
class Plugin:
    id: str = "viz-edf-signals"
    reader: str = "edf"
    adapter: str = "signal-window"
    version: str = "1.0.0"
    capabilities: object = field(default_factory=lambda: SimpleNamespace(operations=["preview"], input_mode="window", shared=False))
    limits: object = field(default_factory=lambda: SimpleNamespace(max_input_bytes=8 * 1024**2, max_output_bytes=2 * 1024**2))

    def matches_filename(self, name):
        return name.lower().endswith((".edf", ".bdf"))


class Catalog:
    def __init__(self):
        self.plugin, self.enabled, self.revision = Plugin(), True, "a" * 64

    async def require_enabled(self, user, plugin):
        assert plugin == "viz-edf-signals"
        if not self.enabled:
            raise VisualizationDisabledError("disabled")
        return copy.deepcopy(self.plugin)

    async def list_for_user(self, user):
        return SimpleNamespace(revision=self.revision)


class Storage:
    def __init__(self):
        self.info = FileInfo(file_id="opaque", filename="long.edf", size=1024**3, user_id="owner")
        self.reads, self.before_read, self.after_read, self.on_info = [], None, None, None
        self.short = False

    async def get_file_info(self, file_id, user):
        if self.on_info:
            await self.on_info()
        return self.info.model_copy(deep=True) if file_id == "opaque" and user == "owner" else None

    async def download_file_range(self, file_id, user, *, offset, length):
        assert file_id == "opaque" and user == "owner"
        if self.before_read:
            await self.before_read()
        self.reads.append((offset, length))
        if self.after_read:
            await self.after_read()
        return b"A" * (length - 1 if self.short else length), self.info.model_copy(deep=True)

    async def download_file(self, *_):
        raise AssertionError("Full-source download is forbidden")


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(module, "_SLOTS", asyncio.Semaphore(2))
    return Catalog(), Storage()


def payload(size=1024**3, read_bytes=4, read_requests=1):
    return {"contract_version": 2, "type": "edf", "reader": "edf", "kind": "series", "media_type": "application/json",
        "series": [{"channel": 0, "label": "EEG", "unit": "uV", "sample_rate": 4, "x": [0, .25, .5, .75], "y": [-2, 1.0000000000000002, 4, 8]}],
        "choices": {"channels": [{"id": 0, "label": "EEG", "unit": "uV", "sample_rate": 4, "samples_per_record": 4,
            "selectable": True, "channel_type": "signal", "physical_min": -100, "physical_max": 100, "digital_min": -32768, "digital_max": 32767}]},
        "selected": {"channels": [0], "start_seconds": 0, "duration_seconds": 1},
        "metadata": {"format": "edf", "variant": "EDF+C", "total_duration_seconds": 2, "record_duration_seconds": 1,
            "records": 2, "signal_count": 1, "header_bytes": 512, "source_bytes": size, "read_bytes": read_bytes, "read_requests": read_requests,
            "identity_fields_hidden": True, "annotations_hidden": True, "no_resampling": True, "calibration": module.CALIBRATION,
            "time_origin": "first data record; relative seconds", "continuity": "header-declared; annotation timeline not read",
            "limits": {"max_samples": 16384, "max_channels": 8, "max_duration_seconds": 60, "max_header_bytes": 1024**2, "max_source_bytes": 8 * 1024**3}},
        "warnings": ["仅显示所选通道和相对时间窗；不重采样、不滤波、不用于诊断。"], "sampled": True}


def worker(_image, **kwargs):
    data = kwargs["read_range"](0, 4)
    assert data == b"AAAA"
    return {"ok": True, "data": payload(size=kwargs["size"])}


async def invoke(env, *, request=None, user="owner", selected_worker=worker, image="sandbox:test"):
    return await module.window_visualization(FileService(env[1]), env[0], image, "opaque", user,
        request or SimpleNamespace(plugin_id="viz-edf-signals", version=None, kind="series", options={}), worker=selected_worker)


@pytest.mark.asyncio
async def test_long_source_reads_only_requested_window_and_no_source_identity_enters_worker(environment):
    received = {}
    def inspect(image, **kwargs):
        received.update(kwargs)
        return worker(image, **kwargs)
    result = await invoke(environment, selected_worker=inspect)
    assert environment[1].reads == [(0, 4)]
    assert result["version"] == preview_version(environment[1].info) and result["revision"] == "a" * 64
    assert result["series"][0]["y"][1] == 1.0000000000000002
    assert received["size"] == 1024**3 and received["limits"]["max_total_bytes"] == 8 * 1024**2
    assert not {"file_id", "user_id", "path", "data", "filename"} & received.keys()
    assert module._SLOTS._value == 2


@pytest.mark.asyncio
async def test_foreign_spill_disabled_reject_before_worker_or_bytes(environment):
    with pytest.raises(FileNotFoundError):
        await invoke(environment, user="foreign")
    environment[1].info.metadata = {"source": "tool_output_spill"}
    with pytest.raises(FileNotFoundError):
        await invoke(environment)
    environment[1].info.metadata = {}
    environment[0].enabled = False
    with pytest.raises(VisualizationDisabledError):
        await invoke(environment)
    assert environment[1].reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("reader", "hdf5"), ("adapter", "h5web"), ("input_mode", "whole"), ("shared", True), ("operations", ["bytes"])])
async def test_requires_explicit_window_capability(environment, field, value):
    target = environment[0].plugin if field in {"reader", "adapter"} else environment[0].plugin.capabilities
    setattr(target, field, value)
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment)
    assert environment[1].reads == []


@pytest.mark.parametrize("options", [{"channels": []}, {"channels": [1, 1]}, {"channels": [True]}, {"channels": [256]},
    {"channels": list(range(9))}, {"start_seconds": -1}, {"start_seconds": True}, {"start_seconds": "0"},
    {"start_seconds": float("nan")}, {"start_seconds": float("inf")}, {"start_seconds": 10**1000},
    {"duration_seconds": 0}, {"duration_seconds": 60.1}, {"duration_seconds": float("inf")}, {"path": "/private/file"}, {"offset": 0}])
def test_options_are_typed_bounded_and_not_a_raw_browser_range_api(options):
    with pytest.raises(module.ScientificPreviewRejected):
        module.validate_window_options(options)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, -1, 8 * 1024**3 + 1])
async def test_separate_hard_source_limit(environment, size):
    environment[1].info.size = size
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment)
    assert environment[1].reads == []


@pytest.mark.asyncio
async def test_stale_version_and_missing_range_provider_fail_before_source_read(environment):
    with pytest.raises(PreviewVersionChanged):
        await invoke(environment, request=SimpleNamespace(plugin_id="viz-edf-signals", version="f" * 64, kind="series", options={}))
    environment[1].download_file_range = None
    with pytest.raises(NotImplementedError):
        await invoke(environment)
    assert environment[1].reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,length", [(-1, 1), (True, 1), (1024**3 - 1, 2), (0, 0), (0, True), (0, 1024**2 + 1)])
async def test_service_checks_ranges_even_when_injected_worker_bypasses_wire(environment, offset, length):
    def bad(_image, **kwargs):
        kwargs["read_range"](offset, length)
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment, selected_worker=bad)
    assert environment[1].reads == [] and module._SLOTS._value == 2


@pytest.mark.asyncio
async def test_plugin_budget_is_total_range_bytes_not_whole_source_limit(environment):
    environment[0].plugin.limits.max_input_bytes = 5
    def bad(_image, **kwargs):
        kwargs["read_range"](0, 4)
        kwargs["read_range"](0, 2)
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment, selected_worker=bad)
    assert environment[1].reads == [(0, 4)]


@pytest.mark.asyncio
async def test_repeated_ranges_consume_request_budget(environment, monkeypatch):
    monkeypatch.setitem(module.WINDOW_PROFILES["edf"], "reads", 2)
    def bad(_image, **kwargs):
        for _ in range(3):
            kwargs["read_range"](0, 1)
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment, selected_worker=bad)
    assert environment[1].reads == [(0, 1)] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "catalog", "plugin", "disabled", "spill"])
async def test_post_read_fences_revoke_bytes_and_result(environment, change):
    catalog, storage = environment
    async def revoke():
        if change == "version": storage.info.metadata = {"sha256": "changed"}
        if change == "catalog": catalog.revision = "b" * 64
        if change == "plugin": catalog.plugin.version = "2.0.0"
        if change == "disabled": catalog.enabled = False
        if change == "spill": storage.info.metadata = {"source": "tool_output_spill"}
    storage.after_read = revoke
    with pytest.raises((PreviewVersionChanged, VisualizationDisabledError, FileNotFoundError)):
        await invoke(environment)
    assert storage.reads == [(0, 4)]


@pytest.mark.asyncio
async def test_short_range_is_not_padded_or_returned(environment):
    environment[1].short = True
    with pytest.raises(module.ScientificPreviewRejected):
        await invoke(environment)


@pytest.mark.asyncio
async def test_cancel_keeps_slot_until_storage_and_worker_cleanup_finish(environment):
    started, release = asyncio.Event(), asyncio.Event()
    stopped = threading.Event()
    async def block():
        started.set()
        await release.wait()
    environment[1].before_read = block
    def cancellable(image, **kwargs):
        try:
            return worker(image, **kwargs)
        finally:
            stopped.set()
    task = asyncio.create_task(invoke(environment, selected_worker=cancellable))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(stopped.wait, 2)
    assert module._SLOTS._value == 1
    release.set()
    for _ in range(100):
        if module._SLOTS._value == 2: break
        await asyncio.sleep(.005)
    assert module._SLOTS._value == 2


@pytest.mark.asyncio
async def test_total_timeout_retains_slot_until_native_cleanup(environment, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(module, "REQUEST_TIMEOUT_SECONDS", .1)
    def slow(_image, **kwargs):
        started.set()
        release.wait(2)
        return {"ok": False, "error": "rejected"}
    task = asyncio.create_task(invoke(environment, selected_worker=slow))
    await asyncio.to_thread(started.wait, 2)
    with pytest.raises(module.VisualizationWorkerError):
        await task
    assert module._SLOTS._value == 1
    release.set()
    for _ in range(100):
        if module._SLOTS._value == 2: break
        await asyncio.sleep(.005)
    assert module._SLOTS._value == 2


@pytest.mark.asyncio
async def test_single_read_timeout_returns_promptly_but_holds_native_io_slot(environment, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(module, "READ_TIMEOUT_SECONDS", .02)
    async def block():
        started.set()
        await release.wait()
    environment[1].before_read = block
    task = asyncio.create_task(invoke(environment))
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(module.VisualizationWorkerError):
        await asyncio.wait_for(task, 2)
    assert module._SLOTS._value == 1
    release.set()
    for _ in range(100):
        if module._SLOTS._value == 2: break
        await asyncio.sleep(.005)
    assert module._SLOTS._value == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "catalog", "plugin", "disabled", "spill"])
async def test_final_fence_after_worker_rejects_a_revoked_result(environment, change):
    catalog, storage = environment
    def revoke(image, **kwargs):
        result = worker(image, **kwargs)
        if change == "version": storage.info.metadata = {"sha256": "changed"}
        if change == "catalog": catalog.revision = "b" * 64
        if change == "plugin": catalog.plugin.version = "2.0.0"
        if change == "disabled": catalog.enabled = False
        if change == "spill": storage.info.metadata = {"source": "tool_output_spill"}
        return result
    with pytest.raises((PreviewVersionChanged, VisualizationDisabledError, FileNotFoundError)):
        await invoke(environment, selected_worker=revoke)


@pytest.mark.asyncio
async def test_disable_during_final_file_metadata_await_is_not_masked(environment):
    completed = threading.Event()
    def complete(image, **kwargs):
        result = worker(image, **kwargs)
        completed.set()
        return result
    async def during_info():
        if completed.is_set():
            environment[0].enabled = False
    environment[1].on_info = during_info
    with pytest.raises(VisualizationDisabledError):
        await invoke(environment, selected_worker=complete)


def validate(value):
    return module.validate_window_payload(value, size=1024**3, read_bytes=4, read_requests=1, limit=2 * 1024**2)


def test_distinct_channel_sample_rates_and_calibration_are_preserved():
    result = payload()
    other = {**result["choices"]["channels"][0], "id": 1, "label": "ECG", "sample_rate": 2, "samples_per_record": 2}
    result["choices"]["channels"].append(other)
    result["metadata"]["signal_count"] = 2
    result["selected"]["channels"].append(1)
    result["series"].append({"channel": 1, "label": "ECG", "unit": "uV", "sample_rate": 2, "x": [0, .5], "y": [1, 2]})
    assert validate(result)["series"][1]["x"] == [0, .5]


@pytest.mark.parametrize("section,key,value", [(None, "patient", "secret"), ("metadata", "identity_fields_hidden", False),
    ("metadata", "patient_id", "private"), ("metadata", "source_bytes", 42), ("metadata", "read_bytes", 8),
    ("metadata", "read_requests", 0), ("metadata", "variant", "EDF+D"), ("metadata", "no_resampling", False),
    ("metadata", "total_duration_seconds", float("nan")), ("selected", "duration_seconds", 61),
    ("selected", "channels", [1]), ("selected", "start_seconds", 3)])
def test_untrusted_schema_refuses_identity_and_unverified_read_semantics(section, key, value):
    result = payload()
    target = result if section is None else result[section]
    target[key] = value
    with pytest.raises((ValueError, TypeError)):
        validate(result)


@pytest.mark.parametrize("key,value", [("x", [0, 0, .5, .75]), ("x", [0, .2, .5, .75]), ("y", [1, float("nan"), 3, 4]),
    ("y", [1, 2]), ("sample_rate", True), ("unit", "mV"), ("channel", True), ("label", "/private/patient")])
def test_series_shape_timebase_label_and_value_guards(key, value):
    result = payload()
    result["series"][0][key] = value
    with pytest.raises((ValueError, TypeError)):
        validate(result)


@pytest.mark.asyncio
async def test_normalized_envelope_also_fits_plugin_output_budget(environment):
    environment[0].plugin.limits.max_output_bytes = 1500
    with pytest.raises(module.VisualizationWorkerError):
        await invoke(environment)


@pytest.mark.asyncio
async def test_arbitrary_parser_and_storage_exceptions_do_not_leak(environment):
    def bad(_image, **kwargs):
        raise RuntimeError("/private/secret patient")
    with pytest.raises(module.VisualizationWorkerError) as error:
        await invoke(environment, selected_worker=bad)
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["channels", "start_seconds", "duration_seconds", "format"])
async def test_self_consistent_worker_result_must_match_explicit_request_and_source_format(environment, change):
    options = {"channels": [0], "start_seconds": 0, "duration_seconds": 1}
    if change == "duration_seconds": options["duration_seconds"] = .5
    def unrelated(image, **kwargs):
        response = worker(image, **kwargs)
        value = response["data"]
        if change == "channels":
            choice = {**value["choices"]["channels"][0], "id": 1}
            value["choices"]["channels"].append(choice)
            value["metadata"]["signal_count"] = 2
            value["selected"]["channels"] = [1]
            value["series"][0]["channel"] = 1
        if change == "start_seconds":
            value["selected"]["start_seconds"] = .25
            value["series"][0]["x"] = [.25, .5, .75, 1]
        if change == "format":
            value["metadata"]["format"] = "bdf"
            value["metadata"]["variant"] = "BDF"
        # The shape/calibration/series protocol is internally valid, so this
        # test specifically exercises binding to the user's request/source.
        validate(value)
        return response
    request = SimpleNamespace(plugin_id="viz-edf-signals", version=None, kind="series", options=options)
    with pytest.raises(module.VisualizationWorkerError, match="协议校验"):
        await invoke(environment, request=request, selected_worker=unrelated)
