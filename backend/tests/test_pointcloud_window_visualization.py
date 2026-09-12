import copy
import json
from pathlib import Path

import pytest

from app.application.services.pointcloud_window_visualization import PointCloudWindowError, validate_pointcloud_window_options, validate_pointcloud_window_payload

PATH = Path(__file__).resolve().parents[2] / "frontend/tests/browser/pointcloud-window-data.json"


@pytest.fixture
def samples(): return json.loads(PATH.read_text())


@pytest.mark.parametrize("name", [f"{family}_{kind}" for family in ("modern", "legacy", "precision") for kind in ("tree", "geometry")])
def test_actual_reader_outputs_match_kind_selection_and_broker(samples, name):
    v = samples[name]
    assert validate_pointcloud_window_payload(v, kind=v["kind"], options=v["selected"], fmt="las",
        **{key: v["metadata"][key] for key in ("source_bytes", "read_bytes", "read_requests")}) is v


CHANGES = {
    "version_bool": lambda v: v.__setitem__("contract_version", True),
    "type": lambda v: v.__setitem__("type", "array-window"), "reader": lambda v: v.__setitem__("reader", "hdf5"),
    "unknown": lambda v: v.__setitem__("url", "https://invalid"), "kind": lambda v: v.__setitem__("kind", "series"),
    "media": lambda v: v.__setitem__("media_type", "text/html"), "sampled": lambda v: v.__setitem__("sampled", False),
    "warnings": lambda v: v.__setitem__("warnings", []), "selected": lambda v: v["selected"].__setitem__("point_offset", True),
    "count": lambda v: v["selected"].__setitem__("point_count", 16385), "overrun": lambda v: v["selected"].__setitem__("point_offset", 64),
    "metadata_path": lambda v: v["metadata"].__setitem__("path", "/private/data"),
    "source": lambda v: v["metadata"].__setitem__("source_bytes", 8 * 1024**3 + 1),
    "source_extent": lambda v: v["metadata"].__setitem__("total_points", 2**32),
    "readbytes": lambda v: v["metadata"].__setitem__("read_bytes", 375),
    "readrequests": lambda v: v["metadata"].__setitem__("read_requests", 1),
    "record": lambda v: v["metadata"].__setitem__("record_bytes", 20),
    "metadata_bytes": lambda v: v["metadata"].__setitem__("metadata_bytes", 0),
    "format": lambda v: v["metadata"].__setitem__("format", "laz"),
    "lasversion": lambda v: v["metadata"].__setitem__("las_version", "1.5"),
    "pointformat": lambda v: v["metadata"].__setitem__("point_format", 9),
    "extra": lambda v: v["metadata"].__setitem__("extra_bytes_per_point", 100),
    "vlrcount": lambda v: v["metadata"].__setitem__("vlr_count", 97),
    "scale": lambda v: v["metadata"]["scales"].__setitem__(0, 0),
    "offset_nan": lambda v: v["metadata"]["offsets"].__setitem__(0, float("nan")),
    "crs_guess": lambda v: v["metadata"].__setitem__("crs_declarations", ["EPSG:4326"]),
    "unit_guess": lambda v: v["metadata"].__setitem__("units", "metres"),
    "bounds": lambda v: v["metadata"]["window_bounds"][0].__setitem__(0, 0),
    "pointbytes": lambda v: v["metadata"].__setitem__("point_bytes", 0),
    "output": lambda v: v["metadata"].__setitem__("output_points", 31),
    "raw_float": lambda v: v["array"]["values"].__setitem__(0, 1.5),
    "raw_int32_overflow": lambda v: v["array"]["values"].__setitem__(0, 2**31),
    "raw_bool": lambda v: v["array"]["values"].__setitem__(0, True),
    "raw_null": lambda v: v["array"]["values"].__setitem__(0, None),
    "shape": lambda v: v["array"].__setitem__("shape", [16, 6]),
    "dimension": lambda v: v["array"].__setitem__("dimensions", ["latitude", "longitude", "height"]),
    "attributes_unknown": lambda v: v["point_attributes"].__setitem__("script", "alert(1)"),
    "intensity": lambda v: v["point_attributes"]["intensity"].__setitem__(0, 65536),
    "class": lambda v: v["point_attributes"]["classification"].__setitem__(0, 256),
    "flags": lambda v: v["point_attributes"]["classification_flags"].__setitem__(0, 16),
    "rgb": lambda v: v["point_attributes"]["rgb"].__setitem__(0, -1),
    "rgb_missing": lambda v: v["point_attributes"].__setitem__("rgb", None),
}


@pytest.mark.parametrize("name", CHANGES)
def test_malicious_or_scientifically_misleading_schema_rejected(samples, name):
    value = samples["modern_geometry"]; CHANGES[name](value)
    with pytest.raises(PointCloudWindowError): validate_pointcloud_window_payload(value)


@pytest.mark.parametrize("field,value", [("kind", "tree"), ("fmt", "laz"), ("source_bytes", 1), ("read_bytes", 1),
    ("read_requests", True), ("options", {"point_offset": 0, "point_count": 32})])
def test_well_formed_payload_bound_to_exact_request(samples, field, value):
    with pytest.raises(PointCloudWindowError): validate_pointcloud_window_payload(samples["modern_geometry"], **{field: value})


@pytest.mark.parametrize("field,value", [("classification", 32), ("classification_flags", 8)])
def test_legacy_classification_flags_are_not_modern_eight_bit_class(samples, field, value):
    result = samples["legacy_geometry"]; result["point_attributes"][field][0] = value
    with pytest.raises(PointCloudWindowError): validate_pointcloud_window_payload(result)


@pytest.mark.parametrize("options", [{}, {"point_count": 1}, {"point_offset": 0, "point_count": True},
    {"point_offset": 0, "point_count": 1, "path": "/private"}])
def test_bad_options_fail_closed(options):
    with pytest.raises(PointCloudWindowError): validate_pointcloud_window_options("geometry", options)


def test_catalog_has_no_decoded_points_or_false_sampled_flag(samples):
    for change in (lambda v: v.__setitem__("sampled", True), lambda v: v.__setitem__("array", {}),
                   lambda v: v["metadata"].__setitem__("window_bounds", [[0, 1]] * 3)):
        value = copy.deepcopy(samples["modern_tree"]); change(value)
        with pytest.raises(PointCloudWindowError): validate_pointcloud_window_payload(value)
