"""Actual ecCodes response validated without native dependencies on the host."""
import copy
import json
from pathlib import Path

import pytest

from app.application.services.grib_window_visualization import GribWindowError, validate_grib_window_options, validate_grib_window_payload

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/browser/grib-window-data.json"


@pytest.fixture
def data(): return json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("kind", ["tree", "image"])
def test_actual_native_payload_matches_all_host_bindings(data, kind):
    value = data[kind]
    assert validate_grib_window_payload(value, kind=kind, options=value["selected"], fmt="grib2", source_bytes=value["metadata"]["source_bytes"], read_bytes=value["metadata"]["read_bytes"], read_requests=value["metadata"]["read_requests"]) is value


@pytest.mark.parametrize("fmt", ["grib", "grb", "grib2", "grb2"])
def test_all_registered_extensions(fmt, data):
    data["tree"]["metadata"]["format"] = fmt
    assert validate_grib_window_payload(data["tree"], fmt=fmt) is data["tree"]


MUTATIONS = {
    "contract-bool": lambda v: v.__setitem__("contract_version", True), "extra-root": lambda v: v.__setitem__("url", "https://invalid"),
    "wrong-kind": lambda v: v.__setitem__("kind", "map"), "list-kind": lambda v: v.__setitem__("kind", []),
    "wrong-reader": lambda v: v.__setitem__("reader", "nexus-window"), "html": lambda v: v.__setitem__("media_type", "text/html"),
    "sampled": lambda v: v.__setitem__("sampled", True), "warning": lambda v: v.__setitem__("warnings", []),
    "choices-extra": lambda v: v["choices"].__setitem__("hostpath", "/private"), "duplicate": lambda v: v["choices"]["messages"].append(copy.deepcopy(v["choices"]["messages"][0])),
    "id-path": lambda v: v["choices"]["messages"][0].__setitem__("id", "/private/source"), "id-overflow": lambda v: v["choices"]["messages"][0].__setitem__("id", "g-ffffffffffffffff"),
    "shape-bomb": lambda v: v["choices"]["messages"][0].__setitem__("shape", [16384, 16384]), "shape-bool": lambda v: v["choices"]["messages"][0].__setitem__("shape", [True, 6]),
    "negative-step": lambda v: v["choices"]["messages"][0].__setitem__("step", [-1, -1]), "zero-step": lambda v: v["choices"]["messages"][0].__setitem__("step", [0, -1]),
    "wrap-span": lambda v: v["choices"]["messages"][0].__setitem__("step", [100, -1]), "latitude": lambda v: v["choices"]["messages"][0].__setitem__("first", [10, 99]),
    "longitude": lambda v: v["choices"]["messages"][0].__setitem__("first", [360, 50]), "alternate": lambda v: v["choices"]["messages"][0].__setitem__("scanning_mode", 16),
    "unknown-earth": lambda v: v["choices"]["messages"][0].__setitem__("earth_shape", 255), "bad-date": lambda v: v["choices"]["messages"][0].__setitem__("reference_time", "2026-02-30T00:00:00Z"),
    "label-html": lambda v: v["choices"]["messages"][0].__setitem__("label", "<img>"), "unit-path": lambda v: v["choices"]["messages"][0].__setitem__("unit", "/Users/private"),
    "unit-long": lambda v: v["choices"]["messages"][0].__setitem__("unit", "x" * 129), "surface-bool": lambda v: v["choices"]["messages"][0].__setitem__("surface_scale", True),
    "bitmap-string": lambda v: v["choices"]["messages"][0].__setitem__("bitmap", "true"), "missing-lie": lambda v: v["choices"]["messages"][0].__setitem__("missing_count", 0),
    "bits": lambda v: v["choices"]["messages"][0].__setitem__("packing_bits", 33), "bytes": lambda v: v["choices"]["messages"][0].__setitem__("byte_length", 2**20 + 1),
    "selection-extra": lambda v: v["selected"].__setitem__("path", "/private"), "selection-roi": lambda v: v["selected"].__setitem__("roi", [0, 0, 400, 400]),
    "array-shape": lambda v: v["array"].__setitem__("shape", [4, 3]), "array-dim": lambda v: v["array"].__setitem__("dimensions", ["x", "y"]),
    "array-length": lambda v: v["array"]["values"].pop(), "array-nan": lambda v: v["array"]["values"].__setitem__(0, float("nan")),
    "array-bool": lambda v: v["array"]["values"].__setitem__(0, True), "array-string": lambda v: v["array"]["values"].__setitem__(0, "1"),
    "axes-swapped": lambda v: v["axes"].reverse(), "axis-units": lambda v: v["axes"][0].__setitem__("unit", "radians"),
    "axis-values": lambda v: v["axes"][0]["values"].__setitem__(0, 51), "axis-bool": lambda v: v["axes"][0]["values"].__setitem__(0, True),
    "axis-sort": lambda v: v["axes"][0]["values"].sort(), "meta-extra": lambda v: v["metadata"].__setitem__("url", "https://invalid"),
    "meta-source": lambda v: v["metadata"].__setitem__("source_bytes", 8 * 1024**3 + 1), "meta-decoder": lambda v: v["metadata"].__setitem__("decoder", "unknown"),
    "meta-packing": lambda v: v["metadata"].__setitem__("packing", "grid_complex"), "meta-grid": lambda v: v["metadata"].__setitem__("grid_type", "rotated_ll"),
    "meta-format": lambda v: v["metadata"].__setitem__("format", "grib1"), "meta-edition-bool": lambda v: v["metadata"].__setitem__("edition", True),
    "meta-reads": lambda v: v["metadata"].__setitem__("read_requests", 129), "meta-readbytes": lambda v: v["metadata"].__setitem__("read_bytes", 8388609),
    "meta-count": lambda v: v["metadata"].__setitem__("scanned_messages", 9), "meta-decoded": lambda v: v["metadata"].__setitem__("decoded_points", 12),
    "meta-output": lambda v: v["metadata"].__setitem__("output_values", 13), "meta-missing": lambda v: v["metadata"].__setitem__("missing_values", 0),
    "meta-offset": lambda v: v["metadata"].__setitem__("page_offset", 1), "meta-next": lambda v: v["metadata"].__setitem__("next_offset", None),
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_unknown_malformed_or_scientifically_inconsistent_payload_rejected(data, name):
    value = data["image"]; MUTATIONS[name](value)
    with pytest.raises(GribWindowError): validate_grib_window_payload(value)


@pytest.mark.parametrize("binding,value", [("kind", "tree"), ("fmt", "grb"), ("source_bytes", 1), ("source_bytes", True), ("read_bytes", 1), ("read_requests", 1), ("options", {"message": "g-0000000000000000", "roi": [0, 0, 1, 1]})])
def test_valid_result_cannot_be_bound_to_other_request(data, binding, value):
    with pytest.raises(GribWindowError): validate_grib_window_payload(data["image"], **{binding: value})


def test_tree_default_offsets_and_request_copy_are_canonical(data):
    assert validate_grib_window_options("tree", {}) == {"offset": 0}
    assert validate_grib_window_payload(data["tree"], options={}) is data["tree"]
    options = data["image"]["selected"]
    value = validate_grib_window_options("image", options)
    options["roi"][0] = 0
    assert value["roi"][0] == 1


@pytest.mark.parametrize("kind,options", [("map", {}), ("tree", {"offset": True}), ("tree", {"url": "https://invalid"}), ("tree", {"offset": 2**33}), ("image", {"message": "g-0000000000000000", "roi": [0, 0, 16384, 2]}), ("image", {"message": "g-0000000000000000", "roi": [0, 0, True, 2]})])
def test_request_limits_precede_native_dispatch(kind, options):
    with pytest.raises(GribWindowError): validate_grib_window_options(kind, options)
