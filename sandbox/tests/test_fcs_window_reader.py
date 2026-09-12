import copy
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import pytest
from app.services import fcs_window_reader as module
from app.services.fcs_window_payload import FcsWindowError
from fcs_window_fixtures import fcs_bytes, official_fcs, SparseFcs


def reference():
    try: import flowio
    except ImportError:
        if os.getenv("AI_DATASEEK_REQUIRE_FCS_REFERENCE") == "1": pytest.fail("FlowIO 1.4.0 is required for independent FCS reference")
        pytest.skip("optional FlowIO oracle; mandatory for FCS acceptance")
    return flowio


class Source:
    def __init__(self, value): self.value, self.reads = value, []
    def read(self, o, n): self.reads.append((o, n)); assert 0 <= o < o+n <= len(self.value); return self.value[o:o+n]
    def preview(self, kind="tree", options=None, limits=None):
        self.reads.clear(); r = module.fcs_window_preview(self.read, len(self.value), "fcs", kind, options, limits)
        assert r["metadata"]["read_bytes"] == sum(n for _, n in self.reads) and r["metadata"]["read_requests"] == len(self.reads)
        return r


SCATTER = {"view": "scatter", "channels": [0, 1], "event_offset": 1, "event_count": 2}
HISTOGRAM = {"view": "histogram", "channels": [1], "event_offset": 0, "event_count": 4, "bins": 4}


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("version", ["3.0", "3.1"])
@pytest.mark.parametrize("datatype,bits", [("I", 8), ("I", 16), ("I", 32), ("F", 32), ("D", 64)])
def test_actual_binary_subset_matches_independent_flowio(endian, version, datatype, bits):
    flowio = reference()
    rows = [[0, 2**bits-1], [1, 2**(bits-1)], [2**bits-2, 7], [42, 3]] if datatype == "I" else [[-1.25, 1e6], [2.5, -3.25], [0, 7], [42, .125]]
    raw = fcs_bytes(rows, endian=endian, version=version, datatype=datatype, bits=bits)
    expected = list(flowio.FlowData(io.BytesIO(raw)).events)
    source = Source(raw); tree = source.preview(); start = int(raw[26:34]); assert max(o+n for o, n in source.reads) <= start
    assert tree["metadata"]["decoded_values"] == 0
    result = source.preview("series", SCATTER)
    assert result["series"][0]["y"] == [expected[2], expected[4]] and result["series"][1]["y"] == [expected[3], expected[5]]
    assert result["series"][0]["x"] == [1, 2]


def test_official_writer_metadata_utf8_calibration_and_compensation_are_inert():
    reference(); raw = official_fcs(); source = Source(raw); r = source.preview("series", SCATTER)
    assert r["series"][0]["y"] == [-2.5, 0]  # not divided by gain 2 or multiplied by calibration 3
    c = r["choices"]["channels"][0]
    assert c["stain"] == "散射" and c["gain"] == 2 and c["calibration"] == {"factor": 3, "unit": "MESF"}
    assert r["metadata"]["compensation"]["spillover"] == {"channels": [0, 1], "matrix": [[1, .1], [.03, 1]]}
    assert "private-patient" not in json.dumps(r) and "/private" not in json.dumps(r)


@pytest.mark.parametrize("datatype", ["F", "D"])
def test_nonfinite_values_become_null_without_clipping_float_range(datatype):
    r = Source(fcs_bytes([[float("nan"), 1], [2, float("inf")], [-1e6, -float("inf")], [1e9, 5]], datatype=datatype)).preview("series", {**SCATTER, "event_offset": 0, "event_count": 4})
    assert r["series"][0]["y"] == [None, 2, -1e6, 1e9] and r["series"][1]["y"] == [1, None, None, 5]
    assert r["metadata"]["nonfinite_values"] == 3 and r["metadata"]["plottable_events"] == 1


def test_large_source_uses_text_offsets_and_only_requested_rows():
    flowio = reference(); prefix, size = fcs_bytes(virtual_total=200000000)
    oracle = SparseFcs(prefix, size); fd = flowio.FlowData(oracle, only_text=True)
    assert fd.event_count == 200000000 and int(prefix[26:34]) == int(prefix[34:42]) == 0
    source = SparseFcs(prefix, size)
    def read(o, n): source.seek(o); return source.read(n)
    r = module.fcs_window_preview(read, size, "fcs", "series", {**SCATTER, "event_offset": 199999998})
    assert r["series"][0]["y"] == [0, 0] and r["metadata"]["source_bytes"] > 1024**3
    assert r["metadata"]["read_bytes"] == len(prefix)+16 and source.reads[-1] == (len(prefix)+199999998*8, 16)
    assert len(source.reads) == 4


@pytest.mark.parametrize("delimiter", ["|", "/", "!", "\x1c"])
def test_delimiter_escaping_and_case_insensitive_keywords(delimiter):
    name = "CD3"+delimiter+"PE" if ord(delimiter) >= 32 else "CD3|PE"
    # slash inside a label is harmless; absolute source paths are not admitted labels.
    r = Source(fcs_bytes(names=["FSC-A", name], delimiter=delimiter)).preview()
    assert r["choices"]["channels"][1]["name"] == name
    raw = fcs_bytes().replace(b"$PAR", b"$pAr")
    assert Source(raw).preview()["metadata"]["channel_count"] == 2


@pytest.mark.parametrize("metadata", [{"$NEXTDATA": "1000"}, {"$BEGINSTEXT": "1"}, {"$ENDSTEXT": "1"}, {"$BEGINANALYSIS": "1"}, {"$ENDANALYSIS": "1"},
    {"$MODE": "C"}, {"$DATATYPE": "A"}, {"$BYTEORD": "2,1"}, {"$PAR": "129"}, {"$TOT": "0"}, {"$TOT": " 4"}, {"$TOT": "+4"}, {"$TOT": "4.0"},
    {"$P1B": "16"}, {"$P1E": "4,0"}, {"$P1E": "4,1"}, {"$P1G": "0"}, {"$P1N": "CD3-A"}, {"$P1N": "/private/secret"}, {"$P1N": "x<script>"},
    {"$P1N": "A,B"}, {"$P1N": "A\u202eB"}, {"$P1CALIBRATION": "NaN,MESF"}, {"$P1D": "Logicle,4,1"}, {"$P1D": "Linear,5,1"},
    {"$UNICODE": "utf-8"}, {"$P3N": "extra"}, {"$P01N": "alias"}, {"$SPILLOVER": "2,FSC-A,CD3-A,1,0,0"},
    {"$SPILLOVER": "2,FSC-A,missing,1,0,0,1"}, {"$SPILLOVER": "2,FSC-A,FSC-A,1,0,0,1"}, {"$TIMESTEP": "NaN"}])
def test_unsupported_or_invalid_metadata_never_reads_events(metadata):
    raw = fcs_bytes(metadata=metadata); source = Source(raw)
    with pytest.raises(FcsWindowError): source.preview("series", SCATTER)
    assert all(o+n <= int(raw[26:34]) for o, n in source.reads)


@pytest.mark.parametrize("bits,ranges", [(16, {"$P1R": "1024"}), (32, {"$P2R": "1000"}), ([8, 16], {})])
def test_integer_mask_or_mixed_width_not_silently_applied(bits, ranges):
    raw = fcs_bytes([[1, 2]]*4, datatype="I", bits=bits, metadata=ranges)
    with pytest.raises(FcsWindowError): Source(raw).preview()


@pytest.mark.parametrize("extra", [[("$par", "2")], [("NOTE", "a"), ("note", "b")], [("$P1N", "different")]])
def test_duplicate_case_insensitive_keywords_rejected(extra):
    with pytest.raises(FcsWindowError): Source(fcs_bytes(extra_pairs=extra)).preview()


@pytest.mark.parametrize("corruption", ["version", "space", "header-offset", "end-exclusive", "trailer", "text-overlap", "invalid-utf8", "empty-value"])
def test_offset_and_text_boundaries(corruption):
    raw = bytearray(fcs_bytes()); begin = int(raw[26:34])
    if corruption == "version": raw[5] = ord("2")
    elif corruption == "space": raw[6] = 0
    elif corruption == "header-offset": raw[26:34] = str(begin+1).rjust(8).encode()
    elif corruption == "end-exclusive": raw[34:42] = str(len(raw)).rjust(8).encode()
    elif corruption == "trailer": raw += b"CRC!"
    elif corruption == "text-overlap": raw[18:26] = str(begin).rjust(8).encode()
    elif corruption == "invalid-utf8": raw[raw.index(b"FSC-A")] = 255
    else: raw = raw.replace(b"|FSC-A|", b"||" )
    with pytest.raises(FcsWindowError): Source(bytes(raw)).preview()


@pytest.mark.parametrize("kind,options", [("tree", {"event_offset": 0}), ("series", {}), ("series", {**SCATTER, "channels": [0, 0]}), ("series", {**SCATTER, "channels": [0, True]}),
    ("series", {**SCATTER, "event_count": 8193}), ("series", {**SCATTER, "event_offset": -1}), ("series", {**HISTOGRAM, "bins": 129}), ("series", {**SCATTER, "sql": "bad"})])
def test_bad_options_fail_before_source_reads(kind, options):
    calls = []
    with pytest.raises(FcsWindowError): module.fcs_window_preview(lambda *args: calls.append(args), 1024, "fcs", kind, options)
    assert not calls


def test_window_bounds_and_budget_checked_before_event_io():
    raw = fcs_bytes(); start = int(raw[26:34]); source = Source(raw)
    for options in [{**SCATTER, "event_offset": 3}, {**SCATTER, "channels": [0, 2]}]:
        with pytest.raises(FcsWindowError): source.preview("series", options)
        assert max(o+n for o, n in source.reads) <= start
    with pytest.raises(FcsWindowError): source.preview("series", SCATTER, {"max_read_bytes": 1048576, "max_total_bytes": start+1, "max_reads": 128})
    assert max(o+n for o, n in source.reads) <= start


def test_callback_error_scrubbed_and_no_partial_payload():
    def read(*_): raise RuntimeError("/private/source-path")
    with pytest.raises(FcsWindowError) as exc: module.fcs_window_preview(read, 1024, "fcs")
    assert "/private" not in str(exc.value)


def test_pure_validator_copy_identical_and_strict_real_bindings():
    path = Path(__file__).resolve().parents[2]/"backend/app/application/services/fcs_window_visualization.py"
    assert path.read_bytes() == (Path(module.__file__).parent/"fcs_window_payload.py").read_bytes()
    spec = importlib.util.spec_from_file_location("fcs_pure_backend", path); validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)
    r = Source(fcs_bytes()).preview("series", SCATTER); m = r["metadata"]
    assert validator.validate_fcs_window_payload(r, kind="series", options=SCATTER, fmt="fcs", source_bytes=m["source_bytes"], read_bytes=m["read_bytes"], read_requests=m["read_requests"]) is r
    for kw in [{"source_bytes": m["source_bytes"]+1}, {"fmt": "edf"}, {"read_bytes": 1}, {"read_requests": 1}, {"kind": "tree"}, {"options": {**SCATTER, "channels": [1, 0]}}]:
        with pytest.raises(validator.FcsWindowError): validator.validate_fcs_window_payload(r, **kw)


def test_wide_event_rows_budgeted_before_read_not_just_selected_output():
    prefix, size = fcs_bytes([[0]*128], datatype="D", virtual_total=8192)
    source = SparseFcs(prefix, size)
    def read(o, n): source.seek(o); return source.read(n)
    with pytest.raises(FcsWindowError): module.fcs_window_preview(read, size, "fcs", "series", {**SCATTER, "event_offset": 0, "event_count": 8192})
    assert all(o+n <= len(prefix) for o, n in source.reads)


def test_wide_rows_streamed_in_bounded_whole_row_chunks():
    prefix, size = fcs_bytes([[0]*64], datatype="D", virtual_total=4096)
    source = SparseFcs(prefix, size)
    def read(o, n): source.seek(o); return source.read(n)
    r = module.fcs_window_preview(read, size, "fcs", "series", {**SCATTER, "event_offset": 0, "event_count": 4096})
    assert source.reads[-2:] == [(len(prefix), 1048576), (len(prefix)+1048576, 1048576)]
    assert r["metadata"]["scanned_event_bytes"] == 2097152 and r["metadata"]["decoded_bytes"] == 65536
    assert r["series"][0]["x"][-1] == 4095


@pytest.mark.parametrize("raw", [b"|$PAR||", b"|$PAR|2|$par|2|", b"|$PAR|2", b"|$PAR|2|BAD|\xff|"])
def test_text_scanner_rejects_ambiguity_duplicate_and_invalid_utf8(raw):
    with pytest.raises((FcsWindowError, UnicodeDecodeError)): module._pairs(raw, "3.1")


def test_over_budget_text_rejected_before_allocation_or_read():
    raw = bytearray(fcs_bytes()); raw[18:26] = b"  300000"; calls = []
    def read(o, n): calls.append((o, n)); return bytes(raw[o:o+n])
    with pytest.raises(FcsWindowError): module.fcs_window_preview(read, 400000, "fcs")
    assert calls == [(0, 58)]
