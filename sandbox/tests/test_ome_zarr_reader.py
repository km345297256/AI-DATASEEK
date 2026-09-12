from __future__ import annotations

import copy
import importlib.util
import json
import math
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import zarr
from numcodecs import Blosc

from app.services import ome_zarr_reader as reader
from app.services.ome_zarr_payload import OmeZarrError, validate_ome_payload
from ome_zarr_window_fixtures import ObjectSource, browser_payloads, change_json, memory_store


def preview(store, kind="tree", options=None, **kwargs):
    source = ObjectSource(store)
    result = reader.ome_zarr_preview(source.read, source.size, source.resources, kind, options, **kwargs)
    return result, source


def roi_options(level=0, indices=None, roi=None):
    return {"level": level, "indices": [1, 1, 2] if indices is None else indices, "roi": [2, 2, 5, 4] if roi is None else roi}


@pytest.mark.parametrize("compression", ["none", "zlib", "gzip", "blosc"])
@pytest.mark.parametrize("separator", [".", "/"])
def test_actual_zarr2_codecs_tczyx_chunk_boundaries(compression, separator):
    store, arrays = memory_store(compression=compression, separator=separator)
    result, source = preview(store, "image", roi_options())
    np.testing.assert_array_equal(result["array"]["values"], arrays[0][1, 1, 2, 2:6, 2:7].reshape(-1))
    assert result["array"]["shape"] == [4, 5]
    assert result["metadata"]["loaded_chunks"] == 4
    assert result["metadata"]["decoded_chunk_bytes"] == 4 * np.prod((1, 1, 2, 3, 4)) * 2
    assert result["metadata"]["read_bytes"] == sum(length for _, _, length in source.reads)
    assert result["metadata"]["read_requests"] == len(source.reads)
    assert all(key in {".zattrs", ".zgroup", "0/.zarray", "1/.zarray"} or key.startswith("0/1" + separator + "1" + separator + "1") for key, _, _ in source.reads)


@pytest.mark.parametrize("compression", ["none", "zlib", "gzip", "blosc"])
def test_structure_reads_only_metadata_not_chunks_or_omero_identity(compression):
    store, _ = memory_store(compression=compression)
    result, source = preview(store)
    assert [key for key, _, _ in source.reads] == [".zgroup", ".zattrs", "0/.zarray", "1/.zarray"]
    assert len(result["choices"]["levels"]) == 2
    assert result["metadata"]["loaded_chunks"] == result["metadata"]["decoded_chunk_bytes"] == 0
    assert "PRIVATE" not in json.dumps(result) and "/private/" not in json.dumps(result)
    assert result["selected"] == {"level": 0, "indices": [0, 0, 0], "roi": [0, 0, 9, 7]}


def test_true_second_level_coordinates_units_and_scale_translation_composition():
    store, arrays = memory_store()
    change_json(store, ".zattrs", lambda attrs: attrs["multiscales"][0].update(coordinateTransformations=[
        {"type": "scale", "scale": [2, 1, 3, 4, 5]}, {"type": "translation", "translation": [1, 0, 2, 3, 4]}]))
    options = roi_options(level=1, roi=[1, 1, 3, 2])
    result, source = preview(store, "image", options)
    np.testing.assert_array_equal(result["array"]["values"], arrays[1][1, 1, 2, 1:3, 1:4].reshape(-1))
    level = result["choices"]["levels"][1]
    assert level["scale"] == [4, 1, 9, 4, 5]
    assert level["translation"] == [21, 0, -4, 403, -246]
    assert [axis["unit"] for axis in result["choices"]["axes"]] == ["second", None, "micrometer", "micrometer", "micrometer"]
    assert all(key in {".zgroup", ".zattrs", "0/.zarray", "1/.zarray"} or key.startswith("1/") for key, _, _ in source.reads)


@pytest.mark.parametrize("shape,chunks,indices", [((5, 7), (3, 4), []), ((3, 5, 7), (2, 3, 4), [2]),
    ((2, 3, 5, 7), (1, 2, 3, 4), [1, 2]), ((2, 2, 3, 5, 7), (1, 1, 2, 3, 4), [1, 1, 2])])
def test_two_to_five_dimensions_and_padded_edge_chunks(shape, chunks, indices):
    store, arrays = memory_store(shape=shape, chunks=chunks, multilevel=False)
    result, _ = preview(store, "image", roi_options(indices=indices, roi=[5, 3, 2, 2]))
    np.testing.assert_array_equal(result["array"]["values"], arrays[0][tuple(indices) + (slice(3, 5), slice(5, 7))].reshape(-1))
    assert result["array"]["shape"] == [2, 2]


@pytest.mark.parametrize("dtype,values", [("|u1", [0, 1, 254, 255]), ("|i1", [-128, 0, 1, 127]),
    ("<u2", [0, 1, 2, 65535]), (">u2", [0, 1, 2, 65535]), ("<i2", [-32768, 0, 1, 32767]),
    (">i4", [-2**31, -1, 0, 2**31-1]), ("<u4", [0, 1, 2, 2**32-1]),
    ("<i8", [-2**53+1, 0, 1, 2**53-1]), (">u8", [0, 1, 2, 2**53-1]),
    ("<f2", [-1.5, 0, 1.25, 65504]), (">f4", [-1.25, 0, 1, 2.5]), ("<f8", [-1e308, 0, 1, 1e308])])
def test_dtype_endian_and_raw_value_precision(dtype, values):
    store, arrays = memory_store(shape=(2, 2), chunks=(2, 2), dtype=dtype, values=values, multilevel=False)
    result, _ = preview(store, "image", roi_options(indices=[], roi=[0, 0, 2, 2]))
    assert result["array"]["dtype"] == np.dtype(dtype).str
    np.testing.assert_array_equal(result["array"]["values"], arrays[0].reshape(-1))
    assert result["metadata"]["value_range"] == [min(values), max(values)]


def test_finite_fill_value_is_not_masked_and_nonfinite_values_become_null():
    store, _ = memory_store(shape=(2, 3), chunks=(2, 3), dtype="<f8", values=[-99, 0, 1, np.nan, np.inf, -np.inf], fill_value=-99, multilevel=False)
    result, _ = preview(store, "image", roi_options(indices=[], roi=[0, 0, 3, 2]))
    assert result["array"]["values"] == [-99, 0, 1, None, None, None]
    assert result["metadata"]["invalid_values"] == 3 and result["metadata"]["value_range"] == [-99, 1]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("value", [2**53, -(2**53)])
def test_unsafe_64_bit_integer_is_rejected_not_rounded(value):
    store, _ = memory_store(shape=(1, 1), chunks=(1, 1), dtype="<i8", values=[value], multilevel=False)
    with pytest.raises(OmeZarrError):
        preview(store, "image", roi_options(indices=[], roi=[0, 0, 1, 1]))


def test_missing_selected_chunk_is_rejected_not_filled():
    store, _ = memory_store()
    del store["0/1.1.1.0.0"]
    with pytest.raises(OmeZarrError):
        preview(store, "image", roi_options())


def test_missing_unselected_chunk_does_not_cause_unrelated_pixels_to_load():
    store, arrays = memory_store()
    del store["0/0.0.0.0.0"]
    result, _ = preview(store, "image", roi_options())
    np.testing.assert_array_equal(result["array"]["values"], arrays[0][1, 1, 2, 2:6, 2:7].reshape(-1))


@pytest.mark.parametrize("bad_key", ["../0/.zarray", "/0/.zarray", "https://invalid.example/data", "0/../0.0", "0/00.0", "0/0..0", "0/0\\0", "0/.zattrs", "1/../../.zattrs", "0/0.0.0.0.0.0"])
def test_private_object_capability_rejects_unsafe_unregistered_names_before_reads(bad_key):
    store, _ = memory_store(shape=(2, 2), chunks=(2, 2), multilevel=False)
    store[bad_key] = b"x"
    source = ObjectSource(store)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(source.read, source.size, source.resources)
    assert source.reads == []


@pytest.mark.parametrize("mutation", [lambda rows: rows.append(dict(rows[0])), lambda rows: rows[1].update(offset=0),
    lambda rows: rows[0].update(size=0), lambda rows: rows[0].update(size=True), lambda rows: rows[0].update(offset=True),
    lambda rows: rows[0].update(size=4 * 1024**2 + 1), lambda rows: rows[0].update(file_id="foreign")])
def test_object_table_duplicate_overlap_overflow_or_identity_fields_rejected(mutation):
    store, _ = memory_store(shape=(2, 2), chunks=(2, 2), multilevel=False)
    source = ObjectSource(store)
    mutation(source.resources)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(source.read, source.size, source.resources)
    assert not source.reads


@pytest.mark.parametrize("mutation", [lambda a: a.update(plate={}), lambda a: a.update(labels=[]),
    lambda a: a["multiscales"][0].update(version="0.5"), lambda a: a["multiscales"].append(a["multiscales"][0]),
    lambda a: a["multiscales"][0]["datasets"][0].update(path="../outside"),
    lambda a: a["multiscales"][0]["datasets"][1].update(path="0"),
    lambda a: a["multiscales"][0]["axes"][-1].update(name="z"),
    lambda a: a["multiscales"][0]["axes"][1].update(unit="meter"),
    lambda a: a["multiscales"][0]["axes"][-1].update(unit="unknown-unit"),
    lambda a: a["multiscales"][0]["datasets"][0].update(coordinateTransformations=[{"type": "affine", "affine": []}]),
    lambda a: a["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"].__setitem__(0, 0),
    lambda a: a["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"].__setitem__(0, float("inf"))])
def test_ngff_metadata_layout_transform_or_units_cannot_escape_subset(mutation):
    store, _ = memory_store()
    change_json(store, ".zattrs", mutation)
    with pytest.raises((OmeZarrError, ValueError)):
        preview(store)


@pytest.mark.parametrize("mutation", [lambda a: a.update(zarr_format=True), lambda a: a.update(zarr_format=3),
    lambda a: a.update(dtype="|O"), lambda a: a.update(dtype=[['x', '<i4']]), lambda a: a.update(dtype="<f1"),
    lambda a: a.update(order="F"), lambda a: a.update(filters=[{"id": "pickle"}]),
    lambda a: a.update(compressor={"id": "pickle"}), lambda a: a.update(compressor={"id": "zlib", "level": 99}),
    lambda a: a.update(compressor={"id": "blosc", "cname": "arbitrary", "clevel": 1, "shuffle": 0}),
    lambda a: a.update(compressor={"id": "zlib", "level": 1, "plugin": "external"}),
    lambda a: a.update(chunks=[1000] * 5), lambda a: a.update(shape=[10**9] * 5), lambda a: a.update(fill_value=-1),
    lambda a: a.update(dimension_separator="../"), lambda a: a.update(chunks=[1, 1, 1, True, 1])])
def test_array_metadata_unknown_plugins_dtype_decoded_budget_and_overflow_rejected(mutation):
    store, _ = memory_store()
    change_json(store, "0/.zarray", mutation)
    with pytest.raises((OmeZarrError, ValueError)):
        preview(store)


@pytest.mark.parametrize("compression", ["zlib", "gzip", "blosc"])
@pytest.mark.parametrize("mutation", ["truncated", "trailing", "expanded"])
def test_compressed_chunks_must_terminate_exactly_at_declared_decoded_size(compression, mutation):
    store, _ = memory_store(shape=(2, 2), chunks=(2, 2), multilevel=False, compression=compression)
    key = "0/0.0"
    data = bytes(store[key])
    if mutation == "truncated":
        store[key] = data[:-1]
    elif mutation == "trailing":
        store[key] = data + b"unexpected"
    elif compression == "blosc":
        changed = bytearray(data)
        struct.pack_into("<I", changed, 4, 1024**3)
        store[key] = bytes(changed)
    else:
        encoder = zlib.compressobj(wbits=31 if compression == "gzip" else 15)
        store[key] = encoder.compress(b"\0" * 10000) + encoder.flush()
    with pytest.raises((OmeZarrError, ValueError, zlib.error)):
        preview(store, "image", roi_options(indices=[], roi=[0, 0, 2, 2]))


@pytest.mark.parametrize("kind,options", [("tree", {"path": "0"}), ("image", {}),
    ("image", {"level": True, "indices": [], "roi": [0, 0, 1, 1]}), ("image", {"level": 16, "indices": [], "roi": [0, 0, 1, 1]}),
    ("image", {"level": 0, "indices": [True], "roi": [0, 0, 1, 1]}), ("image", {"level": 0, "indices": [], "roi": [0, 0, 129, 1]}),
    ("image", {"level": 0, "indices": [], "roi": [0, 0, 1, 1], "codec": "external"})])
def test_options_cannot_create_paths_codecs_or_unbounded_selections(kind, options):
    store, _ = memory_store()
    source = ObjectSource(store)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(source.read, source.size, source.resources, kind, options)
    assert not source.reads


@pytest.mark.parametrize("option", [roi_options(level=2), roi_options(indices=[2, 1, 2]), roi_options(indices=[1, 1]), roi_options(roi=[8, 6, 2, 2])])
def test_selection_outside_level_or_plane_rejects_before_chunks(option):
    store, _ = memory_store()
    source = ObjectSource(store)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(source.read, source.size, source.resources, "image", option)
    assert all(key.endswith((".zattrs", ".zarray", ".zgroup")) for key, _, _ in source.reads)


def test_no_arbitrary_numcodecs_registry_is_used(monkeypatch):
    import numcodecs
    import numcodecs.registry
    store, _ = memory_store(compression="blosc", shape=(2, 2), chunks=(2, 2), multilevel=False)
    monkeypatch.setattr(numcodecs, "get_codec", lambda *_: pytest.fail("codec registry"))
    monkeypatch.setattr(numcodecs.registry, "get_codec", lambda *_: pytest.fail("codec registry"))
    result, _ = preview(store, "image", roi_options(indices=[], roi=[0, 0, 2, 2]))
    assert result["array"]["values"] == [0, 1, 2, 3]


def test_real_payload_binding_and_backend_pure_copy_are_identical():
    sandbox = Path(reader.__file__).with_name("ome_zarr_payload.py")
    backend = Path(__file__).resolve().parents[2] / "backend/app/application/services/ome_zarr_payload.py"
    assert sandbox.read_bytes() == backend.read_bytes()
    spec = importlib.util.spec_from_file_location("ome_backend_contract", backend)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for kind in ("tree", "image"):
        payload = browser_payloads()[kind]
        assert module.validate_ome_payload(payload, kind=kind, size=payload["metadata"]["source_bytes"]) is payload


@pytest.mark.parametrize("kwargs", [{"kind": "tree"}, {"size": 1}, {"read_bytes": 1}, {"read_requests": 1},
    {"options": {"level": 1, "indices": [0, 1, 2], "roi": [1, 1, 3, 2]}}, {"options": {"level": 0, "indices": [1, 1, 2], "roi": [1, 1, 3, 2]}}])
def test_output_cannot_rebind_to_a_different_request(kwargs):
    payload = browser_payloads()["image"]
    with pytest.raises(OmeZarrError):
        validate_ome_payload(payload, **kwargs)


@pytest.mark.parametrize("axis,unit", [(0, "micrometer"), (-1, "second")])
def test_time_and_space_units_must_match_axis_type(axis, unit):
    store, _ = memory_store()
    change_json(store, ".zattrs", lambda attrs: attrs["multiscales"][0]["axes"][axis].update(unit=unit))
    with pytest.raises(OmeZarrError):
        preview(store)


@pytest.mark.parametrize("field,value", [("sampled", False), ("loaded_chunks", 2), ("decoded_chunk_bytes", 0),
    ("decoded_chunk_bytes", 31), ("invalid_values", True)])
def test_output_sampling_and_decoded_chunk_accounting_are_exact(field, value):
    result = browser_payloads()["image"]
    (result if field == "sampled" else result["metadata"])[field] = value
    with pytest.raises(OmeZarrError):
        validate_ome_payload(result)


@pytest.mark.parametrize("value", [-1, 0.5, 65536, None])
def test_integer_payload_values_must_match_dtype_range_and_null_policy(value):
    result = browser_payloads()["image"]
    result["array"]["values"][0] = value
    valid = [v for v in result["array"]["values"] if v is not None]
    result["metadata"].update(value_range=[min(valid), max(valid)], invalid_values=1 if value is None else 0)
    with pytest.raises(OmeZarrError):
        validate_ome_payload(result)


@pytest.mark.parametrize("key,data", [(".zgroup", b'{"zarr_format":2,"zarr_format":2}'),
    (".zgroup", b'{"zarr_format":true}'), (".zattrs", b'{"multiscales":NaN}'),
    (".zattrs", b'{"multiscales":[]}' + b' ' * 65536), (".zattrs", b'\xff'),
    (".zattrs", b'[' * 2000 + b'0' + b']' * 2000)],
    ids=["duplicate-key", "boolean-format", "non-finite", "oversized", "invalid-utf8", "deep-json"])
def test_json_metadata_is_bounded_duplicate_free_finite_and_utf8(key, data):
    store, _ = memory_store()
    store[key] = data
    with pytest.raises((OmeZarrError, ValueError)):
        preview(store)


@pytest.mark.parametrize("limits", [{"max_read_bytes": 32, "max_total_bytes": 32, "max_reads": 256},
    {"max_read_bytes": 1024**2, "max_total_bytes": 32 * 1024**2, "max_reads": 1},
    {"max_read_bytes": 1024**2 + 1, "max_total_bytes": 32 * 1024**2, "max_reads": 256},
    {"max_read_bytes": 1024**2, "max_total_bytes": 32 * 1024**2 + 1, "max_reads": 256},
    {"max_read_bytes": 1024**2, "max_total_bytes": 32 * 1024**2, "max_reads": 257}])
def test_object_reads_enforce_count_per_read_and_total_caps(limits):
    store, _ = memory_store()
    source = ObjectSource(store)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(source.read, source.size, source.resources, limits=limits)
    assert len(source.reads) <= 1


def test_small_range_budget_splits_only_the_selected_objects():
    store, _ = memory_store(shape=(2, 2), chunks=(2, 2), multilevel=False)
    result, source = preview(store, "image", roi_options(indices=[], roi=[0, 0, 2, 2]),
        limits={"max_read_bytes": 32, "max_total_bytes": 32 * 1024**2, "max_reads": 256})
    assert all(length <= 32 for _, _, length in source.reads)
    assert result["array"]["values"] == [0, 1, 2, 3]
    assert result["metadata"]["read_requests"] == len(source.reads)


@pytest.mark.parametrize("mode", ["chunks", "decoded"])
def test_chunk_count_and_accumulated_decoded_budget_reject_before_loading_blocks(mode):
    store, _ = memory_store(multilevel=False)
    if mode == "chunks":
        change_json(store, "0/.zarray", lambda a: a.update(shape=[2, 2, 3, 7, 10], chunks=[1, 1, 1, 1, 1]))
        options = roi_options(roi=[0, 0, 10, 7])
    else:
        change_json(store, "0/.zarray", lambda a: a.update(chunks=[64, 64, 64, 2, 2], dtype="<f4"))
        options = roi_options(roi=[0, 0, 9, 7])
    source = ObjectSource(store)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(source.read, source.size, source.resources, "image", options)
    assert all(key.endswith((".zattrs", ".zarray", ".zgroup")) for key, _, _ in source.reads)


@pytest.mark.parametrize("size", [0, True, 8 * 1024**3 + 1])
def test_source_size_and_object_sum_are_validated_before_reads(size):
    store, _ = memory_store()
    source = ObjectSource(store)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(source.read, size, source.resources)
    assert not source.reads


def test_short_member_range_is_not_padded():
    store, _ = memory_store()
    source = ObjectSource(store)
    with pytest.raises(OmeZarrError):
        reader.ome_zarr_preview(lambda offset, length: source.read(offset, length)[:-1], source.size, source.resources)


@pytest.mark.parametrize("mutate", [lambda r: r["array"].update(shape=[True, True]),
    lambda r: r["metadata"].update(value_range=[False, False])])
def test_boolean_shape_and_range_do_not_impersonate_numeric_response(mutate):
    store, _ = memory_store(shape=(1, 1), chunks=(1, 1), multilevel=False, values=[0])
    result, _ = preview(store, "image", roi_options(indices=[], roi=[0, 0, 1, 1]))
    mutate(result)
    with pytest.raises(OmeZarrError):
        validate_ome_payload(result)


def test_tree_boolean_shape_does_not_impersonate_dimension_one():
    store, _ = memory_store(shape=(1, 1), chunks=(1, 1), multilevel=False, values=[0])
    result, _ = preview(store)
    result["tree"][0]["attributes"]["shape"] = [True, True]
    with pytest.raises(OmeZarrError):
        validate_ome_payload(result)


@pytest.mark.parametrize("field,axis,value", [("shape", -1, 10), ("scale", -1, .25), ("shape", 0, 3), ("scale", 0, 3)])
def test_payload_preserves_reader_multiscale_order_and_nonspatial_consistency(field, axis, value):
    result = browser_payloads()["image"]
    result["choices"]["levels"][1][field][axis] = value
    with pytest.raises(OmeZarrError):
        validate_ome_payload(result)
