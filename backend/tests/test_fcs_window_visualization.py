"""Pure backend response validation: no FCS/NumPy/native reader dependency."""
import copy
import pytest
from app.application.services.fcs_window_visualization import (
    FcsWindowError, LIMITS, SEMANTICS, READ_SEMANTICS, WARNINGS,
    validate_fcs_window_options, validate_fcs_window_payload,
)


def fixture(kind="series"):
    channels = [{"id": i, "name": name, "stain": None, "bits": 32, "range": 4294967296, "exponent": [0, 0], "gain": None, "calibration": None, "display": None} for i, name in enumerate(["FSC-A", "CD3-A"])]
    m = {"format": "fcs", "fcs_version": "3.1", "datatype": "I", "byte_order": "big", "input_mode": "window", "source_bytes": 432,
        "read_bytes": 416 if kind == "tree" else 432, "read_requests": 2 if kind == "tree" else 3, "text_bytes": 358, "total_events": 2, "channel_count": 2, "event_bytes": 8,
        "scanned_event_bytes": 0 if kind == "tree" else 16, "decoded_values": 0 if kind == "tree" else 4, "decoded_bytes": 0 if kind == "tree" else 16,
        "nonfinite_values": 0, "plottable_events": 0 if kind == "tree" else 2, "timestep": None, "compensation": {"declarations": [], "spillover": None},
        "value_semantics": SEMANTICS, "read_semantics": READ_SEMANTICS, "limits": dict(LIMITS)}
    r = {"contract_version": 2, "type": "fcs-window", "reader": "fcs-window", "kind": kind, "media_type": "application/json", "choices": {"channels": channels},
        "selected": {} if kind == "tree" else {"view": "scatter", "channels": [0, 1], "event_offset": 0, "event_count": 2}, "metadata": m, "warnings": list(WARNINGS), "sampled": False}
    if kind == "tree": r["tree"] = [{"path": "/channel-"+str(c["id"]), "node_type": "channel", "attributes": {"name": c["name"], "bits": 32}} for c in channels]
    else: r["series"] = [{"channel": c["id"], "label": c["name"], "x": [0, 1], "y": [4294967295, 0]} for c in channels]
    return r


@pytest.mark.parametrize("kind", ["tree", "series"])
def test_valid_host_bound_response(kind):
    r = fixture(kind); m = r["metadata"]
    assert validate_fcs_window_payload(r, kind=kind, options=r["selected"], fmt="fcs", source_bytes=m["source_bytes"], read_bytes=m["read_bytes"], read_requests=m["read_requests"]) is r


@pytest.mark.parametrize("kw", [{"kind": "tree"}, {"fmt": "nc"}, {"source_bytes": 431}, {"read_bytes": 1}, {"read_requests": 1}, {"source_bytes": True}, {"options": {"view": "scatter", "channels": [1, 0], "event_offset": 0, "event_count": 2}}])
def test_host_request_and_actual_source_counters_bound(kw):
    with pytest.raises(FcsWindowError): validate_fcs_window_payload(fixture(), **kw)


@pytest.mark.parametrize("path,new", [
    (("contract_version",), True), (("kind",), []), (("reader",), "edf"), (("sampled",), True), (("warnings",), ["source-path"]),
    (("metadata", "format"), "csv"), (("metadata", "fcs_version"), "3.2"), (("metadata", "datatype"), "A"), (("metadata", "byte_order"), "native"),
    (("metadata", "source_bytes"), 8589934593), (("metadata", "read_bytes"), 8388609), (("metadata", "read_requests"), 129), (("metadata", "text_bytes"), 262145),
    (("metadata", "total_events"), True), (("metadata", "total_events"), 3), (("metadata", "channel_count"), 129), (("metadata", "event_bytes"), 4),
    (("metadata", "scanned_event_bytes"), 8), (("metadata", "decoded_values"), 16385), (("metadata", "decoded_bytes"), 131073),
    (("metadata", "nonfinite_values"), 1), (("metadata", "plottable_events"), 1), (("metadata", "timestep"), 0), (("metadata", "value_semantics"), "calibrated"),
    (("metadata", "limits", "max_events"), 8193), (("metadata", "compensation", "declarations"), ["$SPILLOVER"]),
    (("choices", "channels", 0, "id"), True), (("choices", "channels", 0, "name"), "<script>"), (("choices", "channels", 0, "name"), "/Users/private"),
    (("choices", "channels", 0, "name"), "CD3-A"), (("choices", "channels", 0, "name"), "A\u202eB"), (("choices", "channels", 0, "range"), 1024),
    (("choices", "channels", 0, "bits"), 64), (("choices", "channels", 0, "gain"), -1), (("choices", "channels", 0, "exponent"), [4, 0]),
    (("choices", "channels", 0, "calibration"), {"factor": 2, "unit": "<img>"}), (("choices", "channels", 0, "display"), {"scale": "Logicle", "values": [1, 2]}),
    (("series", 0, "channel"), True), (("series", 0, "x", 0), .5), (("series", 0, "y", 0), 4294967296), (("series", 0, "y", 0), -1),
    (("series", 0, "y", 0), None), (("series", 0, "y", 0), True), (("series", 0, "y", 0), float("nan")),
])
def test_strict_fields_types_and_scientific_semantics(path, new):
    r = fixture(); target = r
    for key in path[:-1]: target = target[key]
    target[path[-1]] = new
    with pytest.raises(FcsWindowError): validate_fcs_window_payload(r)


@pytest.mark.parametrize("where", ["root", "metadata", "choices", "channel", "trace", "tree"])
def test_no_extra_fields_or_paths(where):
    r = fixture("tree" if where == "tree" else "series")
    target = {"root": r, "metadata": r["metadata"], "choices": r["choices"], "channel": r["choices"]["channels"][0], "trace": r.get("series", [{}])[0], "tree": r.get("tree", [{}])[0]}[where]
    target["url"] = "https://unsafe"
    with pytest.raises(FcsWindowError): validate_fcs_window_payload(r)


@pytest.mark.parametrize("opts", [{}, {"view": "scatter", "channels": [0, 0], "event_offset": 0, "event_count": 1},
    {"view": "scatter", "channels": [0, 1], "event_offset": 0, "event_count": 8193}, {"view": "scatter", "channels": [0, 1], "event_offset": 0, "event_count": 1, "bins": 4},
    {"view": "histogram", "channels": [0], "event_offset": 0, "event_count": 1}, {"view": "histogram", "channels": [0], "event_offset": 0, "event_count": 1, "bins": 129},
    {"view": "histogram", "channels": [0], "event_offset": 0, "event_count": 1, "bins": True}])
def test_options_rejected(opts):
    with pytest.raises(FcsWindowError): validate_fcs_window_options("series", opts)


def test_histogram_bins_bound_and_order_independent_request():
    r = fixture(); r["selected"] = {"view": "histogram", "channels": [0], "event_offset": 0, "event_count": 2, "bins": 4}; r["series"] = r["series"][:1]
    r["metadata"]["decoded_values"] = 2; r["metadata"]["decoded_bytes"] = 8
    assert validate_fcs_window_payload(r, options={"bins": 4, "channels": [0], "view": "histogram", "event_count": 2, "event_offset": 0}) is r
    with pytest.raises(FcsWindowError): validate_fcs_window_payload(r, options={**r["selected"], "bins": 3})
