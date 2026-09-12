import copy
import io
import json
import importlib.util
import os
import struct
from pathlib import Path

import pytest

from app.services.pointcloud_window_payload import MAX_SOURCE, PointCloudWindowError, validate_pointcloud_window_payload
from app.services.pointcloud_window_reader import pointcloud_window_preview
from pointcloud_window_fixtures import SIZES, browser_payloads, las_bytes


def oracle():
    if os.getenv("AI_DATASEEK_REQUIRE_POINTCLOUD_REFERENCE") == "1" and importlib.util.find_spec("laspy") is None:
        pytest.fail("Test-only laspy reference is required")
    return pytest.importorskip("laspy")


def preview(data, kind="geometry", options=None, **kwargs):
    reads = []
    def read(offset, length):
        reads.append((offset, length))
        return data[offset:offset + length]
    result = pointcloud_window_preview(read, len(data), "las", kind, options or ({} if kind == "tree" else {"point_offset": 8, "point_count": 4}), **kwargs)
    return result, reads


@pytest.mark.parametrize("version,pf", [("1.2", p) for p in range(4)] + [("1.4", p) for p in SIZES])
def test_fixed_record_offsets_scaling_attributes_and_official_laspy_oracle(version, pf):
    laspy = oracle()
    raw = las_bytes(pf, version)
    result, reads = preview(raw)
    expected = laspy.read(io.BytesIO(raw))
    array, attrs, m = result["array"], result["point_attributes"], result["metadata"]
    assert array["values"] == [int(v) for i in range(8, 12) for v in (expected.X[i], expected.Y[i], expected.Z[i])]
    assert attrs["intensity"] == expected.intensity[8:12].tolist()
    assert attrs["classification"] == [int(v) for v in expected.classification[8:12]]
    expected_flags = [int(expected.synthetic[i]) | int(expected.key_point[i]) << 1 | int(expected.withheld[i]) << 2
                      | (int(expected.overlap[i]) << 3 if pf >= 6 else 0) for i in range(8, 12)]
    assert attrs["classification_flags"] == expected_flags
    if pf in (2, 3, 7, 8): assert attrs["rgb"] == [int(v) for i in range(8, 12) for v in (expected.red[i], expected.green[i], expected.blue[i])]
    else: assert attrs["rgb"] is None
    assert m["scales"] == expected.header.scales.tolist() and m["offsets"] == expected.header.offsets.tolist()
    assert m["window_bounds"] == [[float(min(axis[8:12])), float(max(axis[8:12]))] for axis in (expected.x, expected.y, expected.z)]
    assert reads[-1] == (m["point_data_offset"] + 8 * SIZES[pf], 4 * SIZES[pf])
    assert m["read_bytes"] == sum(length for _, length in reads) and m["read_requests"] == len(reads)


@pytest.mark.parametrize("pf", [0, 1, 2, 3, 6, 7, 8])
def test_laspy_generated_file_is_independent_oracle(pf):
    laspy = oracle()
    header = laspy.LasHeader(point_format=pf, version="1.4" if pf >= 6 else "1.2")
    header.scales = [.005, .01, .02]; header.offsets = [1e9, -1e9, 400]
    las = laspy.LasData(header)
    las.X = [-2**31, 0, 2**31 - 1]; las.Y = [1, 2, 3]; las.Z = [-5, 10, 25]
    las.intensity = [0, 65535, 100]; las.classification = [1, 2, 3]; las.withheld = [0, 1, 0]
    if pf in (2, 3, 7, 8): las.red = [0, 65535, 128]; las.green = [10, 20, 30]; las.blue = [40, 50, 60]
    stream = io.BytesIO(); las.write(stream, do_compress=False)
    value, _ = preview(stream.getvalue(), options={"point_offset": 0, "point_count": 3})
    assert value["array"]["values"] == [-2**31, 1, -5, 0, 2, 10, 2**31 - 1, 3, 25]
    assert value["point_attributes"]["classification_flags"] == [0, 4, 0]


def test_tree_reads_only_fixed_headers_no_point_or_vlr_evlr_payload():
    raw = las_bytes(vlrs=[("LASF_Projection", 2112, b"file:///private/do-not-read"), ("UNKNOWN", 1, b"ignored")],
                    evlrs=[("LASF_Projection", 34735, b"ignored-geotiff-body")])
    result, reads = preview(raw, "tree")
    assert [n for _, n in reads] == [227, 148, 54, 54, 60]
    assert result["metadata"]["crs_declarations"] == ["geotiff", "wkt"]
    assert result["metadata"]["point_bytes"] == 0 and not result["sampled"]
    assert "private" not in json.dumps(result) and "file:" not in json.dumps(result)
    assert result["metadata"]["units"] == "unknown"


@pytest.mark.parametrize("offset,value,width", [(24, 2, 1), (25, 3, 1), (25, 5, 1), (104, 128, 1),
    (104, 70, 1), (104, 4, 1), (104, 9, 1), (105, 29, 2), (94, 374, 2), (96, 100, 4),
    (247, 2**64 - 1, 8), (100, 97, 4), (243, 97, 4), (227, 1, 8), (235, 1, 8),
    (6, 2, 2), (6, 32, 2), (107, 1, 4)])
def test_malformed_or_unsupported_headers_reject_before_points(offset, value, width):
    raw = bytearray(las_bytes()); raw[offset:offset + width] = value.to_bytes(width, "little")
    reads = []
    def read(o, n): reads.append((o, n)); return bytes(raw[o:o + n])
    with pytest.raises(PointCloudWindowError): pointcloud_window_preview(read, len(raw), "las")
    assert all(o + n <= 375 for o, n in reads)


@pytest.mark.parametrize("offset,value", [(131, 0.), (131, -1.), (131, float("nan")), (139, float("inf")),
                                         (155, float("nan")), (179, float("inf")), (187, 1e99)])
def test_invalid_scale_offset_or_declared_bounds(offset, value):
    raw = bytearray(las_bytes()); struct.pack_into("<d", raw, offset, value)
    with pytest.raises(PointCloudWindowError): preview(bytes(raw), "tree")


@pytest.mark.parametrize("user,extended", [("laszip encoded", False), ("copc", False), ("copc", True)])
def test_compressed_or_copc_metadata_refuses_even_with_las_extension(user, extended):
    raw = las_bytes(**{("evlrs" if extended else "vlrs"): [(user, 1, b"fake")]})
    with pytest.raises(PointCloudWindowError, match="LAZ/COPC"): preview(raw, "tree")


def test_vlr_and_evlr_extents_cannot_overlap_points_or_overflow_source():
    raw = bytearray(las_bytes(vlrs=[("unknown", 1, b"x")]))
    struct.pack_into("<H", raw, 375 + 20, 65535)
    with pytest.raises(PointCloudWindowError): preview(bytes(raw), "tree")
    raw = bytearray(las_bytes(evlrs=[("unknown", 1, b"x")]))
    pos = int.from_bytes(raw[235:243], "little"); struct.pack_into("<Q", raw, pos + 20, 2**64 - 1)
    with pytest.raises(PointCloudWindowError): preview(bytes(raw), "tree")


def test_sparse_eight_gib_source_never_fetches_whole_file_or_unselected_points():
    raw = bytearray(las_bytes(count=1)); stride = SIZES[7]
    count = (MAX_SOURCE - 375) // stride
    struct.pack_into("<Q", raw, 247, count)
    last = count - 1
    reads = []
    def read(offset, length):
        reads.append((offset, length))
        if offset < 375: return bytes(raw[offset:offset + length])
        assert offset == 375 + last * stride and length == stride
        return bytes(raw[375:])
    result = pointcloud_window_preview(read, MAX_SOURCE, "las", "geometry", {"point_offset": last, "point_count": 1})
    assert result["metadata"]["source_bytes"] == MAX_SOURCE and result["metadata"]["total_points"] == count
    assert sum(n for _, n in reads) == 375 + stride and len(reads) == 3 and result["sampled"]


def test_extra_dimensions_are_stride_only_and_can_exceed_request_budget():
    raw = las_bytes(extra_bytes=100)
    value, reads = preview(raw)
    assert value["metadata"]["extra_bytes_per_point"] == 100 and reads[-1][1] == 4 * 136
    raw = bytearray(las_bytes(count=1, extra_bytes=65000))
    count = 200; size = 375 + count * 65036
    struct.pack_into("<Q", raw, 247, count)
    reads = []
    def read(o, n): reads.append((o, n)); return bytes(raw[o:o+n])
    with pytest.raises(PointCloudWindowError, match="超预算"):
        pointcloud_window_preview(read, size, "las", "geometry", {"point_offset": 0, "point_count": count})
    assert reads == [(0, 227), (227, 148)]


@pytest.mark.parametrize("kind,options", [("geometry", {}), ("geometry", {"point_offset": True, "point_count": 1}),
    ("geometry", {"point_offset": 0, "point_count": 16385}), ("geometry", {"point_offset": 0, "point_count": 0}),
    ("geometry", {"point_offset": -1, "point_count": 1}), ("tree", {"point_offset": 0}), ("series", {})])
def test_invalid_options_reject_before_any_callback(kind, options):
    with pytest.raises(PointCloudWindowError): pointcloud_window_preview(lambda *_: pytest.fail("unexpected IO"), 1000, "las", kind, options)


@pytest.mark.parametrize("fmt", ["laz", "copc", "bin", "LAS"])
def test_unregistered_format_cannot_trigger_io(fmt):
    with pytest.raises(PointCloudWindowError): pointcloud_window_preview(lambda *_: pytest.fail("unexpected IO"), 1000, fmt)


def test_budget_reduction_exact_range_and_callback_cancel_fail_closed():
    raw = las_bytes()
    with pytest.raises(PointCloudWindowError): preview(raw, limits={"max_read_bytes": 1048576, "max_total_bytes": 226, "max_reads": 128})
    with pytest.raises(PointCloudWindowError): pointcloud_window_preview(lambda *_: b"", len(raw), "las")
    class Cancelled(Exception): pass
    def cancelled(*_): raise Cancelled()
    with pytest.raises(Cancelled): pointcloud_window_preview(cancelled, len(raw), "las")


def test_maximum_window_is_complete_not_globally_sampled():
    value, _ = preview(las_bytes(count=16384), options={"point_offset": 0, "point_count": 16384})
    assert value["array"]["shape"] == [16384, 3] and len(value["array"]["values"]) == 49152
    assert value["sampled"] is False and len(json.dumps(value).encode()) < 2 * 1024**2


def test_large_coordinate_offset_keeps_int32_resolution_for_local_origin():
    value = browser_payloads()["precision_geometry"]
    assert value["array"]["values"][3] - value["array"]["values"][0] == 1
    assert value["metadata"]["scales"] == [.001, .002, .003]


def test_private_contract_binds_actual_source_and_broker_counts():
    value, _ = preview(las_bytes())
    for field in ("source_bytes", "read_bytes", "read_requests"):
        with pytest.raises(PointCloudWindowError): validate_pointcloud_window_payload(value, **{field: value["metadata"][field] + 1})
    with pytest.raises(PointCloudWindowError): validate_pointcloud_window_payload(value, options={"point_offset": 0, "point_count": 4})
    with pytest.raises(PointCloudWindowError): validate_pointcloud_window_payload(value, kind="tree")


def test_backend_validator_is_identical_and_browser_fixtures_are_actual_outputs():
    root = Path(__file__).resolve().parents[2]
    if (root / "backend").exists():
        assert (root / "sandbox/app/services/pointcloud_window_payload.py").read_bytes() == (root / "backend/app/application/services/pointcloud_window_visualization.py").read_bytes()
        fixture = root / "frontend/tests/browser/pointcloud-window-data.json"
        if fixture.exists(): assert json.loads(fixture.read_text()) == browser_payloads()
