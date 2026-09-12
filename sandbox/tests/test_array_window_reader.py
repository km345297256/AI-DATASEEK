"""Synthetic-only adversarial checks for the read-range HDF5 adapter."""
import copy
import io
import json
import sys
import zlib
from pathlib import Path

import h5py
import numpy as np
import pytest

from app.services.array_window_reader import (
    ArrayWindowError, RangeFile, MAX_CHUNK_BYTES, MAX_ENCODED_CHUNK_BYTES, MAX_SOURCE_BYTES,
    array_window_preview, validate_array_window_options,
)


def fixture(build=None):
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as handle:
        if build:
            build(handle)
        else:
            handle.create_dataset("signal", data=np.arange(20, dtype=np.float64), chunks=(4,), compression="gzip")
            handle.create_dataset("image", data=np.arange(120, dtype=np.int16).reshape(10, 12), chunks=(2, 3), compression="gzip", shuffle=True, fletcher32=True)
    return buffer.getvalue()


def preview(data, kind="tree", options=None, fmt="h5", limits=None):
    calls = []
    def read(offset, length):
        calls.append((offset, length))
        assert 0 <= offset < len(data) and 0 < length <= 1024**2 and offset + length <= len(data)
        return data[offset:offset + length]
    result = array_window_preview(read, len(data), fmt, kind, options, limits)
    assert result["metadata"]["read_requests"] == len(calls)
    assert result["metadata"]["read_bytes"] == sum(length for _, length in calls)
    return result, calls


def selected(tree, label, selection):
    variable = next(v for v in tree["choices"]["variables"] if v["label"] == label)
    return {"variable": variable["id"], "selection": selection, "decode": "raw"}


def sl(start, stop, step=1):
    return {"start": start, "stop": stop, "step": step}


def test_tree_then_exact_series_and_image():
    data = fixture()
    tree, calls = preview(data)
    assert len(calls) <= 3
    assert "array" not in tree and tree["selected"] == {}
    options = selected(tree, "signal", [sl(3, 17, 3)])
    series, _ = preview(data, "series", options)
    assert series["array"]["values"] == [3, 6, 9, 12, 15]
    assert series["axes"] == [{"dimension": 0, "indices": [3, 6, 9, 12, 15]}]
    assert series["selected"] == options
    image, _ = preview(data, "image", selected(tree, "image", [sl(2, 5), sl(3, 8, 2)]))
    assert image["array"] == {"shape": [3, 3], "dimensions": ["index_0", "index_1"], "values": [27, 29, 31, 39, 41, 43, 51, 53, 55]}
    assert image["metadata"]["chunks_touched"] == 4
    assert image["metadata"]["decoded_chunk_bytes"] == 48


def test_netcdf4_raw_packing_kept_explicit():
    import h5netcdf
    buffer = io.BytesIO()
    with h5netcdf.File(buffer, "w") as handle:
        handle.dimensions["time"] = 4
        variable = handle.create_variable("temperature", ("time",), dtype="i2", fillvalue=-9999)
        variable[:] = [0, 10, -9999, 20]
        variable.attrs["scale_factor"] = 0.1
        variable.attrs["add_offset"] = 273.15
    data = buffer.getvalue()
    tree, _ = preview(data, fmt="nc")
    result, _ = preview(data, "series", selected(tree, "temperature", [sl(0, 4)]), "nc")
    assert result["array"]["values"] == [0, 10, -9999, 20]
    assert result["metadata"]["attributes"] == {"scale_factor": 0.1, "add_offset": 273.15, "_FillValue": -9999}
    assert result["metadata"]["format"] == "nc"
    assert "no CF" in result["metadata"]["value_semantics"]


def test_nonfinite_and_precision_are_explicit():
    data = fixture(lambda h: h.create_dataset("precise", data=[1.0000000000000002, float("nan"), float("inf"), -float("inf")]))
    tree, _ = preview(data)
    result, _ = preview(data, "series", selected(tree, "precise", [sl(0, 4)]))
    assert result["array"]["values"] == [1.0000000000000002, None, None, None]
    assert result["metadata"]["nonfinite_values"] == 3
    json.dumps(result, allow_nan=False)


def test_boolean_values_are_explicit_zero_one_and_bounded_units_only():
    def build(handle):
        dataset = handle.create_dataset("n", data=np.array([True, False], dtype=bool))
        dataset.attrs["units"] = np.bytes_("flag")
        dataset.attrs["missing_value"] = np.array([0, 1])  # Nonscalar never read.
        dataset.attrs["scale_factor"] = "x" * 200000  # Vlen never read.
    data = fixture(build)
    tree, _ = preview(data)
    result, _ = preview(data, "series", selected(tree, "n", [sl(0, 2)]))
    assert result["array"]["values"] == [1, 0]
    assert all(type(v) is int for v in result["array"]["values"])
    assert result["metadata"]["attributes"] == {"units": "flag"}
    assert result["metadata"]["read_bytes"] < 65536


def test_empty_scalar_and_empty_file_have_inert_catalogs():
    data = fixture(lambda h: (h.create_dataset("empty", (0,), dtype="f8"), h.create_dataset("scalar", data=1)))
    tree, _ = preview(data)
    assert all(v["reason"] == "shape" and not v["selectable"] for v in tree["choices"]["variables"])
    empty, _ = preview(fixture(lambda _: None))
    assert empty["tree"] == [] and empty["choices"]["variables"] == []


@pytest.mark.parametrize("dtype,value", [("i8", 2**53), ("u8", 2**63), ("i8", -(2**53))])
def test_unsafe_js_integer_never_silently_rounded(dtype, value):
    data = fixture(lambda h: h.create_dataset("n", data=np.array([value], dtype=dtype)))
    tree, _ = preview(data)
    with pytest.raises(ArrayWindowError):
        preview(data, "series", selected(tree, "n", [sl(0, 1)]))


def test_unsafe_links_storage_and_datatypes_not_dereferenced():
    def build(handle):
        handle.create_dataset("safe", data=[1, 2])
        handle["soft"] = h5py.SoftLink("/safe")
        handle["external"] = h5py.ExternalLink("/private/secret.h5", "credentials")
        handle.create_dataset("external_store", (2,), dtype="i4", external=[("/private/secret.raw", 0, 8)])
        layout = h5py.VirtualLayout(shape=(2,), dtype="i4")
        layout[:] = h5py.VirtualSource("/private/secret.h5", "x", shape=(2,))
        handle.create_virtual_dataset("virtual", layout)
        handle.create_dataset("variable_string", data=["secret"], dtype=h5py.string_dtype())
        handle.create_dataset("compound", data=np.array([(1,)], dtype=[("secret_field", "i4")]))
        handle.create_dataset("references", (1,), dtype=h5py.ref_dtype)
        handle["cycle"] = handle
    data = fixture(build)
    tree, _ = preview(data)
    reasons = {v["label"]: v["reason"] for v in tree["choices"]["variables"]}
    assert reasons == {"compound": "type", "external_store": "storage", "references": "type", "safe": "", "variable_string": "type", "virtual": "storage"}
    assert "secret" not in json.dumps(tree) and "/private" not in json.dumps(tree)
    assert {n["attributes"]["label"]: n["attributes"]["reason"] for n in tree["tree"]}["cycle"] == "alias"
    for name in ["external_store", "virtual", "variable_string", "references", "compound"]:
        with pytest.raises(ArrayWindowError):
            preview(data, "series", selected(tree, name, [sl(0, 1)]))


def test_unknown_filter_and_oversize_decoded_chunk_are_not_read():
    def build(handle):
        handle.create_dataset("plugin", shape=(8,), dtype="i4", chunks=(8,), compression=32001, allow_unknown_filter=True)
        handle.create_dataset("huge_chunk", shape=(MAX_CHUNK_BYTES,), dtype="i4", chunks=(MAX_CHUNK_BYTES,), compression="gzip")
        handle.create_dataset("lossy", data=np.arange(8, dtype="f4"), scaleoffset=2)
    data = fixture(build)
    tree, _ = preview(data)
    assert {v["label"]: v["reason"] for v in tree["choices"]["variables"]} == {"huge_chunk": "chunk", "lossy": "filter", "plugin": "filter"}
    for name in ["huge_chunk", "lossy", "plugin"]:
        with pytest.raises(ArrayWindowError):
            preview(data, "series", selected(tree, name, [sl(0, 1)]))


@pytest.mark.parametrize("stream", [zlib.compress(b"x" * 1000000), zlib.compress(b"abcd") + b"trailing", zlib.compress(b"a"), b"not-zlib"])
def test_forged_deflate_stream_rejected_before_native_decompression(stream):
    def build(handle):
        dataset = handle.create_dataset("bomb", shape=(4,), dtype="u1", chunks=(4,), compression="gzip")
        dataset.id.write_direct_chunk((0,), stream)
    data = fixture(build)
    tree, _ = preview(data)
    assert tree["choices"]["variables"][0]["selectable"]
    with pytest.raises(ArrayWindowError):
        preview(data, "series", selected(tree, "bomb", [sl(0, 1)]))


def test_checksum_is_still_verified_and_optional_uncompressed_chunk_is_safe():
    def bad_checksum(handle):
        dataset = handle.create_dataset("n", data=np.arange(4, dtype="i4"), chunks=(4,), fletcher32=True)
        mask, chunk = dataset.id.read_direct_chunk((0,))
        dataset.id.write_direct_chunk((0,), chunk[:-1] + bytes([chunk[-1] ^ 255]), filter_mask=mask)
    data = fixture(bad_checksum); tree, _ = preview(data)
    with pytest.raises(ArrayWindowError):
        preview(data, "series", selected(tree, "n", [sl(0, 4)]))
    def optional(handle):
        dataset = handle.create_dataset("n", shape=(4,), dtype="u1", chunks=(4,), compression="gzip")
        dataset.id.write_direct_chunk((0,), bytes([1, 2, 3, 4]), filter_mask=1)
    data = fixture(optional); tree, _ = preview(data)
    result, _ = preview(data, "series", selected(tree, "n", [sl(0, 4)]))
    assert result["array"]["values"] == [1, 2, 3, 4]


@pytest.mark.parametrize("filters", [{"fletcher32": True}, {"compression": "gzip", "fletcher32": True}, {"shuffle": True, "compression": "gzip", "fletcher32": True}])
def test_odd_length_chunks_and_partial_edge_preserve_checksum_and_values(filters):
    data = fixture(lambda h: h.create_dataset("odd", data=np.array([1, 2, 3, 4, 5], dtype="u1"), chunks=(3,), **filters))
    tree, _ = preview(data)
    result, calls = preview(data, "series", selected(tree, "odd", [sl(0, 5)]))
    assert result["array"]["values"] == [1, 2, 3, 4, 5]
    assert result["metadata"]["decoded_chunk_bytes"] == 6 and result["metadata"]["chunks_touched"] == 2
    assert len(calls) <= 3 and result["metadata"]["read_bytes"] <= 65536


def test_exact_decode_budget_with_gzip_preflight_stays_bounded():
    def build(handle):
        dataset = handle.create_dataset("n", shape=(4, MAX_CHUNK_BYTES), dtype="u1", chunks=(1, MAX_CHUNK_BYTES), compression="gzip")
        for row in range(4):
            dataset[row, 0] = row
    data = fixture(build)
    tree, _ = preview(data)
    result, calls = preview(data, "series", selected(tree, "n", [sl(0, 4), 0]))
    assert result["array"]["values"] == [0, 1, 2, 3]
    assert result["metadata"]["decoded_chunk_bytes"] == 16 * 1024**2
    assert result["metadata"]["read_bytes"] < 65536 and len(calls) <= 4


def test_aggregate_chunk_decode_budget_rejected_before_values():
    data = fixture(lambda h: h.create_dataset("many", shape=(6, 524288), dtype="f8", chunks=(1, 524288), compression="gzip"))
    tree, _ = preview(data)
    with pytest.raises(ArrayWindowError):
        preview(data, "series", selected(tree, "many", [sl(0, 6), 0]))


def test_fixed_dimensions_and_out_of_bounds():
    data = fixture(lambda h: h.create_dataset("cube", data=np.arange(24).reshape(2, 3, 4)))
    tree, _ = preview(data)
    result, _ = preview(data, "series", selected(tree, "cube", [1, 2, sl(1, 4)]))
    assert result["array"]["values"] == [21, 22, 23]
    for selection in [[2, 0, sl(0, 4)], [0, 0, sl(0, 5)], [sl(0, 1)]]:
        with pytest.raises(ArrayWindowError):
            preview(data, "series", selected(tree, "cube", selection))


@pytest.mark.parametrize("kind,options", [
    ("tree", {"variable": "x"}), ("series", {}), ("image", {}), ("heatmap", {}),
    ("series", {"variable": "/secret", "selection": [sl(0, 2)], "decode": "raw"}),
    *[("series", {"variable": "v-" + "0" * 32, "selection": value, "decode": "raw"}) for value in [[], [True], [-1], [sl(0, 2, 0)], [sl(2, 1)], [sl(0, 16385)], [sl(0, 2), sl(0, 2)], [sl(0, 2), *([0] * 8)], [dict(sl(0, 2), url="x")]]],
    ("series", {"variable": "v-" + "0" * 32, "selection": [sl(0, 2)], "decode": "cf"}),
])
def test_options_are_strict(kind, options):
    with pytest.raises(ArrayWindowError):
        validate_array_window_options(kind, options)


@pytest.mark.parametrize("size,limits", [(255, None), (MAX_SOURCE_BYTES + 1, None), (True, None), (1024, {}), (1024, {"max_read_bytes": 1024**2, "max_total_bytes": 8 * 1024**2 + 1, "max_reads": 128})])
def test_bad_source_and_budget_no_reads(size, limits):
    calls = []
    with pytest.raises(ArrayWindowError):
        RangeFile(lambda *args: calls.append(args), size, limits)
    assert not calls


def test_rangefile_cache_readinto_seek_and_write_prohibition():
    data = bytes(range(256)) * 100
    calls = []
    source = RangeFile(lambda o, n: (calls.append((o, n)), data[o:o+n])[1], len(data))
    assert source.read(16) == data[:16]
    source.seek(8)
    target = bytearray(10)
    assert source.readinto(target) == 10 and target == data[8:18]
    assert len(calls) == 1
    source.seek(-3, 2)
    assert source.read(10) == data[-3:] and source.read(1) == b""
    for action in [lambda: source.seek(-1), lambda: source.seek(len(data) + 1), lambda: source.write(b"x"), lambda: source.truncate(), lambda: source.read(MAX_ENCODED_CHUNK_BYTES + 1)]:
        with pytest.raises(ArrayWindowError):
            action()
    source.close()
    with pytest.raises(ArrayWindowError):
        source.read(1)


def test_compressed_chunk_preflight_cache_avoids_a_second_range_download():
    data = bytes(range(256)) * 1024
    calls = []
    source = RangeFile(lambda o, n: (calls.append((o, n)), data[o:o+n])[1], len(data))
    source.seek(20000)
    original = source.read(100000)
    source.seek(20000)
    assert source.read(100000) == original
    source.seek(21000)
    assert source.read(1000) == original[1000:2000]
    assert calls == [(20000, 100000)]
    assert source.read_bytes == 100000 and source.read_requests == 1
    source.close()


def test_short_callback_and_read_budget_fail_closed():
    data = fixture()
    with pytest.raises(ArrayWindowError):
        array_window_preview(lambda _o, n: b"x" * (n-1), len(data), "h5")
    with pytest.raises(ArrayWindowError):
        preview(data, limits={"max_read_bytes": 256, "max_total_bytes": 512, "max_reads": 2})


@pytest.mark.parametrize("signature", [b"CDF\x01", b"CDF\x02", b"CDF\x05", b"MATLAB 5.0 MAT-file", b"not HDF"])
def test_non_hdf_signatures_no_whole_fallback(signature):
    with pytest.raises(ArrayWindowError):
        preview(signature.ljust(100000, b"\x00"), fmt="nc")


def test_userblock_hdf_signature(tmp_path):
    filename = tmp_path / "userblock.mat"
    with h5py.File(filename, "w", userblock_size=512) as handle:
        handle.create_dataset("n", data=[1, 2, 3])
    data = filename.read_bytes()
    result, _ = preview(data, fmt="mat")
    assert result["choices"]["variables"][0]["selectable"]


def test_matlab_userblock_chunk_preflight_uses_same_bounded_byte_capability(tmp_path):
    filename = tmp_path / "matlab73.mat"
    with h5py.File(filename, "w", userblock_size=4096) as handle:
        handle.create_dataset("n", data=np.array([1, 2, 3, 4, 5], dtype="u1"), chunks=(3,), compression="gzip", fletcher32=True)
    content = filename.read_bytes()
    content = b"MATLAB 7.3 MAT-file".ljust(128, b" ") + content[128:]
    tree, _ = preview(content, fmt="mat")
    result, calls = preview(content, "series", selected(tree, "n", [sl(0, 5)]), fmt="mat")
    assert result["array"]["values"] == [1, 2, 3, 4, 5]
    assert len(calls) <= 3 and result["metadata"]["read_bytes"] < 65536


def test_catalog_truncated_with_bounded_nodes_and_no_secret_attributes():
    def build(handle):
        for i in range(160):
            dataset = handle.create_dataset(f"v{i:03}", data=[i])
            dataset.attrs["secret"] = "do not return"
    result, _ = preview(fixture(build))
    assert len(result["tree"]) == 128 and result["metadata"]["catalog_truncated"]
    assert "do not return" not in json.dumps(result)


def test_real_sparse_gigabyte_file_reads_tiny_window(tmp_path):
    filename = tmp_path / "sparse.h5"
    with h5py.File(filename, "w") as handle:
        space = h5py.h5s.create_simple((150_000_000,))
        dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        dcpl.set_fill_time(h5py.h5d.FILL_TIME_NEVER)
        dataset = h5py.Dataset(h5py.h5d.create(handle.id, b"large", h5py.h5t.NATIVE_DOUBLE, space, dcpl=dcpl))
        dataset[:3] = [1.25, 2.5, 3.75]
    size = filename.stat().st_size
    assert size > 1024**3
    calls = []
    with filename.open("rb") as stream:
        def read(offset, length):
            calls.append((offset, length))
            stream.seek(offset)
            return stream.read(length)
        tree = array_window_preview(read, size, "h5")
        calls.clear()
        result = array_window_preview(read, size, "h5", "series", selected(tree, "large", [sl(0, 3)]))
    assert result["array"]["values"] == [1.25, 2.5, 3.75]
    assert sum(n for _, n in calls) < 65536 and len(calls) <= 3


def test_backend_pure_validator_accepts_real_results_and_request_bindings():
    import importlib.util
    path = Path(__file__).parents[2] / "backend/app/application/services/array_window_visualization.py"
    if not path.exists():
        path = Path(__file__).parents[2].parent / "backend/app/application/services/array_window_visualization.py"
    spec = importlib.util.spec_from_file_location("array_schema", path)
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)
    data = fixture()
    tree, _ = preview(data)
    options = selected(tree, "image", [sl(1, 3), sl(2, 4)])
    result, calls = preview(data, "image", options)
    schema.validate_array_window_payload(tree, kind="tree", options={}, fmt="h5")
    schema.validate_array_window_payload(result, kind="image", options=options, fmt="h5", source_bytes=len(data), read_bytes=sum(n for _, n in calls), read_requests=len(calls))
    mutations = [lambda r: r["array"]["values"].__setitem__(0, True), lambda r: r["axes"][0]["indices"].__setitem__(0, 5), lambda r: r["metadata"].__setitem__("value_semantics", "scaled"), lambda r: r["metadata"].__setitem__("read_bytes", 8*1024**2+1), lambda r: r["choices"]["variables"][0].__setitem__("label", "/Users/private"), lambda r: r.__setitem__("raw_path", "/tmp/leak")]
    for mutate in mutations:
        bad = copy.deepcopy(result)
        mutate(bad)
        with pytest.raises(schema.ArrayWindowError):
            schema.validate_array_window_payload(bad)
    for binding in [dict(kind="series"), dict(options={**options, "selection": [sl(2, 4), sl(2, 4)]}), dict(fmt="nc"), dict(source_bytes=len(data)+1), dict(read_bytes=1), dict(read_requests=128)]:
        with pytest.raises(schema.ArrayWindowError):
            schema.validate_array_window_payload(result, **binding)
