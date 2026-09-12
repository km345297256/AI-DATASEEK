"""Strict boundary tests: no source IO, NumPy, ObsPy or native decoder."""
import copy
import pytest
from app.application.services.seismic_window_visualization import (
    SeismicWindowError, LIMITS, VALUE_SEMANTICS, TIME_AXIS, WARNINGS,
    validate_seismic_window_payload, validate_seismic_window_options,
)


def fixture(kind="series"):
    r = {"id": 0, "label": "XX.TEST.00.BHZ", "variant": "MiniSEED2", "encoding": "INT32", "byte_order": "big", "samples": 4, "sample_interval": .25,
        "unit": "unknown", "unit_source": "not supplied", "declared_scale": None, "start_time": "2024-02-29T00:00:00.000000Z", "begin_seconds": 0,
        "time_adjustment_seconds": 0, "timing_quality": None, "quality_flags": 0, "relation": "uncompared", "gap_seconds": None}
    m = {"format": "mseed", "input_mode": "window", "source_bytes": 512, "read_bytes": 56 if kind == "tree" else 72, "read_requests": 2 if kind == "tree" else 3,
        "record_bytes": 512, "record_slots": 1, "catalog_offset": 0, "catalog_count": 1, "catalog_complete": True,
        "decoded_samples": 0 if kind == "tree" else 4, "decoded_bytes": 0 if kind == "tree" else 16, "nonfinite_values": 0,
        "value_semantics": VALUE_SEMANTICS, "time_axis": TIME_AXIS, "limits": dict(LIMITS)}
    value = {"contract_version": 2, "type": "seismic-window", "reader": "seismic-window", "kind": kind, "media_type": "application/json", "choices": {"records": [r]},
        "selected": {} if kind == "tree" else {"record": 0, "start_sample": 0, "sample_count": 4}, "metadata": m, "warnings": list(WARNINGS), "sampled": False}
    if kind == "tree": value["tree"] = [{"path": "/record-0", "node_type": "record", "attributes": {"label": r["label"], "encoding": r["encoding"]}}]
    else: value["series"] = [{"record": 0, "label": r["label"], "unit": "unknown", "x": [0, .25, .5, .75], "y": [1, -2, 3, 4]}]
    return value


@pytest.mark.parametrize("kind", ["tree", "series"])
def test_valid_bound_response(kind):
    value = fixture(kind); m = value["metadata"]
    assert validate_seismic_window_payload(value, kind=kind, options=value["selected"], fmt="mseed", source_bytes=512, read_bytes=m["read_bytes"], read_requests=m["read_requests"]) is value


@pytest.mark.parametrize("binding", [{"kind": "tree"}, {"fmt": "sac"}, {"source_bytes": 513}, {"read_bytes": 1}, {"read_requests": 1},
    {"options": {"record": 0, "start_sample": 1, "sample_count": 3}}])
def test_response_bound_to_actual_source_and_request(binding):
    with pytest.raises(SeismicWindowError): validate_seismic_window_payload(fixture(), **binding)


@pytest.mark.parametrize("path,new", [
    (("kind",), []), (("contract_version",), True), (("reader",), "edf"), (("sampled",), True), (("warnings",), ["untrusted"]),
    (("metadata", "format"), "ms"), (("metadata", "source_bytes"), True), (("metadata", "source_bytes"), 8589934593), (("metadata", "read_bytes"), 8388609),
    (("metadata", "read_requests"), 129), (("metadata", "record_bytes"), 513), (("metadata", "record_slots"), 2), (("metadata", "catalog_count"), 17),
    (("metadata", "catalog_complete"), False), (("metadata", "decoded_samples"), 65536), (("metadata", "decoded_bytes"), 524281), (("metadata", "nonfinite_values"), 1),
    (("choices", "records", 0, "id"), True), (("choices", "records", 0, "label"), "/private/secret"), (("choices", "records", 0, "samples"), 3),
    (("choices", "records", 0, "sample_interval"), .5), (("choices", "records", 0, "unit"), "nm"), (("choices", "records", 0, "declared_scale"), 2),
    (("choices", "records", 0, "start_time"), "2024-02-30T00:00:00.000000Z"), (("choices", "records", 0, "start_time"), "https://bad"),
    (("choices", "records", 0, "timing_quality"), 101), (("choices", "records", 0, "quality_flags"), 256), (("choices", "records", 0, "relation"), "merged"),
    (("series", 0, "record"), True), (("series", 0, "x", 0), .01), (("series", 0, "y", 0), True), (("series", 0, "y", 0), 2**31),
    (("series", 0, "y", 0), None), (("series", 0, "y", 0), float("inf")), (("selected", "sample_count"), 5),
])
def test_exact_types_fields_and_scientific_bounds(path, new):
    value = fixture(); target = value
    for key in path[:-1]: target = target[key]
    target[path[-1]] = new
    with pytest.raises(SeismicWindowError): validate_seismic_window_payload(value)


@pytest.mark.parametrize("kind,options", [(None, {}), ([], {}), ("tree", None), ("tree", {"record_offset": True, "record_limit": 1}), ("tree", {"record_offset": 0, "record_limit": 17}),
    ("series", {}), ("series", {"record": 0, "start_sample": -1, "sample_count": 1}), ("series", {"record": 0, "start_sample": 0, "sample_count": 16385}),
    ("series", {"record": 0, "start_sample": 0, "sample_count": 1, "merge": True})])
def test_invalid_options(kind, options):
    with pytest.raises(SeismicWindowError): validate_seismic_window_options(kind, options)


@pytest.mark.parametrize("location", ["root", "metadata", "record", "tree", "choices"])
def test_no_free_form_metadata_or_paths(location):
    value = fixture("tree"); target = {"root": value, "metadata": value["metadata"], "record": value["choices"]["records"][0], "tree": value["tree"][0], "choices": value["choices"]}[location]
    target["path_leak"] = "/private/secret"
    with pytest.raises(SeismicWindowError): validate_seismic_window_payload(value)


def test_directory_selection_is_exact_and_order_independent():
    value = fixture("tree"); value["selected"] = {"record_offset": 0, "record_limit": 4}
    assert validate_seismic_window_payload(value, options={"record_limit": 4, "record_offset": 0}) is value
    with pytest.raises(SeismicWindowError): validate_seismic_window_payload(value, options={"record_offset": 0, "record_limit": 3})
