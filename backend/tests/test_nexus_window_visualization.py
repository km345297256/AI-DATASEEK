"""No HDF5 dependency: strict host validation of actual isolated-reader output."""
import copy
import json
from pathlib import Path

import pytest

from app.application.services.nexus_window_visualization import (
    NexusWindowError, validate_nexus_window_options, validate_nexus_window_payload,
)

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/browser/nexus-window-data.json"


@pytest.fixture
def data():
    return json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("fmt", ["nxs", "nx", "h5", "hdf5", "hdf"])
def test_every_registered_extension_has_matching_payload_validation(data, fmt):
    data["tree"]["metadata"]["format"] = fmt
    assert validate_nexus_window_payload(data["tree"], fmt=fmt) is data["tree"]


@pytest.mark.parametrize("kind", ["tree", "series", "image"])
def test_actual_h5py_payload_with_full_host_request_bindings(data, kind):
    result = data[kind]
    assert validate_nexus_window_payload(result, kind=kind, options=result["selected"], fmt="nxs",
        source_bytes=result["metadata"]["source_bytes"], read_bytes=result["metadata"]["read_bytes"],
        read_requests=result["metadata"]["read_requests"]) is result


@pytest.mark.parametrize("field,value", [("kind", "series"), ("fmt", "h5"), ("source_bytes", 1), ("read_bytes", 1), ("read_requests", 1), ("source_bytes", True)])
def test_valid_payload_cannot_be_rebound_to_wrong_host_request(data, field, value):
    with pytest.raises(NexusWindowError): validate_nexus_window_payload(data["image"], **{field: value})


def test_valid_payload_does_not_match_other_selection(data):
    selection = copy.deepcopy(data["image"]["selected"])
    selection["selection"][0]["start"] = 0
    with pytest.raises(NexusWindowError): validate_nexus_window_payload(data["image"], options=selection)


MUTATIONS = {
    "contract-bool": lambda r: r.__setitem__("contract_version", True),
    "unknown-root": lambda r: r.__setitem__("url", "https://invalid.example"),
    "wrong-reader": lambda r: r.__setitem__("reader", "array-window"),
    "wrong-kind": lambda r: r.__setitem__("kind", "map"),
    "kind-array": lambda r: r.__setitem__("kind", []),
    "wrong-type": lambda r: r.__setitem__("type", "series"),
    "media-html": lambda r: r.__setitem__("media_type", "text/html"),
    "warning-url": lambda r: r.__setitem__("warnings", ["https://invalid.example"]),
    "sampling-true": lambda r: r.__setitem__("sampled", True),
    "choices-extra": lambda r: r["choices"].__setitem__("unknown", []),
    "duplicate-id": lambda r: r["choices"]["signals"].append(copy.deepcopy(r["choices"]["signals"][0])),
    "path-id": lambda r: r["choices"]["signals"][0].__setitem__("id", "/private/secret"),
    "html-label": lambda r: r["choices"]["signals"][0].__setitem__("label", "<script>"),
    "hostpath-unit": lambda r: r["choices"]["signals"][0].__setitem__("unit", "/Users/private/file"),
    "raw-header": lambda r: r["choices"]["signals"][0].__setitem__("raw_header", {}),
    "dtype-object": lambda r: r["choices"]["signals"][0].__setitem__("dtype", "|O"),
    "dtype-float16": lambda r: r["choices"]["signals"][0].__setitem__("dtype", "<f2"),
    "dtype-bool": lambda r: r["choices"]["signals"][0].__setitem__("dtype", "|b1"),
    "dtype-noncanonical": lambda r: r["choices"]["signals"][0].__setitem__("dtype", "=i2"),
    "shape-bool": lambda r: r["choices"]["signals"][0]["shape"].__setitem__(0, True),
    "shape-empty": lambda r: r["choices"]["signals"][0].__setitem__("shape", []),
    "shape-rank3": lambda r: r["choices"]["signals"][0]["shape"].append(1),
    "huge-chunk": lambda r: r["choices"]["signals"][0].__setitem__("chunks", [10000, 10000]),
    "axis-source-url": lambda r: r["choices"]["signals"][0]["axes"][0].__setitem__("source", "https://invalid.example"),
    "axis-source-array": lambda r: r["choices"]["signals"][0]["axes"][0].__setitem__("source", []),
    "axis-index-impersonation": lambda r: r["choices"]["signals"][0]["axes"][0].__setitem__("source", "index"),
    "axis-extra": lambda r: r["choices"]["signals"][0]["axes"][0].__setitem__("url", "x"),
    "axis-absent": lambda r: r["choices"]["signals"][0]["axes"].pop(),
    "errors-shape": lambda r: r["choices"]["signals"][0]["errors"].__setitem__("chunks", [4]),
    "selection-extra": lambda r: r["selected"].__setitem__("decode", "auto"),
    "selection-bool": lambda r: r["selected"]["selection"][0].__setitem__("step", True),
    "selection-other-id": lambda r: r["selected"].__setitem__("nxdata", "n-" + "0" * 32),
    "selection-int": lambda r: r["selected"]["selection"].__setitem__(0, 1),
    "selection-oob": lambda r: r["selected"]["selection"][0].__setitem__("stop", 5),
    "source-too-large": lambda r: r["metadata"].__setitem__("source_bytes", 8 * 1024**3 + 1),
    "read-too-large": lambda r: r["metadata"].__setitem__("read_bytes", 8 * 1024**2 + 1),
    "read-count-too-large": lambda r: r["metadata"].__setitem__("read_requests", 129),
    "attribute-too-large": lambda r: r["metadata"].__setitem__("attribute_bytes", 65537),
    "decode-too-large": lambda r: r["metadata"].__setitem__("decoded_chunk_bytes", 16 * 1024**2 + 1),
    "decode-wrong": lambda r: r["metadata"].__setitem__("decoded_chunk_bytes", 319),
    "chunks-wrong": lambda r: r["metadata"].__setitem__("chunks_touched", 3),
    "output-wrong": lambda r: r["metadata"].__setitem__("output_values", 6),
    "missing-wrong": lambda r: r["metadata"].__setitem__("nonfinite_values", 1),
    "semantic-calibrated": lambda r: r["metadata"].__setitem__("value_semantics", "calibrated"),
    "metadata-path": lambda r: r["metadata"].__setitem__("path", "/private/secret"),
    "array-shape": lambda r: r["array"].__setitem__("shape", [3, 2]),
    "array-label": lambda r: r["array"]["dimensions"].__setitem__(0, "wrong"),
    "array-missing": lambda r: r["array"]["values"].pop(),
    "array-bool": lambda r: r["array"]["values"].__setitem__(0, True),
    "array-fraction": lambda r: r["array"]["values"].__setitem__(0, .5),
    "array-null-integer": lambda r: r["array"]["values"].__setitem__(0, None),
    "array-overflow": lambda r: r["array"]["values"].__setitem__(0, 65536),
    "array-unsafeint": lambda r: r["array"]["values"].__setitem__(0, 2**53),
    "axis-dimension-bool": lambda r: r["axes"][0].__setitem__("dimension", False),
    "axis-indices-bool": lambda r: r["axes"][0]["indices"].__setitem__(0, True),
    "axis-wrong-source": lambda r: r["axes"][0].__setitem__("source", "index"),
    "axis-wrong-unit": lambda r: r["axes"][0].__setitem__("unit", "meter"),
    "axis-nan": lambda r: r["axes"][0]["values"].__setitem__(0, float("nan")),
    "axis-duplicate": lambda r: r["axes"][0]["values"].__setitem__(0, 101),
    "errors-negative": lambda r: r["errors"].__setitem__(0, -1),
    "errors-extra": lambda r: r["errors"].append(1),
    "errors-omitted": lambda r: r.__setitem__("errors", None),
}


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_unknown_or_malicious_payload_fails_closed(data, name):
    result = data["image"]
    MUTATIONS[name](result)
    with pytest.raises(NexusWindowError): validate_nexus_window_payload(result)


@pytest.mark.parametrize("mutate", [
    lambda r: r["tree"][0]["attributes"].__setitem__("script", "alert()"),
    lambda r: r["tree"][0].__setitem__("path", "/entry/secret"),
    lambda r: r.__setitem__("array", {}),
    lambda r: r["metadata"].__setitem__("chunks_touched", 1),
    lambda r: r["selected"].__setitem__("nxdata", "n-" + "1" * 32),
])
def test_tree_does_not_conceal_pixels_or_arbitrary_attributes(data, mutate):
    result = data["tree"]
    mutate(result)
    with pytest.raises(NexusWindowError): validate_nexus_window_payload(result)


@pytest.mark.parametrize("kind,options", [("tree", {"path": "/entry"}), ("series", {}), ("image", {}), ([], {}),
    ("series", {"nxdata": "n-" + "0" * 32, "selection": [{"start": 0, "stop": 20000, "step": 1}]})])
def test_options_before_dispatch_are_strict(kind, options):
    with pytest.raises(NexusWindowError): validate_nexus_window_options(kind, options)


def test_selection_is_copied(data):
    raw = data["image"]["selected"]
    parsed = validate_nexus_window_options("image", raw)
    raw["selection"][0]["stop"] = 999
    assert parsed["selection"][0]["stop"] == 3
