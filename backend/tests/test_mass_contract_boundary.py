"""The mass-spectrum exception must not widen any other v2 capability.

Fixtures are committed synthetic outputs from the real sandbox reader. The
host storage and transport are fakes: these tests exercise the actual shared
request gate, private validation, request binding and public normalization.
"""
import asyncio
import copy
import functools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.services import extended_visualization as extended
from app.application.services import unified_visualization as unified
from app.application.services.file_preview import PreviewVersionChanged, preview_version
from app.application.services.file_service import FileService
from app.application.services.visualization_catalog import VisualizationDisabledError
from test_unified_visualization import environment, invoke


PLUGIN = "viz-mass-spectrum"
LIMIT = 2 * 1024**2
FIXTURES = Path(__file__).resolve().parents[2] / "frontend/tests/browser/mass-spectrum-data.json"


@pytest.fixture
def samples():
    return json.loads(FIXTURES.read_text())


def large_spectrum(samples, points=16384):
    value = copy.deepcopy(samples["mgf_series"])
    value["choices"]["spectra"][0]["points"] = points
    value["metadata"].update(source_bytes=points * 24, output_points=points)
    value["array"] = {"shape": [points, 2], "dimensions": ["m/z", "intensity"],
                      "values": [scalar for i in range(points) for scalar in (i + .25, -i)]}
    return value


def legacy_array(reader, size):
    return {"contract_version": 2, "type": reader, "reader": reader, "kind": "series",
            "media_type": "application/json", "array": {"shape": [size], "values": [1] * size},
            "metadata": {}, "warnings": [], "sampled": False}


def attach_worker(monkeypatch, environment, payload):
    """Keep real host services; substitute only the isolated worker transport."""
    storage, _ = environment
    meta = payload["metadata"]
    storage.infos["file"].filename = "synthetic." + meta["format"]
    storage.infos["file"].size = meta["source_bytes"]
    storage.content["file"] = b"S" * meta["source_bytes"]
    calls = []

    def worker(image, data, **kwargs):
        calls.append((image, len(data), kwargs))
        assert image == "sandbox:test" and kwargs["reader"] == "mass-spectrum"
        assert kwargs["format"] == meta["format"] and kwargs["truncated"] is False
        return {"ok": True, "data": copy.deepcopy(payload)}

    monkeypatch.setattr(extended, "_SLOTS", asyncio.Semaphore(2))
    monkeypatch.setattr(unified, "extended_visualization",
                        functools.partial(extended.extended_visualization, worker=worker))
    return calls


@pytest.mark.parametrize("reader", ["tabular", "hdf5", "root", "jcamp"])
def test_existing_numeric_readers_keep_exact_16384_scalar_budget(reader):
    value = legacy_array(reader, 16384)
    assert extended.validate_payload(value, reader, "series", LIMIT) is value
    for size in (16385, 32768):
        with pytest.raises(ValueError, match="Invalid numeric array"):
            extended.validate_payload(legacy_array(reader, size), reader, "series", LIMIT)


def test_mass_only_allows_16384_complete_pairs_not_32768_points(samples):
    value = large_spectrum(samples)
    assert len(value["array"]["values"]) == 32768
    assert extended.validate_payload(value, "mass-spectrum", "series", LIMIT) is value
    for points in (16385, 32768):
        with pytest.raises(ValueError):
            extended.validate_payload(large_spectrum(samples, points), "mass-spectrum", "series", LIMIT)


@pytest.mark.parametrize("reader", ["tabular", "hdf5", "root", "jcamp"])
@pytest.mark.parametrize("spoof", ["reader", "type", "both"])
def test_mass_envelope_cannot_grant_other_readers_larger_arrays(samples, reader, spoof):
    value = large_spectrum(samples)
    if spoof in {"reader", "both"}: value["reader"] = reader
    if spoof in {"type", "both"}: value["type"] = reader
    with pytest.raises(ValueError): extended.validate_payload(value, reader, "series", LIMIT)


def test_legacy_array_cannot_bypass_mass_semantics_by_renaming_reader():
    with pytest.raises(ValueError):
        extended.validate_payload(legacy_array("mass-spectrum", 32768), "mass-spectrum", "series", LIMIT)


@pytest.mark.parametrize("name", ["mgf_tree", "mgf_series", "mzml_tree", "mzml_series", "page_first", "page_second"])
def test_real_private_fixtures_pass_shared_validation_and_binding(samples, name):
    value = samples[name]
    assert extended.validate_payload(value, "mass-spectrum", value["kind"], LIMIT) is value
    extended.validate_requested_selection(value, "mass-spectrum", value["kind"], value["selected"],
                                          value["metadata"]["format"], value["metadata"]["source_bytes"])


@pytest.mark.parametrize("change", ["source", "format", "kind", "selection"])
def test_well_formed_payload_still_must_match_exact_request(samples, change):
    value = samples["mgf_series"]
    extended.validate_payload(value, "mass-spectrum", "series", LIMIT)
    kind, options, fmt, size = "series", value["selected"], "mgf", value["metadata"]["source_bytes"]
    if change == "source": size += 1
    if change == "format": fmt = "mzml"
    if change == "kind": kind, options = "tree", {}
    if change == "selection": options = {"spectrum": "s-000001"}
    with pytest.raises(ValueError):
        extended.validate_requested_selection(value, "mass-spectrum", kind, options, fmt, size)


@pytest.mark.parametrize("name", ["mgf_tree", "mgf_series", "mzml_series"])
def test_public_normalization_preserves_scientific_units_values_and_no_private_envelope(samples, name):
    value = samples[name]
    before = copy.deepcopy(value)
    extended.validate_payload(value, "mass-spectrum", value["kind"], LIMIT)
    result = unified.normalize_result({**value, "plugin_id": PLUGIN, "version": "b" * 64, "revision": "c" * 64})
    assert value == before
    assert result.kind == ("tree" if value["kind"] == "tree" else "array")
    assert result.metadata == value["metadata"] and result.warnings == value["warnings"] and result.sampled is False
    field = "tree" if value["kind"] == "tree" else "array"
    assert result.payload == {"view_kind": value["kind"], "media_type": "application/json",
                              field: value[field], "choices": value["choices"], "selected": value["selected"]}
    assert not {"type", "reader", "contract_version", "plugin_id", "version", "revision", "metadata"} & result.payload.keys()


def test_mass_shared_and_public_output_budgets_both_count_utf8_envelopes(samples):
    value = large_spectrum(samples)
    private_bytes = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    with pytest.raises(ValueError): extended.validate_payload(value, "mass-spectrum", "series", private_bytes - 1)
    extended.validate_payload(value, "mass-spectrum", "series", private_bytes)
    result = unified.normalize_result({**value, "plugin_id": PLUGIN, "version": "b" * 64, "revision": "c" * 64})
    public_bytes = len(json.dumps(result.model_dump(), ensure_ascii=False, allow_nan=False).encode())
    assert public_bytes > private_bytes
    plugin = SimpleNamespace(limits=SimpleNamespace(max_output_bytes=public_bytes - 1))
    with pytest.raises(unified.ScientificPreviewRejected): unified.check_output_budget(result, plugin)
    plugin.limits.max_output_bytes = public_bytes
    unified.check_output_budget(result, plugin)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["mgf_tree", "mgf_series", "mzml_tree", "mzml_series", "page_first", "page_second"])
async def test_real_fixture_through_all_shared_host_boundaries(environment, monkeypatch, samples, name):
    payload = samples[name]
    calls = attach_worker(monkeypatch, environment, payload)
    storage, _ = environment
    request = {"kind": payload["kind"], "options": payload["selected"]}
    if payload["kind"] == "series" or payload["selected"].get("offset", 0):
        request["version"] = preview_version(storage.infos["file"])
    result = await invoke(environment, PLUGIN, "preview", **request)
    assert result.payload["selected"] == payload["selected"] and result.plugin_id == PLUGIN
    assert result.version == preview_version(storage.infos["file"])
    assert storage.reads == [("file", 0, payload["metadata"]["source_bytes"])] and len(calls) == 1
    assert extended._SLOTS._value == 2


@pytest.mark.asyncio
async def test_maximum_mass_array_reaches_public_boundary_without_truncation(environment, monkeypatch, samples):
    payload = large_spectrum(samples)
    attach_worker(monkeypatch, environment, payload)
    result = await invoke(environment, PLUGIN, "preview", kind="series", options=payload["selected"],
                          version=preview_version(environment[0].infos["file"]))
    assert result.kind == "array" and result.payload["array"] == payload["array"] and not result.sampled


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,options", [(None, {}), (None, {"offset": 0}), ("tree", {}), ("tree", {"offset": 0})])
async def test_default_kind_and_explicit_zero_page_remain_valid_without_client_version(environment, monkeypatch, samples, kind, options):
    attach_worker(monkeypatch, environment, samples["mgf_tree"])
    result = await invoke(environment, PLUGIN, "preview", kind=kind, options=options)
    assert result.kind == "tree" and result.payload["selected"] == {"offset": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,options", [
    (None, {"offset": 64}), ("tree", {"offset": 64}), ("series", {"spectrum": "s-000000"}),
    (None, {"spectrum": "s-000000"}), ("tree", {"offset": True}), ("tree", {"offset": False}),
    ("tree", {"offset": 1}), ("tree", {"offset": 1024}), ("tree", {"offset": []}),
    ("tree", {"reader": "binary"}), ("tree", {"version": "b" * 64}),
    ("series", {"spectrum": "s-000000", "version": "b" * 64}),
])
async def test_missing_client_version_or_invalid_selection_cannot_read_or_start_worker(environment, monkeypatch, samples, kind, options):
    calls = attach_worker(monkeypatch, environment, samples["mgf_tree"])
    with pytest.raises(unified.ScientificPreviewRejected):
        await invoke(environment, PLUGIN, "preview", kind=kind, options=options)
    assert environment[0].reads == [] and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["foreign", "private", "disabled", "stale", "oversize", "format", "lowered-budget"])
async def test_mass_scope_and_whole_input_budget_never_start_storage_or_worker(environment, monkeypatch, samples, reason):
    calls = attach_worker(monkeypatch, environment, samples["mgf_tree"])
    storage, catalog = environment
    args = {}
    if reason == "foreign": args["user"] = "other"
    if reason == "private": storage.infos["file"].metadata = {"source": "tool_output_spill"}
    if reason == "disabled": await catalog.set_state("owner", PLUGIN, False)
    if reason == "stale": args["version"] = "f" * 64
    if reason == "oversize": storage.infos["file"].size = 16 * 1024**2 + 1
    if reason == "format": storage.infos["file"].filename = "other.mca"
    if reason == "lowered-budget":
        snapshot = await catalog.runtime.visualization_snapshot()
        catalog.runtime.visualization_snapshot.return_value = snapshot.model_copy(update={"plugins": [
            p.model_copy(update={"limits": p.limits.model_copy(update={"max_input_bytes": 1})}) if p.id == PLUGIN else p
            for p in snapshot.plugins]})
    with pytest.raises((FileNotFoundError, VisualizationDisabledError, PreviewVersionChanged, unified.ScientificPreviewRejected)):
        await invoke(environment, PLUGIN, "preview", **args)
    assert storage.reads == [] and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,options", [(None, {"offset": 64}), ("tree", {"offset": 64}), ("series", {"spectrum": "s-000000"})])
async def test_direct_extended_entry_does_not_skip_version_gate(environment, monkeypatch, samples, kind, options):
    calls = attach_worker(monkeypatch, environment, samples["mgf_tree"])
    storage, catalog = environment
    with pytest.raises(unified.ScientificPreviewRejected):
        await unified.extended_visualization(FileService(storage), catalog, "sandbox:test", "file", "owner",
            extended.ExtendedPreviewRequest(plugin_id=PLUGIN, kind=kind, options=options))
    assert storage.reads == [] and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["reader", "wrong-selection", "source", "malformed-pairs"])
async def test_valid_private_response_is_not_normalized_when_request_binding_fails(environment, monkeypatch, samples, bad):
    payload = samples["mgf_series"]
    calls = attach_worker(monkeypatch, environment, payload)
    storage, _ = environment
    if bad == "reader": payload["reader"] = payload["type"] = "hdf5"
    if bad == "wrong-selection":
        payload["selected"]["spectrum"] = "s-000001"
        payload["choices"]["spectra"][0].update(id="s-000001", index=1)
        payload["metadata"]["offset"] = 1
    if bad == "source": storage.infos["file"].size += 1; storage.content["file"] += b"S"
    if bad == "malformed-pairs": payload["array"]["values"].append(99)
    with pytest.raises(unified.VisualizationWorkerError):
        await invoke(environment, PLUGIN, "preview", kind="series", options={"spectrum": "s-000000"},
                     version=preview_version(storage.infos["file"]))
    assert len(calls) == 1 and extended._SLOTS._value == 2
