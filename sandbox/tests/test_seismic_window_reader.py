import importlib.util
import io
import os
from pathlib import Path
import struct

import pytest

from app.services import seismic_window_reader as module
from app.services.seismic_window_payload import SeismicWindowError
from seismic_window_fixtures import mseed_bytes, official_mseed, sac_bytes


class Source:
    def __init__(self, value): self.value, self.reads = value, []
    def read(self, offset, length):
        assert 0 <= offset and 0 < length <= 1048576 and offset+length <= len(self.value)
        self.reads.append((offset, length)); return self.value[offset:offset+length]
    def preview(self, fmt="mseed", kind="tree", options=None):
        self.reads.clear(); result = module.seismic_window_preview(self.read, len(self.value), fmt, kind, options)
        assert result["metadata"]["read_bytes"] == sum(n for _, n in self.reads)
        assert result["metadata"]["read_requests"] == len(self.reads)
        return result


def _official():
    try: import obspy
    except ImportError:
        if os.getenv("AI_DATASEEK_REQUIRE_SEISMIC_REFERENCE") == "1": pytest.fail("ObsPy 1.4.2 independent reference is required")
        pytest.skip("optional independent ObsPy/libmseed reference; required in seismic acceptance gate")
    return obspy


@pytest.mark.parametrize("endian", [">", "<"])
@pytest.mark.parametrize("encoding", [1, 3, 4, 5])
def test_mseed_plain_encodings_and_header_only(endian, encoding):
    values = [1, -2, 3, 4] if encoding < 4 else [1.25, -2.5, float("nan"), 4.75]
    source = Source(mseed_bytes(values, endian=endian, encoding=encoding))
    tree = source.preview(); assert tree["metadata"]["decoded_samples"] == 0; assert max(o+n for o, n in source.reads) <= 56
    result = source.preview(kind="series", options={"record": 0, "start_sample": 1, "sample_count": 3})
    assert result["series"][0]["x"] == [.25, .5, .75]
    assert result["series"][0]["y"] == ([-2, 3, 4] if encoding < 4 else [-2.5, None, 4.75])
    assert result["choices"]["records"][0]["unit"] == "unknown"


@pytest.mark.parametrize("endian", [">", "<"])
@pytest.mark.parametrize("version", [6, 7])
def test_sac_binary_versions_are_raw_and_header_only(endian, version):
    source = Source(sac_bytes(endian=endian, version=version, interval=0.10000000000000002))
    tree = source.preview("sac"); r = tree["choices"]["records"][0]
    assert r["unit"] == "nm/s" and r["declared_scale"] == 2
    assert r["start_time"] == "2024-02-29T12:30:10.375000Z"
    assert all(o+n <= 632 or o >= 648 for o, n in source.reads)
    result = source.preview("sac", "series", {"record": 0, "start_sample": 0, "sample_count": 4})
    assert result["series"][0]["y"] == [1.25, -2.5, 0.0, 4.75]
    if version == 7: assert r["sample_interval"] == 0.10000000000000002


@pytest.mark.parametrize("encoding", ["INT16", "INT32", "FLOAT32", "FLOAT64", "STEIM1", "STEIM2"])
@pytest.mark.parametrize("endian", [">", "<"])
def test_libmseed_writer_and_reader_are_independent_oracles(encoding, endian):
    obspy = _official(); import numpy as np
    differences = np.tile([0, 1, -1, 7, -8, 31, -32, 127, -128, 511, -512, 8191, -8192, 32767, -32768, 100000, -100000], 20)
    values = np.cumsum(differences).astype("int32")
    if encoding == "INT16": values = (values % 20000).astype("int32")
    if encoding in ("FLOAT32", "FLOAT64"): values = values.astype("float64")/7
    value = official_mseed(values, encoding, endian); expected = obspy.read(io.BytesIO(value), format="MSEED")[0].data
    source = Source(value); tree = source.preview(); output = []
    for record in tree["choices"]["records"]:
        result = source.preview(kind="series", options={"record": record["id"], "start_sample": 0, "sample_count": record["samples"]})
        output.extend(result["series"][0]["y"])
        assert record["start_time"].startswith("2024-02-29T00:00:")
    np.testing.assert_array_equal(output, expected)
    assert source.preview()["choices"]["records"][0]["start_time"] == "2024-02-29T00:00:00.123456Z"


@pytest.mark.parametrize("encoding", ["STEIM1", "STEIM2"])
@pytest.mark.parametrize("count", [1, 2, 15, 100, 2000])
def test_libmseed_small_and_cross_frame_records(encoding, count):
    obspy = _official(); import numpy as np
    values = np.cumsum(np.random.default_rng(104).integers(-7, 8, count)).astype("int32")
    raw = official_mseed(values, encoding, record_bytes=4096)
    expected = obspy.read(io.BytesIO(raw))[0].data
    result = Source(raw).preview(kind="series", options={"record": 0, "start_sample": 0, "sample_count": count})
    assert result["series"][0]["y"] == expected.tolist()


def test_mseed_directory_pages_do_not_scan_skipped_slots_and_gap_not_merged():
    source = Source(b"".join(mseed_bytes(second=i if i < 2 else i+1) for i in range(24)))
    tree = source.preview(); records = tree["choices"]["records"]
    assert records[1]["relation"] == "continuous" and records[2]["relation"] == "gap" and records[2]["gap_seconds"] == 1
    assert tree["metadata"]["record_slots"] == 24 and tree["metadata"]["catalog_complete"] is False
    page = source.preview(options={"record_offset": 20, "record_limit": 4})
    assert [r["id"] for r in page["choices"]["records"]] == [20, 21, 22, 23]
    assert all(o < 56 or o >= 20*512 for o, _ in source.reads)
    result = source.preview(kind="series", options={"record": 20, "start_sample": 0, "sample_count": 4})
    assert len(result["series"]) == 1


@pytest.mark.parametrize("factor,multiplier,interval", [(2, 4, .125), (2, -4, 2), (-2, 4, .5), (-2, -4, 8)])
def test_sample_rate_factor_signs(factor, multiplier, interval):
    result = Source(mseed_bytes(factor=factor, multiplier=multiplier)).preview()
    assert result["choices"]["records"][0]["sample_interval"] == interval


@pytest.mark.parametrize("activity,expected", [(0, "2024-02-29T00:00:00.101050Z"), (2, "2024-02-29T00:00:00.100050Z")])
def test_explicit_timing_corrections_not_double_applied(activity, expected):
    result = Source(mseed_bytes(fraction=1000, correction=10, activity=activity, microseconds=50, block100_rate=12.5)).preview()
    assert result["choices"]["records"][0]["start_time"] == expected
    assert result["choices"]["records"][0]["sample_interval"] == .08


def test_sac_large_logical_source_reads_only_requested_range():
    header, size = sac_bytes(sample_count=300000000)
    reads = []
    def read(offset, length):
        reads.append((offset, length))
        if offset == 0: return header[:length]
        assert offset >= 632 and (offset-632) % 4 == 0
        return struct.pack("<"+str(length//4)+"f", *([1.25]*(length//4)))
    result = module.seismic_window_preview(read, size, "sac", "series", {"record": 0, "start_sample": 299999996, "sample_count": 4})
    assert result["series"][0]["y"] == [1.25]*4 and size > 1024**3
    assert reads == [(0, 632), (632+299999996*4, 16)]


@pytest.mark.parametrize("kind,options", [("tree", {"url": "bad"}), ("tree", {"record_offset": 0, "record_limit": 17}), ("series", {}),
    ("series", {"record": True, "start_sample": 0, "sample_count": 1}), ("series", {"record": 0, "start_sample": -1, "sample_count": 1}),
    ("series", {"record": 0, "start_sample": 0, "sample_count": 16385}), ("series", {"record": 0, "start_sample": 0, "sample_count": 1, "merge": True})])
def test_invalid_options_never_read(kind, options):
    reads = []
    with pytest.raises(SeismicWindowError): module.seismic_window_preview(lambda *args: reads.append(args), 512, "mseed", kind, options)
    assert not reads


@pytest.mark.parametrize("offset,values", [(6, b"V"), (20, b"\0\0"), (26, b"\x3c"), (36, b"\x10"), (39, b"\0"),
    (46, b"\0\x20"), (50, b"\0\x30"), (52, b"\x13"), (54, b"\x1f"), (55, b"\x01")])
def test_mseed_bad_headers_rejected(offset, values):
    raw = bytearray(mseed_bytes()); raw[offset:offset+len(values)] = values
    with pytest.raises(SeismicWindowError): Source(bytes(raw)).preview()


@pytest.mark.parametrize("field,value", [(6, 5), (9, -1), (15, 2), (35, 0), (16, 999)])
def test_sac_unsupported_or_inconsistent_headers(field, value):
    raw = bytearray(sac_bytes()); struct.pack_into("<i", raw, 280+field*4, value)
    with pytest.raises(SeismicWindowError): Source(bytes(raw)).preview("sac")


def test_pure_backend_contract_copy_and_real_payload_binding():
    path = Path(__file__).resolve().parents[2] / "backend/app/application/services/seismic_window_visualization.py"
    assert path.read_bytes() == (Path(module.__file__).parent / "seismic_window_payload.py").read_bytes()
    spec = importlib.util.spec_from_file_location("seismic_pure_backend", path); validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)
    for fmt, raw in [("mseed", mseed_bytes()), ("sac", sac_bytes())]:
        result = Source(raw).preview(fmt, "series", {"record": 0, "start_sample": 1, "sample_count": 2})
        assert validator.validate_seismic_window_payload(result, kind="series", options=result["selected"], fmt=fmt, source_bytes=len(raw), read_bytes=result["metadata"]["read_bytes"], read_requests=result["metadata"]["read_requests"]) is result


@pytest.mark.parametrize("encoding", ["STEIM1", "STEIM2"])
@pytest.mark.parametrize("corruption", ["reverse", "control", "sample_count", "offset", "frame_count"])
def test_steim_malformed_prechecks_or_integrity_fail(encoding, corruption):
    _official(); raw = bytearray(official_mseed([1, 2, 3, 4], encoding))
    offset = struct.unpack_from(">H", raw, 44)[0]
    if corruption == "reverse": struct.pack_into(">i", raw, offset+8, 999)
    elif corruption == "control": raw[offset] |= 0x40
    elif corruption == "sample_count": struct.pack_into(">H", raw, 30, 65535)
    elif corruption == "offset": struct.pack_into(">H", raw, 44, offset+1)
    else:
        block = struct.unpack_from(">H", raw, 46)[0]
        while struct.unpack_from(">H", raw, block)[0] != 1001: block = struct.unpack_from(">H", raw, block+2)[0]; assert block
        raw[block+7] = 255
    with pytest.raises(SeismicWindowError): Source(bytes(raw)).preview(kind="series", options={"record": 0, "start_sample": 0, "sample_count": 1})


@pytest.mark.parametrize("code,nibble", [(2, 0), (3, 3)])
def test_steim2_reserved_difference_packing_rejected(code, nibble):
    words = [0]*16; words[0] = code << 24; words[1] = 1; words[2] = 2; words[3] = nibble << 30
    with pytest.raises(SeismicWindowError): module._steim(struct.pack(">16I", *words), 2, "STEIM2", ">")


def test_steim_integration_uses_int32_wrap_and_reverse_check():
    words = [0]*16; words[0] = 1 << 24; words[1] = 2**31-1; words[2] = 2**31
    words[3] = 0x00010000  # first inter-record difference discarded, then +1
    assert module._steim(struct.pack(">16I", *words), 2, "STEIM1", ">") == [2**31-1, -2**31]


@pytest.mark.parametrize("mode", ["bytes", "reads", "short", "nonbytes", "offset", "cancel"])
def test_range_budget_and_cancellation_never_return_partial_payload(mode):
    raw = mseed_bytes(); reads = []
    def read(offset, length):
        reads.append((offset, length))
        if mode == "cancel": raise RuntimeError("/private/never-echo-this")
        if mode == "short": return raw[offset:offset+length-1]
        if mode == "nonbytes": return bytearray(raw[offset:offset+length])
        return raw[offset:offset+length]
    limits = {"max_read_bytes": 1048576, "max_total_bytes": 48 if mode == "bytes" else 8388608, "max_reads": 1 if mode == "reads" else 128}
    options = {"record": 0, "start_sample": 4 if mode == "offset" else 0, "sample_count": 1}
    with pytest.raises(SeismicWindowError) as exc: module.seismic_window_preview(read, len(raw), "mseed", "series", options, limits)
    assert "/private" not in str(exc.value)
    if mode in ("bytes", "reads", "short", "nonbytes", "cancel"): assert reads == [(0, 48)]


def test_current_directory_rejects_variable_record_sizes_and_missing_id_not_compared():
    raw = mseed_bytes()+mseed_bytes(record_bytes=256)+bytes(256)
    with pytest.raises(SeismicWindowError): Source(raw).preview()
    result = Source(mseed_bytes(station=b"<bad>")+mseed_bytes(station=b"<bad>", second=1)).preview()
    assert all(r["relation"] == "uncompared" for r in result["choices"]["records"])


@pytest.mark.parametrize("endian", [">", "<"])
def test_sac_independent_obspy_reader(endian):
    obspy = _official(); raw = sac_bytes(endian=endian)
    reference = obspy.read(io.BytesIO(raw), format="SAC")[0]
    result = Source(raw).preview("sac", "series", {"record": 0, "start_sample": 0, "sample_count": 4})
    assert result["series"][0]["y"] == reference.data.tolist()
    r = result["choices"]["records"][0]
    assert r["sample_interval"] == reference.stats.delta
    assert r["start_time"] == reference.stats.starttime.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_sac7_footer_delta_prechecked_before_samples(value):
    raw = bytearray(sac_bytes(version=7)); struct.pack_into("<d", raw, 648, value)
    with pytest.raises(SeismicWindowError): Source(bytes(raw)).preview("sac")


def test_known_record_slot_rejects_pointer_before_cross_slot_read():
    raw = bytearray(mseed_bytes()*3)
    struct.pack_into(">HH", raw, 512+44, 608, 600)
    raw[1112:1120] = struct.pack(">HH4B", 1000, 0, 3, 1, 9, 0)
    source = Source(bytes(raw))
    with pytest.raises(SeismicWindowError): source.preview(options={"record_offset": 1, "record_limit": 1})
    assert source.reads == [(0, 48), (48, 8), (512, 48)]


def test_unknown_first_record_bounds_b1000_discovery_before_read():
    raw = bytearray(mseed_bytes()*3); struct.pack_into(">HH", raw, 44, 608, 600)
    source = Source(bytes(raw))
    with pytest.raises(SeismicWindowError): source.preview()
    assert source.reads == [(0, 48)]


def test_first_b1000_bounds_following_blockette_before_read():
    raw = bytearray(mseed_bytes()*3); raw[39] = 2
    struct.pack_into(">H", raw, 44, 800); struct.pack_into(">H", raw, 50, 600)
    source = Source(bytes(raw))
    with pytest.raises(SeismicWindowError): source.preview()
    assert source.reads == [(0, 48), (48, 8)]


def test_large_mseed_logical_source_direct_record_selection_does_not_scan():
    raw = mseed_bytes(); size = 2*1024**3; reads = []
    def read(offset, length):
        reads.append((offset, length)); return raw[offset % 512:offset % 512+length]
    record = size//512-1
    result = module.seismic_window_preview(read, size, "mseed", "series", {"record": record, "start_sample": 1, "sample_count": 2})
    assert result["series"][0]["y"] == [-2, 3]
    assert reads == [(0, 48), (48, 8), (record*512, 48), (record*512+48, 8), (record*512+60, 8)]
    assert result["metadata"]["read_bytes"] == 120


def test_steim2_illegal_last_frame_tail_is_stricter_than_legacy_libmseed():
    obspy = _official()
    words = [0]*16; words[0] = (1 << 24) | (2 << 22); words[1] = 10; words[2] = 11; words[3] = 0x00010000
    frame = struct.pack(">16I", *words)  # word4 code2/dnib0 is reserved, after the requested sample count
    with pytest.raises(SeismicWindowError): module._steim(frame, 2, "STEIM2", ">")
    raw = bytearray(mseed_bytes([10, 11])); raw[52] = 11; struct.pack_into(">H", raw, 44, 64); raw[64:128] = frame
    # ObsPy 1.4.2 bundles legacy libmseed, which ignores this unused tail word.
    # Do not misreport the reference as rejecting it: our admitted subset is stricter.
    assert obspy.read(io.BytesIO(raw), format="MSEED")[0].data.tolist() == [10, 11]
    with pytest.raises(SeismicWindowError): Source(bytes(raw)).preview(kind="series", options={"record": 0, "start_sample": 0, "sample_count": 2})
