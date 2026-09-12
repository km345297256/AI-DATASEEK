import copy
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest

from app.services.grib_eccodes_runtime import get_eccodes_runtime
from app.services.grib_window_reader import GribWindowError, grib_window_preview
from grib_window_fixtures import browser_payloads, message, preview, sections

ec = get_eccodes_runtime()


def test_pinned_eccodes_binary_and_embedded_definitions_are_available():
    assert ec.codes_get_api_version() == "2.48.2"
    assert ec.codes_definition_path() == "/MEMFS/definitions"
    assert ec.codes_samples_path() == "/MEMFS/samples"


def test_real_eccodes_fixture_payload_values_coordinates_units_bitmap():
    payload = browser_payloads()
    assert len(payload["tree"]["choices"]["messages"]) == 2
    assert payload["image"]["array"]["values"] == [281, 282, 283, None, 287, 288, 289, 290, 293, 294, 295, 296]
    assert payload["image"]["axes"][0]["values"] == [50, 49, 48]
    assert payload["image"]["axes"][1]["values"] == [11, 12, 13, 14]
    assert payload["image"]["choices"]["messages"][0]["unit"] == "K"
    assert payload["image"]["metadata"]["decoded_points"] == 24


@pytest.mark.parametrize("scanning", [0, 32, 64, 96, 128, 160, 192, 224])
def test_all_supported_scan_directions_and_consecutive_dimension(scanning):
    data = message(scanning=scanning)
    tree, _ = preview(data)
    out, _ = preview(data, "image", {"message": tree["choices"]["messages"][0]["id"], "roi": [0, 0, 6, 4]})
    expected = np.arange(24, dtype=float) + 280
    expected[4] = np.nan
    expected = expected.reshape((6, 4)).T if scanning & 32 else expected.reshape((4, 6))
    assert out["array"]["values"] == [None if math.isnan(v) else v for v in expected.reshape(-1)]
    assert out["axes"][1]["values"] == (list(range(15, 9, -1)) if scanning & 128 else list(range(10, 16)))
    assert out["axes"][0]["values"] == (list(range(47, 51)) if scanning & 64 else list(range(50, 46, -1)))


def test_tree_never_reads_section7_payload_or_calls_native_array_decoders(monkeypatch):
    data = message()
    start, length = sections(data)[7]
    def forbidden(*args, **kwargs): pytest.fail("tree must not unpack values or coordinates")
    monkeypatch.setattr(ec, "codes_get_values", forbidden)
    monkeypatch.setattr(ec, "codes_get_array", forbidden)
    tree, calls = preview(data)
    assert tree["metadata"]["decoded_points"] == 0
    assert all(offset + count <= start + 5 or offset >= start + length for offset, count in calls)
    assert sum(count for _, count in calls) < len(data)


def test_actual_catalog_next_page_and_second_message_are_explicit():
    block = message()
    data = block * 10
    first, _ = preview(data)
    assert len(first["choices"]["messages"]) == 8 and first["metadata"]["next_offset"] == 8 * len(block)
    second, calls = preview(data, options={"offset": first["metadata"]["next_offset"]})
    assert len(second["choices"]["messages"]) == 2 and second["metadata"]["next_offset"] is None
    assert min(o for o, _ in calls) >= 8 * len(block)
    out, _ = preview(data, "image", {"message": second["choices"]["messages"][1]["id"], "roi": [1, 1, 2, 2]})
    assert out["array"]["values"] == [287, 288, 293, 294]


def test_seam_is_never_sorted_or_bridged_but_single_side_roi_is_valid():
    data = message(longitude=358)
    tree, _ = preview(data)
    options = {"message": tree["choices"]["messages"][0]["id"], "roi": [1, 0, 3, 2]}
    with pytest.raises(GribWindowError): preview(data, "image", options)
    options["roi"] = [2, 0, 3, 2]
    out, _ = preview(data, "image", options)
    assert out["axes"][1]["values"] == [0, 1, 2]


@pytest.mark.parametrize("fmt", ["grib", "grb", "grib2", "grb2"])
def test_registered_extensions_are_content_checked(fmt):
    assert preview(message(), fmt=fmt)[0]["metadata"]["format"] == fmt


def changed(section, start, replacement):
    data = bytearray(message())
    offset = sections(data)[section][0] if section else 0
    data[offset + start:offset + start + len(replacement)] = replacement
    return bytes(data)


@pytest.mark.parametrize("section,start,value", [
    (3, 6, (2**32 - 1).to_bytes(4, "big")), (3, 30, (2**31).to_bytes(4, "big")),
    (3, 12, b"\0\x28"), (3, 71, b"\x10"), (3, 71, b"\x01"), (3, 38, b"\0\0\0\x01"),
    (3, 54, b"\0"), (4, 7, b"\0\x08"), (4, 5, b"\0\x01"), (4, 28, b"\x01"),
    (4, 10, b"\xfe"), (1, 10, b"\x01"), (5, 9, b"\0\x02"), (5, 19, b"\x40"),
    (5, 11, b"\x7f\x80\0\0"), (5, 15, b"\x7f\xff"), (5, 5, (2**32 - 1).to_bytes(4, "big")),
    (6, 5, b"\xfe"), (6, 5, b"\x01"),
])
def test_unsupported_or_declared_bombs_never_enter_native_decoder(monkeypatch, section, start, value):
    data = changed(section, start, value)
    def forbidden(*a, **kw): pytest.fail("native constructor must not see an unsupported declaration")
    monkeypatch.setattr(ec, "codes_new_from_message", forbidden)
    result, _ = preview(data)
    assert result["choices"]["messages"] == [] and result["metadata"]["skipped_messages"] == 1


@pytest.mark.parametrize("data", [b"not-grib" * 20, changed(0, 7, b"\x01"), changed(0, 8, b"\xff" * 8), changed(1, 0, b"\0\0\0\x01"), changed(3, 4, b"\x01"), message()[:-1], message()[:-4] + b"bad!"])
def test_bad_framing_and_truncation_fail_closed(data):
    with pytest.raises(GribWindowError): preview(data)


def test_bitmap_padding_must_be_zero_and_finite_missing_sentinel_is_not_a_mask():
    data = message(ni=3, nj=3, missing=(0,))
    position, length = sections(data)[6]
    invalid = bytearray(data); invalid[position + length - 1] |= 1
    assert preview(bytes(invalid))[0]["choices"]["messages"] == []
    # An explicitly present value equal to a common missing sentinel is retained.
    data = message(missing=(), value_offset=9719)
    tree, _ = preview(data)
    out, _ = preview(data, "image", {"message": tree["choices"]["messages"][0]["id"], "roi": [0, 0, 2, 1]})
    assert out["array"]["values"] == [9999, 10000]


def test_sparse_multi_gigabyte_source_only_fetches_selected_message_ranges():
    offset, data, reads = 2500000000, message(), []
    def read(o, n):
        reads.append((o, n))
        assert offset <= o and o + n <= offset + len(data)
        return data[o - offset:o - offset + n]
    result = grib_window_preview(read, offset + len(data), "grib2", "image", {"message": f"g-{offset:016x}", "roi": [1, 1, 2, 2]})
    assert result["array"]["values"] == [287, 288, 293, 294]
    assert sum(n for _, n in reads) == len(data)


@pytest.mark.parametrize("limits", [{"max_read_bytes": 1048577, "max_total_bytes": 8388608, "max_reads": 128}, {"max_read_bytes": 1048576, "max_total_bytes": 10, "max_reads": 128}, {"max_read_bytes": 1048576, "max_total_bytes": 8388608, "max_reads": 1}, {"max_read_bytes": True, "max_total_bytes": 8388608, "max_reads": 128}])
def test_budgets_are_not_widened_or_ignored(limits):
    with pytest.raises(GribWindowError): preview(message(), limits=limits)


def test_cancellation_short_read_and_filelike_fallback_are_not_retried():
    calls = []
    def cancelled(o, n): calls.append((o, n)); raise RuntimeError("cancel /private/hidden")
    with pytest.raises(GribWindowError) as error: grib_window_preview(cancelled, 2500000000, "grib2")
    assert len(calls) == 1 and "hidden" not in str(error.value)
    with pytest.raises(GribWindowError): grib_window_preview(lambda o, n: b"", 2500000000, "grib2")


def test_worker_and_host_schema_copies_and_actual_payload_agree():
    root = Path(__file__).resolve().parents[2]
    host = root / "backend/app/application/services/grib_window_visualization.py"
    if not host.exists(): pytest.skip("separate sandbox build context")
    assert host.read_bytes().rstrip() == (root / "sandbox/app/services/grib_window_payload.py").read_bytes().rstrip()
    spec = importlib.util.spec_from_file_location("isolated_grib_validator", host)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    for key, payload in browser_payloads().items():
        if key == "provenance": continue
        assert module.validate_grib_window_payload(payload, kind=payload["kind"], options=payload["selected"], fmt="grib2", source_bytes=payload["metadata"]["source_bytes"], read_bytes=payload["metadata"]["read_bytes"], read_requests=payload["metadata"]["read_requests"]) is payload
