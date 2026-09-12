import base64
import builtins
import json
import math
import struct
import threading
import zlib

import pytest

from app.services import instrument_window_reader as reader
from instrument_window_fixtures import MemorySource, SparseSource, edf_bytes, edf_header, spe_bytes, spe_header


def preview(data, fmt="edf", kind="tree", options=None, **kwargs):
    source = data if hasattr(data, "read") else MemorySource(data)
    return reader.instrument_window_preview(source.read, source.size, fmt, kind, options, **kwargs), source


def png_pixels(result):
    png = base64.b64decode(result["data_base64"])
    size = struct.unpack_from(">I", png, 33)[0]
    return zlib.decompress(png[41:41 + size])


@pytest.mark.parametrize("fmt,maker", [("edf", edf_bytes), ("spe", spe_bytes)])
def test_structure_reads_only_headers_and_hides_all_free_metadata(fmt, maker):
    result, source = preview(maker(), fmt)
    assert result["metadata"]["frame_count"] == 2
    assert result["metadata"]["frame_shape"] == [3, 4]
    assert result["selected"] == {"frame": 0, "roi": [0, 0, 4, 3]}
    assert source.reads == ([(0, 512), (536, 512)] if fmt == "edf" else [(0, 4100)])
    assert result["metadata"]["read_bytes"] == result["metadata"]["header_bytes"]
    assert "PRIVATE" not in json.dumps(result) and "/private" not in json.dumps(result)
    assert result["sampled"] is True and "data_base64" not in result


@pytest.mark.parametrize("fmt,maker", [("edf", edf_bytes), ("spe", spe_bytes)])
def test_second_frame_narrow_roi_reads_only_two_selected_rows(fmt, maker):
    result, source = preview(maker(), fmt, "image", {"frame": 1, "roi": [1, 1, 2, 2]})
    base = 1048 if fmt == "edf" else 4124
    assert source.reads[-2:] == [(base + 10, 4), (base + 18, 4)]
    assert result["metadata"]["display_range"] == [105, 110]
    assert png_pixels(result) == bytes([0, 0, 51, 0, 204, 255])
    assert result["selected"] == {"frame": 1, "roi": [1, 1, 2, 2]}


def test_full_width_adjacent_rows_are_coalesced_without_outside_pixels():
    result, source = preview(spe_bytes(), "spe", "image", {"frame": 1, "roi": [0, 0, 4, 3]})
    assert source.reads == [(0, 4100), (4124, 24)]
    assert result["metadata"]["read_requests"] == 2


@pytest.mark.parametrize("datatype,code,values", [
    ("SignedByte", "b", [-128, -1, 0, 127]), ("UnsignedByte", "B", [0, 1, 254, 255]),
    ("SignedShort", "h", [-32768, -1, 0, 32767]), ("UnsignedShort", "H", [0, 1, 65534, 65535]),
    ("SignedInteger", "i", [-2**31, -1, 0, 2**31-1]), ("UnsignedInteger", "I", [0, 1, 2**32-2, 2**32-1]),
    ("FloatValue", "f", [-1.25, 0, 1, 2.5]), ("Double", "d", [-1e308, 0, 1, 1e308]),
])
@pytest.mark.parametrize("order", ["<", ">"])
def test_edf_supported_numeric_types_endian_and_extreme_finite_range(datatype, code, values, order):
    data = edf_bytes(2, 2, frames=1, dtype=code, datatype=datatype, values=values, order=order)
    result, _ = preview(data, "edf", "image", {"frame": 0, "roi": [0, 0, 2, 2]})
    assert result["metadata"]["display_range"] == [min(values), max(values)]
    assert result["metadata"]["byte_order"] == ("little" if order == "<" else "big")
    assert png_pixels(result)[1] == 0 and png_pixels(result)[-1] == 255
    assert result["sampled"] is False


@pytest.mark.parametrize("datatype,values", [(0, [-1.25, 0, 1, 2.5]), (1, [-2**31, 0, 1, 2**31-1]), (2, [-32768, 0, 1, 32767]), (3, [0, 1, 2, 65535])])
def test_spe_exact_supported_datatype_codes(datatype, values):
    result, _ = preview(spe_bytes(2, 2, frames=1, datatype=datatype, values=values), "spe", "image", {"frame": 0, "roi": [0, 0, 2, 2]})
    assert result["metadata"]["display_range"] == [min(values), max(values)]


@pytest.mark.parametrize("values,bounds,invalid", [([float("nan"), float("inf"), -float("inf"), 1.0], [1, 1], 3), ([float("nan")] * 4, [0, 0], 4), ([7.0] * 4, [7, 7], 0)])
def test_nonfinite_and_constant_values_have_explicit_policy(values, bounds, invalid):
    result, _ = preview(spe_bytes(2, 2, frames=1, datatype=0, values=values), "spe", "image", {"frame": 0, "roi": [0, 0, 2, 2]})
    assert result["metadata"]["display_range"] == bounds
    assert result["metadata"]["invalid_pixels"] == invalid
    assert set(png_pixels(result)) == {0}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("fmt", ["edf", "spe"])
def test_sparse_multi_gigabyte_source_only_reads_header_and_high_offset_roi(fmt):
    width, height, frames = 50000, 25000, 2
    frame_bytes = width * height * 2
    if fmt == "edf":
        size = 2 * (512 + frame_bytes)
        source = SparseSource(size, [(0, edf_header(width, height)), (512 + frame_bytes, edf_header(width, height, frame=1))])
        selected_base = 1024 + frame_bytes
    else:
        size = 4100 + frames * frame_bytes
        source = SparseSource(size, [(0, spe_header(width, height, frames=frames))])
        selected_base = 4100 + frame_bytes
    result, source = preview(source, fmt, "image", {"frame": 1, "roi": [49998, 24999, 2, 1]})
    assert source.reads[-1] == (selected_base + (24999 * width + 49998) * 2, 4)
    assert result["metadata"]["source_bytes"] > 5_000_000_000
    assert sum(length for _, length in source.reads) == result["metadata"]["header_bytes"] + 4
    assert max(offset for offset, _ in source.reads) > 4_000_000_000


@pytest.mark.parametrize("kind,options", [("image", {}), ("image", {"frame": True, "roi": [0, 0, 1, 1]}),
    ("image", {"frame": -1, "roi": [0, 0, 1, 1]}), ("image", {"frame": 1000000, "roi": [0, 0, 1, 1]}),
    ("image", {"frame": 0, "roi": [0, 0, 1025, 1]}), ("image", {"frame": 0, "roi": [0, 0, True, 1]}),
    ("image", {"frame": 0, "roi": [0, 0, 0, 1]}), ("image", {"frame": 0, "roi": [0.0, 0, 1, 1]}),
    ("tree", {"path": "/private"}), ("tree", {"frame": 0}), ("table", {})])
def test_option_escalation_rejected_before_any_read(kind, options):
    source = MemorySource(spe_bytes())
    with pytest.raises(reader.InstrumentPreviewError):
        preview(source, "spe", kind, options)
    assert source.reads == []


@pytest.mark.parametrize("options", [{"frame": 2, "roi": [0, 0, 1, 1]}, {"frame": 0, "roi": [3, 2, 2, 1]}, {"frame": 0, "roi": [0, 2, 1, 2]}])
def test_out_of_bounds_selection_rejected_before_pixel_reads(options):
    source = MemorySource(spe_bytes())
    with pytest.raises(reader.InstrumentPreviewError):
        preview(source, "spe", "image", options)
    assert source.reads == [(0, 4100)]


@pytest.mark.parametrize("fields", [{"Compression": "GZIP"}, {"EDF_BinaryFileName": "/private/data.bin"},
    {"EDF_BinaryFilePosition": "0"}, {"Dim_3": "1"}, {"Dim_9": "2"}, {"EDF_DataFormatVersion": "1.0"},
    {"DataType": "Unsigned64"}, {"DataType": "UNKNOWN"}, {"ByteOrder": "auto"}, {"Size": "22"},
    {"Size": "999999999999999999999999"}, {"EDF_BinarySize": "25"}, {"Dim_1": "0"}, {"Dim_2": "100001"},
    {"EDF_HeaderSize": "1024"}, {"HeaderID": "bad\x00"}, {"dim_1": "4"}])
def test_edf_unsupported_layouts_overflows_duplicates_and_external_paths_rejected(fields):
    source = MemorySource(edf_bytes(frames=1, fields=fields))
    with pytest.raises(reader.InstrumentPreviewError):
        preview(source)
    assert source.reads == [(0, 512)]


@pytest.mark.parametrize("data", [b"0       " + b" " * 504, b"{\n" + b" " * 510, b"{\nA=1;\n}\n" + b" " * 500,
    edf_bytes(frames=1)[:-1], edf_bytes(frames=1) + b"x", edf_bytes(frames=1) + edf_bytes(2, 2, frames=1)])
def test_edf_signal_header_unclosed_header_truncation_trailing_and_heterogeneous_frames_rejected(data):
    with pytest.raises(reader.InstrumentPreviewError):
        preview(data)


@pytest.mark.parametrize("fields", [{1992: ("f", 3.0)}, {1992: ("f", 0.0)}, {1992: ("f", float("nan"))},
    {678: ("Q", 4200)}, {1446: ("i", -1)}, {1446: ("i", 2**31-1)}, {108: ("h", 4)}, {108: ("h", 8)},
    {42: ("H", 0)}, {656: ("H", 65535)}, {1510: ("h", 2)}, {1510: ("h", -1)}, {1510: ("h", 1)},
    {600: ("H", 1)}, {98: ("h", 1)}, {100: ("h", -1)}, {102: ("h", 1)}, {104: ("h", 1)}])
def test_spe_malformed_or_unsupported_fixed_header_rejected(fields):
    source = MemorySource(spe_bytes(fields=fields))
    with pytest.raises(reader.InstrumentPreviewError):
        preview(source, "spe")
    assert source.reads == [(0, 4100)]


def test_spe_single_explicit_sensor_roi_matches_stored_dimensions():
    fields = {1510: ("h", 1), 1512: ("H", 11), 1514: ("H", 18), 1516: ("H", 2),
              1518: ("H", 21), 1520: ("H", 29), 1522: ("H", 3)}
    result, _ = preview(spe_bytes(fields=fields), "spe")
    assert result["metadata"]["frame_shape"] == [3, 4]


@pytest.mark.parametrize("name,value", [("max_total_bytes", 4103), ("max_reads", 1), ("max_read_bytes", 4099)])
def test_declared_read_budgets_stop_before_pixel_io(name, value):
    limits = {"max_read_bytes": 1024**2, "max_total_bytes": 32 * 1024**2, "max_reads": 2048}
    limits[name] = value
    source = MemorySource(spe_bytes())
    with pytest.raises(reader.InstrumentPreviewError):
        preview(source, "spe", "image", {"frame": 0, "roi": [0, 0, 2, 2]}, limits=limits)
    assert len(source.reads) <= 1


@pytest.mark.parametrize("limits", [{}, {"max_read_bytes": True, "max_total_bytes": 1, "max_reads": 1},
    {"max_read_bytes": 2 * 1024**2, "max_total_bytes": 32 * 1024**2, "max_reads": 2048},
    {"max_read_bytes": 1024**2, "max_total_bytes": 32 * 1024**2 + 1, "max_reads": 2048},
    {"max_read_bytes": 1024**2, "max_total_bytes": 32 * 1024**2, "max_reads": 2049}])
def test_caller_cannot_escalate_hard_limits(limits):
    source = MemorySource(spe_bytes())
    with pytest.raises(reader.InstrumentPreviewError):
        preview(source, "spe", limits=limits)
    assert not source.reads


@pytest.mark.parametrize("phase", ["before", "header", "pixel"])
def test_cancellation_fences_prevent_any_completed_result(phase):
    stop = threading.Event()
    source = MemorySource(spe_bytes())
    if phase == "before":
        stop.set()
    else:
        source.after_read = lambda offset, _length: stop.set() if (phase == "header" or offset >= 4100) else None
    with pytest.raises(reader.InstrumentPreviewError, match="取消"):
        preview(source, "spe", "image", {"frame": 0, "roi": [0, 0, 2, 2]}, cancelled=stop)
    assert len(source.reads) == {"before": 0, "header": 1, "pixel": 2}[phase]


def test_no_filesystem_open_or_write_is_used(monkeypatch):
    data = spe_bytes()
    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: pytest.fail("instrument parser touched filesystem"))
    result, _ = preview(data, "spe", "image", {"frame": 0, "roi": [0, 0, 2, 2]})
    assert result["metadata"]["read_bytes"] == 4108


@pytest.mark.parametrize("size", [0, True, -1, 8 * 1024**3 + 1])
def test_source_size_rejected_without_read(size):
    with pytest.raises(reader.InstrumentPreviewError):
        reader.instrument_window_preview(lambda *_: pytest.fail("read"), size, "spe")


def test_short_range_never_padded_or_parsed():
    with pytest.raises(reader.InstrumentPreviewError, match="长度"):
        reader.instrument_window_preview(lambda _offset, length: b"\x00" * (length - 1), 4104, "spe")
