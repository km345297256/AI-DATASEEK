import copy
import importlib.util
import json
import zlib
from pathlib import Path

import h5py
import numpy as np
import pytest

from app.services.nexus_window_reader import NexusWindowError, nexus_window_preview
from app.services.nexus_window_payload import validate_nexus_window_options, validate_nexus_window_payload
from nexus_window_fixtures import browser_payloads, fixture, nxdata, preview, selected, sl


def test_actual_tree_series_image_original_values_units_and_errors():
    result = browser_payloads()
    assert len(result["tree"]["choices"]["signals"]) == 2
    assert "array" not in result["tree"] and result["tree"]["metadata"]["output_values"] == 0
    assert result["series"]["array"]["values"] == [2, 4, 6, 8]
    assert result["series"]["axes"][0]["values"] == [101, 102, 103, 104]
    assert result["series"]["axes"][0]["unit"] == "eV"
    assert result["series"]["errors"] == [1, 1, 1, 1]
    assert result["image"]["array"]["values"] == [8, 9, 10, 14, 15, 16]
    assert result["image"]["axes"][0]["values"] == [100.5, 101]
    assert result["image"]["axes"][1]["values"] == [201, 201.5, 202]
    assert result["image"]["metadata"]["output_values"] == 17


@pytest.mark.parametrize("fmt", ["nxs", "nx", "h5", "hdf5", "hdf"])
def test_hdf5_format_envelope_binding(fmt):
    result, _ = preview(fixture(), fmt=fmt)
    assert result["metadata"]["format"] == fmt


@pytest.mark.parametrize("dtype", ["|i1", "|u1", "<i2", ">i2", "<u2", ">u2", "<i4", ">i4", "<u4", ">u4", "<i8", ">i8", "<u8", ">u8", "<f4", ">f4", "<f8", ">f8"])
def test_supported_dtype_and_endian_raw_values(dtype):
    data = fixture(lambda h: nxdata(h, dtype=dtype))
    tree, _ = preview(data)
    out, _ = preview(data, "series", selected(tree, "spectrum", [sl(3, 8)]))
    assert out["choices"]["signals"][0]["dtype"] == dtype
    assert out["array"]["values"] == list(range(3, 8))


@pytest.mark.parametrize("build", [
    lambda h: nxdata(h, axes=False),
    lambda h: nxdata(h).attrs.__setitem__("axes", np.array([b"."])),
])
def test_explicit_index_fallback_only_for_missing_or_dot_axes(build):
    data = fixture(build)
    tree, _ = preview(data)
    out, _ = preview(data, "series", selected(tree, "spectrum", [sl(1, 7, 2)]))
    assert out["axes"] == [{"dimension": 0, "indices": [1, 3, 5], "values": [1, 3, 5], "label": "index_0", "unit": None, "source": "index"}]


def test_same_file_hard_link_signal_and_axis_are_allowed():
    def build(handle):
        group = nxdata(handle)
        shared = handle.create_group("shared")
        shared["signal"] = group["counts"]
        shared["coordinates"] = group["energy"]
        del group["counts"]
        del group["energy"]
        group["counts"] = shared["signal"]
        group["energy"] = shared["coordinates"]
        shared["cycle"] = handle
    data = fixture(build)
    tree, _ = preview(data)
    assert len(tree["choices"]["signals"]) == 1
    out, _ = preview(data, "series", selected(tree, "spectrum", [sl(2, 4)]))
    assert out["array"]["values"] == [2, 3]


@pytest.mark.parametrize("mutation", [
    lambda g: g.attrs.__setitem__("signal", np.bytes_("missing")),
    lambda g: g.attrs.__setitem__("signal", np.bytes_("../counts")),
    lambda g: g.attrs.__setitem__("signal", "counts"),
    lambda g: g.attrs.__setitem__("axes", np.array([b"energy", b"energy"])),
    lambda g: g.attrs.__setitem__("axes", np.array([b"missing"])),
    lambda g: g.attrs.__setitem__("axes", np.bytes_("energy:x")),
    lambda g: g.attrs.__setitem__("energy_indices", np.int32(1)),
    lambda g: g.attrs.__setitem__("energy_indices", np.array([0, 1])),
    lambda g: g["counts"].attrs.__setitem__("uncertainties", np.bytes_("counts_errors")),
    lambda g: g["counts"].attrs.__setitem__("axes", np.bytes_("other")),
    lambda g: g["counts_errors"].attrs.__setitem__("units", np.bytes_("mm")),
    lambda g: g["energy"].attrs.__setitem__("units", "x" * 1000000),
    lambda g: g.create_dataset("errors", data=np.ones(12)),
])
def test_unsupported_or_conflicting_metadata_not_guessed(mutation):
    data = fixture(lambda h: mutation(nxdata(h)))
    tree, _ = preview(data)
    assert tree["choices"]["signals"] == []
    assert tree["metadata"]["skipped_nxdata"] == 1


@pytest.mark.parametrize("kind", ["bin-edge", "multidimensional", "wrong-errors", "rank3", "compound", "vlen", "scaleoffset", "external", "virtual"])
def test_invalid_axis_shape_dtype_storage_or_rank_is_unselectable(kind):
    def build(h):
        g = nxdata(h)
        if kind in {"bin-edge", "multidimensional"}:
            del g["energy"]
            g.create_dataset("energy", data=np.arange(13) if kind == "bin-edge" else np.ones((3, 4)))
        elif kind == "wrong-errors":
            del g["counts_errors"]
            g.create_dataset("counts_errors", data=np.ones(11))
        else:
            del g["counts"]
            if kind == "rank3": g.create_dataset("counts", data=np.ones((2, 3, 4)))
            elif kind == "compound": g.create_dataset("counts", data=np.zeros(12, dtype=[("secret", "i4")]))
            elif kind == "vlen": g.create_dataset("counts", shape=(12,), dtype=h5py.string_dtype())
            elif kind == "scaleoffset": g.create_dataset("counts", data=np.arange(12), scaleoffset=1)
            elif kind == "external": g.create_dataset("counts", shape=(12,), dtype="f8", external=[("never-read.bin", 0, 96)])
            else:
                layout = h5py.VirtualLayout(shape=(12,), dtype="f8")
                layout[:] = h5py.VirtualSource("never-read.h5", "data", shape=(12,))
                g.create_virtual_dataset("counts", layout)
    tree, _ = preview(fixture(build))
    assert tree["choices"]["signals"] == []


@pytest.mark.parametrize("link", [h5py.ExternalLink("/private/NEVER", "/x"), h5py.SoftLink("/entry/spectrum/counts")])
def test_links_rejected_before_target_dereference(link):
    def build(h):
        nxdata(h)
        h["blocked"] = link
    with pytest.raises(NexusWindowError): preview(fixture(build))


@pytest.mark.parametrize("values", [[0, 0, 1, 2], [0, np.nan, 1, 2], [0, 2, 1, 3], [0, np.inf, 2, 3]])
def test_nonfinite_or_nonmonotone_coordinates_rejected(values):
    def build(h):
        g = nxdata(h, shape=(4,))
        g["energy"][:] = values
    data = fixture(build)
    tree, _ = preview(data)
    with pytest.raises(NexusWindowError): preview(data, "series", selected(tree, "spectrum", [sl(0, 4)]))


def test_descending_axes_and_nonfinite_signal_preserved_without_scaling():
    def build(h):
        g = nxdata(h, shape=(4,))
        g["energy"][:] = [4, 3, 2, 1]
        g["counts"][:] = [1.0000000000000002, np.nan, np.inf, -9999]
        g["counts"].attrs["scale_factor"] = 1000.0
        g["counts"].attrs["add_offset"] = 1000.0
    data = fixture(build)
    tree, _ = preview(data)
    out, _ = preview(data, "series", selected(tree, "spectrum", [sl(0, 4)]))
    assert out["array"]["values"] == [1.0000000000000002, None, None, -9999]
    assert out["axes"][0]["values"] == [4, 3, 2, 1]
    assert out["metadata"]["nonfinite_values"] == 2


@pytest.mark.parametrize("field,values", [("counts", [2**53, 1]), ("counts_errors", [-1, 1])])
def test_unsafe_integer_or_negative_uncertainty_rejected(field, values):
    def build(h):
        g = nxdata(h, shape=(2,), dtype="i8")
        g[field][:] = values
    data = fixture(build)
    tree, _ = preview(data)
    with pytest.raises(NexusWindowError): preview(data, "series", selected(tree, "spectrum", [sl(0, 2)]))


def test_source_paths_and_html_attributes_are_redacted():
    def build(h):
        g = nxdata(h, "<script>")
        g["counts"].attrs["units"] = np.bytes_("/Users/private/secret")
        g["counts_errors"].attrs["units"] = np.bytes_("/Users/private/secret")
    out, _ = preview(fixture(build))
    encoded = json.dumps(out)
    assert "<script>" not in encoded and "/Users/" not in encoded
    assert "[redacted]" in encoded


@pytest.mark.parametrize("dtype", ["f2", "?"])
def test_unsupported_half_float_and_boolean_do_not_poison_other_nxdata(dtype):
    def build(h):
        nxdata(h, "valid")
        g = nxdata(h, "unsupported")
        del g["counts"]
        g.create_dataset("counts", data=np.ones(12, dtype=dtype))
    tree, _ = preview(fixture(build))
    assert [v["label"] for v in tree["choices"]["signals"]] == ["valid"]
    assert tree["metadata"]["skipped_nxdata"] == 1


def test_unit_conflict_is_compared_before_redaction():
    def build(h):
        g = nxdata(h)
        g["counts"].attrs["units"] = np.bytes_("/Users/secret/a")
        g["counts_errors"].attrs["units"] = np.bytes_("/Users/secret/b")
    tree, _ = preview(fixture(build))
    assert tree["choices"]["signals"] == []


@pytest.mark.parametrize("limit", ["signals", "objects", "depth"])
def test_catalog_limits_are_explicit_not_an_unbounded_search(limit):
    def build(h):
        if limit == "signals":
            for index in range(33):
                nxdata(h, f"signal-{index:03}", axes=False, errors=False, chunks=False)
        elif limit == "objects":
            for index in range(200): h.create_group(f"empty-{index:03}")
        else:
            nxdata(h.create_group("a/b/c/d/e/f/g/h"))
    tree, reads = preview(fixture(build))
    assert tree["metadata"]["catalog_truncated"] is True
    assert len(tree["choices"]["signals"]) == (32 if limit == "signals" else 0)
    assert len(reads) <= 128


@pytest.mark.parametrize("mutation", [
    lambda g: g.attrs.__setitem__("signal", np.bytes_("x" * 129)),
    lambda g: g.attrs.__setitem__("axes", np.array([[b"energy"]])),
    lambda g: g.attrs.__setitem__("signal", np.bytes_(b"cou\0nts")),
])
def test_fixed_attribute_size_shape_and_nul_are_not_silently_coerced(mutation):
    tree, _ = preview(fixture(lambda h: mutation(nxdata(h))))
    assert tree["choices"]["signals"] == []


def test_metadata_total_budget_is_checked_before_reading_attribute_value():
    from app.services.nexus_window_reader import _Metadata, _Unsupported
    def build(h):
        g = nxdata(h)
        g.attrs["bounded"] = np.bytes_("x" * 128)
        metadata = _Metadata()
        for _ in range(512): assert metadata.attr(g, "bounded") == "x" * 128
        assert metadata.bytes == 65536
        with pytest.raises(_Unsupported): metadata.attr(g, "bounded")
        assert metadata.bytes == 65536
    fixture(build)


@pytest.mark.parametrize("stream", [zlib.compress(b"x" * 1048576), zlib.compress(b"x" * 32) + b"trailing", b"broken"], ids=["decode-bomb", "trailing-stream", "corrupt-stream"])
def test_hdf5_compressed_chunk_is_verified_before_native_decode(stream):
    def build(h):
        g = nxdata(h)
        g["counts"].id.write_direct_chunk((0,), stream)
    data = fixture(build)
    tree, _ = preview(data)
    with pytest.raises(NexusWindowError): preview(data, "series", selected(tree, "spectrum", [sl(0, 1)]))


def test_aggregate_chunk_budget_is_preflighted_before_any_values_are_read(monkeypatch):
    def build(h):
        g = h.create_group("entry/spectrum")
        g.attrs["NX_class"], g.attrs["signal"], g.attrs["axes"] = np.bytes_("NXdata"), np.bytes_("counts"), np.array([b"energy"])
        for name in ("counts", "energy", "counts_errors"):
            g.create_dataset(name, shape=(1048576,), dtype="f8", chunks=(262144,), compression="gzip")
    data = fixture(build)  # Unallocated chunks, not a large materialized file.
    tree, _ = preview(data)
    import app.services.nexus_window_reader as module
    reads = []
    monkeypatch.setattr(module, "_read", lambda *a: reads.append(a))
    with pytest.raises(NexusWindowError): preview(data, "series", selected(tree, "spectrum", [sl(0, 1048576, 262144)]))
    assert reads == []  # Each field is 8 MiB decoded; their 24 MiB sum is rejected.


def test_sparse_large_source_tree_and_small_window_never_fetch_whole_source():
    data = fixture()
    size, reads = 2500000000, []
    def read(offset, length):
        reads.append((offset, length))
        assert length <= 1024**2
        return data[offset:offset + length].ljust(length, b"\0")
    tree = nexus_window_preview(read, size, "nxs")
    out = nexus_window_preview(read, size, "nxs", "series", selected(tree, "spectrum", [sl(1, 3)]))
    assert out["array"]["values"] == [1, 2]
    assert sum(n for _, n in reads) < 65536


def test_output_budget_includes_signal_axes_and_errors():
    data = fixture(lambda h: nxdata(h, shape=(6000,), chunks=False))
    tree, _ = preview(data)
    with pytest.raises(NexusWindowError): preview(data, "series", selected(tree, "spectrum", [sl(0, 6000)]))


@pytest.mark.parametrize("limits", [
    {"max_read_bytes": 1, "max_total_bytes": 1, "max_reads": 1},
    {"max_read_bytes": 1048577, "max_total_bytes": 8388608, "max_reads": 128},
    {"max_read_bytes": True, "max_total_bytes": 8388608, "max_reads": 128},
    {"max_read_bytes": 1048576, "max_total_bytes": 8388609, "max_reads": 128},
])
def test_read_budgets_are_never_widened(limits):
    with pytest.raises(NexusWindowError): preview(fixture(), limits=limits)


def test_cancelled_or_short_read_is_not_retried_or_replaced_by_file_io():
    data = fixture()
    calls = []
    def cancelled(offset, length):
        calls.append((offset, length))
        raise RuntimeError("cancelled /private/hidden")
    with pytest.raises(NexusWindowError) as error: nexus_window_preview(cancelled, len(data), "nxs")
    assert len(calls) == 1 and "hidden" not in str(error.value)
    with pytest.raises(NexusWindowError): nexus_window_preview(lambda o, n: b"", len(data), "nxs")


def test_host_and_worker_schema_copies_are_identical_and_cross_validate_actual_payload():
    root = Path(__file__).resolve().parents[2]
    host = root / "backend/app/application/services/nexus_window_visualization.py"
    if not host.exists(): pytest.skip("separate sandbox build has no backend tree")
    worker = root / "sandbox/app/services/nexus_window_payload.py"
    assert host.read_bytes() == worker.read_bytes()
    spec = importlib.util.spec_from_file_location("isolated_nexus_validator", host)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, payload in browser_payloads().items():
        if key == "provenance": continue
        assert module.validate_nexus_window_payload(payload, kind=payload["kind"], options=payload["selected"], fmt="nxs", source_bytes=payload["metadata"]["source_bytes"], read_bytes=payload["metadata"]["read_bytes"], read_requests=payload["metadata"]["read_requests"]) is payload
