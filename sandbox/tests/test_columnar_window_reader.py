import io
import importlib.util
import struct
from pathlib import Path
from decimal import Decimal

import pyarrow as pa
import pyarrow.feather as feather
import pyarrow.parquet as pq
import pytest

from app.services import columnar_window_reader as module


def data_table():
    return pa.table({"identity": pa.array([2**63 - 1, -2**63, None, 42], type=pa.int64()),
        "decimal": pa.array([Decimal("1234567890123456.1200"), Decimal("-0.0100"), None, Decimal("0.0000")], type=pa.decimal128(20, 4)),
        "label": ["科学 <data>", "", None, "test"], "value": [1.0000000000000002, float("nan"), None, 4.5], "ok": [True, False, None, True]})


def fixture(fmt="parquet", *, compression=None, table=None, groups=2):
    target = io.BytesIO(); table = data_table() if table is None else table
    if fmt == "parquet": pq.write_table(table, target, compression=compression, row_group_size=groups, write_page_checksum=True)
    elif fmt == "feather": feather.write_feather(table, target, compression=compression or "uncompressed", chunksize=groups)
    else:
        with pa.ipc.new_file(target, table.schema, options=pa.ipc.IpcWriteOptions(compression=compression)) as writer:
            for batch in table.to_batches(max_chunksize=groups): writer.write_batch(batch)
    return target.getvalue()


class Source:
    def __init__(self, value, gap=0):
        self.value, self.gap, self.reads = value, gap, []
        tail = 8 if value[:4] == b"PAR1" else 10
        self.footer = len(value) - tail - int.from_bytes(value[-tail:-tail + 4], "little")
        self.size = len(value) + gap
    def read(self, start, length):
        assert 0 <= start < self.size and 0 < length <= 1024**2 and start + length <= self.size
        self.reads.append((start, length))
        end = start + length; result = b""
        if start < self.footer: result += self.value[start:min(end, self.footer)]
        result += b"\0" * max(0, min(end, self.footer + self.gap) - max(start, self.footer))
        if end > self.footer + self.gap: result += self.value[max(start, self.footer + self.gap) - self.gap:end - self.gap]
        return result
    def preview(self, fmt="parquet", kind="tree", options=None, limits=None):
        self.reads.clear()
        result = module.columnar_window_preview(self.read, self.size, fmt, kind, options, limits)
        assert result["metadata"]["read_bytes"] == sum(n for _, n in self.reads)
        assert result["metadata"]["read_requests"] == len(self.reads)
        return result


@pytest.mark.parametrize("fmt,compression", [("parquet", None), ("parquet", "snappy"), ("parquet", "gzip"), ("parquet", "zstd"), ("parquet", "lz4"), ("arrow", None), ("arrow", "lz4"), ("arrow", "zstd"), ("feather", "lz4")])
def test_real_columnar_tree_and_precise_values(fmt, compression):
    source = Source(fixture(fmt, compression=compression))
    tree = source.preview(fmt)
    assert tree["metadata"]["total_rows"] == 4
    assert tree["metadata"]["decoded_bytes"] == 0
    assert tree["metadata"]["scan_rows"] == 0
    options = {"columns": [1, 0, 3, 2, 4], "row_offset": 0, "row_limit": 200}
    value = source.preview(fmt, "table", options)
    assert value["selected"] == options
    assert value["table"]["rows"] == [["1234567890123456.1200", str(2**63 - 1), 1.0000000000000002, "科学 <data>", True], ["-0.0100", str(-2**63), None, "", False], [None] * 5, ["0.0000", "42", 4.5, "test", True]]
    assert value["metadata"]["nonfinite_values"] == 1
    assert value["sampled"] is False


@pytest.mark.parametrize("fmt", ["parquet", "arrow", "feather"])
def test_greater_than_whole_file_limit_is_truly_range_read(fmt):
    source = Source(fixture(fmt), gap=128 * 1024**2)
    tree = source.preview(fmt)
    assert tree["metadata"]["source_bytes"] > 64 * 1024**2
    assert tree["metadata"]["read_bytes"] < 20000
    options = {"columns": [0], "row_offset": 2, "row_limit": 1}
    result = source.preview(fmt, "table", options)
    assert result["table"]["rows"] == [[None]]
    assert result["metadata"]["read_bytes"] < 20000
    assert result["metadata"]["groups_read"] == 1
    assert result["metadata"]["scan_rows"] == 2


@pytest.mark.parametrize("kind,options", [("tree", {"x": 1}), ("table", {}), ("table", {"columns": [True], "row_offset": 0, "row_limit": 1}), ("table", {"columns": [0, 0], "row_offset": 0, "row_limit": 1}), ("table", {"columns": [0], "row_offset": -1, "row_limit": 1}), ("table", {"columns": [0], "row_offset": 0, "row_limit": 201}), ("table", {"columns": [0], "row_offset": 0, "row_limit": 1, "sql": "SELECT 1"})])
def test_options_rejected_before_range(kind, options):
    reads = []
    with pytest.raises(module.ColumnarWindowError): module.columnar_window_preview(lambda *args: reads.append(args), 500, "parquet", kind, options)
    assert reads == []


@pytest.mark.parametrize("fmt", ["parquet", "arrow", "feather"])
@pytest.mark.parametrize("table", [pa.table({"a": [[1, 2]]}), pa.table({"a": [b"binary"]}), pa.table({"a": pa.array([0], type=pa.timestamp("ns"))})])
def test_nested_binary_and_temporal_types_rejected(fmt, table):
    with pytest.raises(module.ColumnarWindowError): Source(fixture(fmt, table=table)).preview(fmt)


def test_ipc_stream_not_accepted_as_file():
    target = io.BytesIO()
    with pa.ipc.new_stream(target, data_table().schema) as writer: writer.write_table(data_table())
    value = target.getvalue()
    with pytest.raises(module.ColumnarWindowError): module.columnar_window_preview(lambda o, n: value[o:o+n], len(value), "arrow")


def test_empty_table_is_metadata_and_empty_window():
    source = Source(fixture(table=pa.table({"a": pa.array([], type=pa.int64())})))
    assert source.preview()["metadata"]["total_rows"] == 0
    assert source.preview(kind="table", options={"columns": [0], "row_offset": 0, "row_limit": 200})["table"]["rows"] == []


def test_range_budget_and_short_reads_fail_closed():
    source = Source(fixture())
    with pytest.raises(module.ColumnarWindowError): source.preview(limits={"max_read_bytes": 16, "max_total_bytes": 32, "max_reads": 2})
    with pytest.raises(module.ColumnarWindowError): module.columnar_window_preview(lambda o, n: b"x" * (n-1), source.size, "parquet")


def test_row_group_scan_budget_is_independent_of_200_output_rows():
    source = Source(fixture(table=pa.table({"a": pa.array([0] * (module.MAX_SCAN_ROWS + 1), type=pa.int8())}), groups=module.MAX_SCAN_ROWS + 1))
    with pytest.raises(module.ColumnarWindowError): source.preview(kind="table", options={"columns": [0], "row_offset": 0, "row_limit": 1})


@pytest.mark.parametrize("codec", [None, "snappy", "gzip", "zstd", "lz4_raw", "lz4"])
def test_actual_compressed_stream_must_match_bounded_declared_size(codec):
    value = b"scientific-data" * 500
    compressed = value if codec is None else pa.Codec(codec).compress(value).to_pybytes()
    module._decompressed(compressed, len(value), codec)
    for size in (len(value)-1, len(value)+1, module.MAX_BLOCK + 1):
        with pytest.raises((module.ColumnarWindowError, pa.ArrowException)): module._decompressed(compressed, size, codec)


@pytest.mark.parametrize("compression", [None, "gzip", "snappy", "zstd", "lz4"])
def test_parquet_v2_pages_and_dictionary_are_preflighted(compression):
    target = io.BytesIO()
    pq.write_table(data_table(), target, compression=compression, data_page_version="2.0", row_group_size=2, write_page_checksum=True)
    result = Source(target.getvalue()).preview(kind="table", options={"columns": [0, 1, 2, 3, 4], "row_offset": 1, "row_limit": 2})
    assert result["table"]["rows"][0][0] == str(-2**63)
    assert result["metadata"]["groups_read"] == 2


@pytest.mark.parametrize("fmt", ["parquet", "arrow", "feather"])
def test_metadata_inspection_cannot_decode_any_data(monkeypatch, fmt):
    source = Source(fixture(fmt, compression="zstd"))
    monkeypatch.setattr(module, "_decompressed", lambda *args: pytest.fail("tree decoded a page or buffer"))
    result = source.preview(fmt)
    assert result["metadata"]["scan_rows"] == 0
    if fmt == "parquet": assert all(o + n <= 4 or o >= source.footer for o, n in source.reads)


def _uvar(value):
    result = bytearray()
    while value > 127: result.append((value & 127) | 128); value >>= 7
    result.append(value); return bytes(result)


def _page_header(raw):
    result, last = b"", 0
    for key, value in raw.items():
        typ = 12 if isinstance(value, dict) else 1 if value is True else 2 if value is False else 5
        result += bytes([((key-last) << 4) | typ]); last = key
        if isinstance(value, dict): result += _page_header(value)
        elif type(value) is int: result += _uvar((value << 1) ^ (value >> 63))
    return result + b"\0"


@pytest.mark.parametrize("mutation", ["declared_size", "crc", "bad_page_type", "decoded_budget", "bad_values"])
def test_parquet_page_tampering_rejected(mutation):
    value = bytearray(fixture(compression="gzip")); parser = module.Compact(value[4:]); header = parser.value(12)
    if mutation == "declared_size": header[2] += 1
    if mutation == "crc": header[4] = header[4] ^ 1
    if mutation == "bad_page_type": header[1] = 1
    if mutation == "decoded_budget": header[2] = module.MAX_BLOCK + 1
    if mutation == "bad_values": header[7][1] = module.MAX_SCAN_ROWS + 1
    replacement = _page_header(header)
    if len(replacement) != parser.pos:
        # A changed header cannot steal bytes from the first page's body.
        replacement = replacement[:parser.pos].ljust(parser.pos, b"\0")
    value[4:4+parser.pos] = replacement
    with pytest.raises(module.ColumnarWindowError): Source(bytes(value)).preview(kind="table", options={"columns": [0], "row_offset": 0, "row_limit": 1})


@pytest.mark.parametrize("prefix", [4194305, -2, 1])
def test_arrow_compressed_buffer_length_tampering_rejected(prefix):
    value = bytearray(fixture("arrow", compression="zstd"))
    length = int.from_bytes(value[-10:-6], "little"); footer = module.Flat(value[-10-length:-10]); root = footer.root()
    block = footer.vector(root, 3, 24, 64)[0]
    offset, meta_size = footer.num(block, "q"), footer.num(block+8, "i")
    message = module.Flat(value[offset+8:offset+meta_size]); batch = message.table(message.root(), 2)
    item = next(v for v in message.vector(batch, 2, 16, 384) if message.num(v+8, "q") > 8)
    start = offset + meta_size + message.num(item, "q")
    struct.pack_into("<q", value, start, prefix)
    with pytest.raises(module.ColumnarWindowError): Source(bytes(value)).preview("arrow", "table", {"columns": [0], "row_offset": 0, "row_limit": 1})


@pytest.mark.parametrize("fmt", ["parquet", "arrow", "feather"])
def test_real_payloads_pass_backend_pure_validator(fmt):
    path = Path(__file__).resolve().parents[2] / "backend/app/application/services/columnar_window_visualization.py"
    spec = importlib.util.spec_from_file_location("columnar_pure_validation", path)
    validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)
    source = Source(fixture(fmt))
    for kind, options in [("tree", {}), ("table", {"columns": [0, 1, 3], "row_offset": 1, "row_limit": 3})]:
        result = source.preview(fmt, kind, options)
        assert validator.validate_columnar_window_payload(result, kind=kind, options=options, fmt=fmt, source_bytes=source.size,
            read_bytes=sum(n for _, n in source.reads), read_requests=len(source.reads)) is result


def test_int64_uint64_and_decimal_never_coerce_to_double():
    source = Source(fixture(table=pa.table({"u": pa.array([2**64-1], type=pa.uint64()), "d": pa.array([Decimal("9" * 38)], type=pa.decimal128(38, 0))})))
    result = source.preview(kind="table", options={"columns": [0, 1], "row_offset": 0, "row_limit": 1})
    assert result["table"]["rows"] == [[str(2**64-1), "9"*38]]


@pytest.mark.parametrize("size", [True, 0, 8589934593])
def test_invalid_source_size_never_reads(size):
    reads = []
    with pytest.raises(module.ColumnarWindowError): module.columnar_window_preview(lambda *args: reads.append(args), size, "parquet")
    assert reads == []
