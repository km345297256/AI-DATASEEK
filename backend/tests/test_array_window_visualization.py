"""Strict public-boundary tests without importing HDF5 or touching source data."""
import copy

import pytest

from app.application.services.array_window_visualization import (
    ArrayWindowError, VALUE_SEMANTICS, WARNING, validate_array_window_options,
    validate_array_window_payload,
)

ID = "v-" + "a" * 32


def fixture(kind="series"):
    selected = {"variable": ID, "selection": [{"start": 0, "stop": 4, "step": 1}], "decode": "raw"}
    value = {"contract_version": 2, "type": "array-window", "reader": "array-window", "kind": kind, "media_type": "application/json",
        "choices": {"variables": [{"id": ID, "label": "signal", "shape": [4], "dtype": "<f8", "chunks": [2], "selectable": True, "reason": ""}]},
        "selected": selected if kind == "series" else {}, "warnings": [WARNING], "sampled": False,
        "metadata": {"format": "h5", "container": "HDF5", "input_mode": "window", "value_semantics": VALUE_SEMANTICS,
            "source_bytes": 1024**3, "read_bytes": 16384, "read_requests": 1, "chunks_touched": 2 if kind == "series" else 0,
            "decoded_chunk_bytes": 32 if kind == "series" else 0, "catalog_truncated": False, "attributes": {}, "nonfinite_values": 1 if kind == "series" else 0,
            "coordinates": "zero-based dimension indices; not geospatial coordinates", "limits": {"max_elements": 16384, "max_chunk_bytes": 4194304, "max_decoded_bytes": 16777216, "max_nodes": 128}}}
    if kind == "series":
        value["array"] = {"shape": [4], "dimensions": ["index_0"], "values": [1.0000000000000002, None, -9999, 4]}
        value["axes"] = [{"dimension": 0, "indices": [0, 1, 2, 3]}]
    else:
        value["tree"] = [{"path": "/" + ID, "node_type": "array", "attributes": {"label": "signal", "depth": 0, "reason": ""}}]
    return value


@pytest.mark.parametrize("kind", ["tree", "series"])
def test_valid_bound_response(kind):
    value = fixture(kind)
    assert validate_array_window_payload(value, kind=kind, options=value["selected"], fmt="h5", source_bytes=1024**3, read_bytes=16384, read_requests=1) is value


@pytest.mark.parametrize("bindings", [{"kind": "image"}, {"fmt": "nc"}, {"source_bytes": 1024}, {"read_bytes": 4}, {"read_requests": 2},
    {"options": {"variable": ID, "selection": [{"start": 1, "stop": 4, "step": 1}], "decode": "raw"}}])
def test_valid_but_unbound_results_rejected(bindings):
    with pytest.raises(ArrayWindowError):
        validate_array_window_payload(fixture(), **bindings)


@pytest.mark.parametrize("path,value", [
    (("kind",), []), (("contract_version",), True), (("reader",), "h5web"), (("sampled",), True), (("warnings",), ["/private/leak"]),
    (("metadata", "format"), []), (("metadata", "read_bytes"), 8388609), (("metadata", "read_requests"), 129),
    (("metadata", "source_bytes"), True), (("metadata", "decoded_chunk_bytes"), 16777217), (("metadata", "chunks_touched"), 129),
    (("metadata", "attributes"), {"units": "/Users/secret"}), (("metadata", "attributes"), {"scale_factor": 10**400}),
    (("metadata", "attributes"), {"url": "bad"}), (("metadata", "nonfinite_values"), 0), (("metadata", "catalog_truncated"), 1),
    (("choices", "variables", 0, "id"), "/some/path"), (("choices", "variables", 0, "dtype"), "object"),
    (("choices", "variables", 0, "chunks"), [600000]), (("choices", "variables", 0, "shape"), [0]), (("choices", "variables", 0, "label"), "<svg>"),
    (("array", "values", 0), True), (("array", "values", 0), float("inf")), (("array", "values", 0), 2**53),
    (("array", "shape"), [True]), (("array", "dimensions"), ["time"]), (("axes", 0, "indices", 0), True),
    (("selected", "decode"), "cf"), (("selected", "selection", 0, "step"), 0), (("selected", "selection", 0, "stop"), 5),
])
def test_strict_response_types_fields_and_budgets(path, value):
    result = fixture()
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ArrayWindowError):
        validate_array_window_payload(result)


@pytest.mark.parametrize("mutate", [lambda r: r.__setitem__("filename", "/private"), lambda r: r["tree"][0].__setitem__("node_type", []),
    lambda r: r["tree"].append(copy.deepcopy(r["tree"][0])), lambda r: r["tree"].clear(),
    lambda r: r["tree"][0]["attributes"].__setitem__("label", "other"), lambda r: r["metadata"]["attributes"].__setitem__("units", "K")])
def test_tree_whitelist_and_choice_binding(mutate):
    result = fixture("tree"); mutate(result)
    with pytest.raises(ArrayWindowError):
        validate_array_window_payload(result)


@pytest.mark.parametrize("kind,options", [(None, {}), ([], {}), ("tree", {"slice": True}), ("series", {}), ("image", {}),
    ("series", {"variable": ID, "selection": [True], "decode": "raw"}),
    ("series", {"variable": ID, "selection": [{"start": 0, "stop": 16385, "step": 1}], "decode": "raw"})])
def test_invalid_options(kind, options):
    with pytest.raises(ArrayWindowError):
        validate_array_window_options(kind, options)
