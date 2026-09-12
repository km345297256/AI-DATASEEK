"""Single-file columnar windows. Native Arrow runs only in the isolated worker.

Wire formats: Apache parquet-format/parquet.thrift and Arrow format/{File,
Message,Schema}.fbs. Small independent Thrift/FlatBuffer *preflights* enforce
budgets before Arrow parses metadata or allocates decoded column/batch buffers.
No SQL, filenames, filesystem adapters, remote services or extension loading.
"""
from __future__ import annotations

from collections import OrderedDict
import io
import json
import math
import re
import struct
import zlib

MAX_SOURCE = 8 * 1024**3
MAX_READ = 1024**2
MAX_TOTAL = 8 * 1024**2
MAX_READS = 128
MAX_OUTPUT = 2 * 1024**2
MAX_METADATA = 1024**2
MAX_BLOCK = 4 * 1024**2
MAX_DECODED = 16 * 1024**2
MAX_SCAN_ROWS = 262144
MAX_COLUMNS = 128
MAX_GROUPS = 1024
MAX_IPC_BATCHES = 64
FORMATS = {"parquet", "parq", "arrow", "feather"}
SEMANTICS = "source order; integers and decimals are exact strings; nonfinite floats are null; no SQL or aggregation"
WARNING = "仅显示所选列和行窗口，不排序、不聚合；整数和小数以精确文本保留，非有限浮点值显示为空。"
LIMITS = {"max_rows": 200, "max_columns": 32, "max_schema_columns": MAX_COLUMNS,
          "max_metadata_bytes": MAX_METADATA, "max_block_bytes": MAX_BLOCK,
          "max_decoded_bytes": MAX_DECODED, "max_scan_rows": MAX_SCAN_ROWS}


class ColumnarWindowError(ValueError):
    pass


def _fail():
    raise ColumnarWindowError("列式文件、窗口或解码预算无效；仅支持单文件 Parquet 与 Arrow IPC file / Feather v2 的受限平面类型。")


def _int(v, maximum=2**53 - 1, minimum=0):
    return type(v) is int and minimum <= v <= maximum


def validate_columnar_window_options(kind, options):
    if kind not in ("tree", "table") or not isinstance(options, dict):
        _fail()
    if kind == "tree":
        if options:
            _fail()
        return {}
    if (set(options) != {"columns", "row_offset", "row_limit"}
        or not isinstance(options["columns"], list) or not 1 <= len(options["columns"]) <= 32
        or any(not _int(c, MAX_COLUMNS - 1) for c in options["columns"])
        or len(set(options["columns"])) != len(options["columns"])
        or not _int(options["row_offset"]) or not _int(options["row_limit"], 200, 1)):
        _fail()
    return {"columns": list(options["columns"]), "row_offset": options["row_offset"], "row_limit": options["row_limit"]}


class RangeFile(io.RawIOBase):
    def __init__(self, read_range, size, limits=None):
        super().__init__()
        self.limits = {"max_read_bytes": MAX_READ, "max_total_bytes": MAX_TOTAL, "max_reads": MAX_READS}
        if not callable(read_range) or not _int(size, MAX_SOURCE, 16):
            _fail()
        if limits is not None:
            if not isinstance(limits, dict) or set(limits) != set(self.limits) or any(not _int(v, self.limits[k], 1) for k, v in limits.items()):
                _fail()
            self.limits = dict(limits)
        self.source, self.size, self.position = read_range, size, 0
        self.read_bytes = self.read_requests = self.cached_bytes = 0
        self.cache = OrderedDict()

    def readable(self): return True
    def writable(self): return False
    def seekable(self): return True
    def tell(self): return self.position

    def seek(self, offset, whence=0):
        if type(offset) is not int or type(whence) is not int or whence not in (0, 1, 2): _fail()
        position = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        if not _int(position, self.size): _fail()
        self.position = position
        return position

    def at(self, offset, length):
        if self.closed or not _int(offset, self.size) or not _int(length, MAX_TOTAL) or offset + length > self.size: _fail()
        if not length: return b""
        for (start, end), value in list(self.cache.items()):
            if start <= offset and offset + length <= end:
                self.cache.move_to_end((start, end))
                return value[offset - start:offset - start + length]
        pieces = []
        for start in range(offset, offset + length, self.limits["max_read_bytes"]):
            count = min(self.limits["max_read_bytes"], offset + length - start)
            if self.read_bytes + count > self.limits["max_total_bytes"] or self.read_requests >= self.limits["max_reads"]: _fail()
            self.read_bytes += count; self.read_requests += 1
            value = self.source(start, count)
            if not isinstance(value, bytes) or len(value) != count: _fail()
            pieces.append(value)
        value = b"".join(pieces)
        self.cache[(offset, offset + length)] = value; self.cached_bytes += length
        while self.cached_bytes > MAX_TOTAL:
            _, removed = self.cache.popitem(last=False); self.cached_bytes -= len(removed)
        return value

    def read(self, size=-1):
        if type(size) is not int or size < -1: _fail()
        if size == -1: size = self.size - self.position
        value = self.at(self.position, min(size, self.size - self.position))
        self.position += len(value)
        return value

    def readinto(self, target):
        value = self.read(len(target)); target[:len(value)] = value
        return len(value)

    def write(self, value): _fail()
    def truncate(self, size=None): _fail()
    def close(self):
        self.cache.clear(); self.cached_bytes = 0
        super().close()


class Compact:
    """Bounded Thrift compact protocol metadata; never loads executable types."""
    def __init__(self, data): self.data, self.pos, self.nodes = data, 0, 0
    def take(self, n):
        if not _int(n, MAX_METADATA) or self.pos + n > len(self.data): _fail()
        value = self.data[self.pos:self.pos + n]; self.pos += n
        return value
    def byte(self): return self.take(1)[0]
    def varint(self):
        value = 0
        for shift in range(0, 70, 7):
            b = self.byte(); value |= (b & 127) << shift
            if b < 128:
                if value > 2**64 - 1: _fail()
                return value
        _fail()
    def value(self, typ, depth=0):
        self.nodes += 1
        if self.nodes > 65536 or depth > 16: _fail()
        if typ in (1, 2): return typ == 1
        if typ == 3: return struct.unpack("b", self.take(1))[0]
        if typ in (4, 5, 6):
            n = self.varint(); return (n >> 1) ^ -(n & 1)
        if typ == 7: return struct.unpack("<d", self.take(8))[0]
        if typ == 8:
            n = self.varint()
            if n > 65536: _fail()
            return self.take(n)
        if typ in (9, 10):
            b = self.byte(); n, element = b >> 4, b & 15
            if n == 15: n = self.varint()
            if n > 4096 or not 1 <= element <= 12: _fail()
            return [self.value(self.byte() if element in (1, 2) else element, depth + 1) for _ in range(n)]
        if typ == 11: _fail()  # Parquet metadata has no approved map fields.
        if typ != 12: _fail()
        result, last = {}, 0
        while True:
            b = self.byte()
            if b == 0: return result
            field = last + (b >> 4) if b >> 4 else self.value(4, depth + 1)
            if not _int(field, 32767, 1) or field in result: _fail()
            result[field] = self.value(b & 15, depth + 1); last = field


class Flat:
    """Bounds-checked access to approved Arrow FlatBuffer tables and vectors."""
    def __init__(self, data): self.data = data
    def num(self, offset, fmt):
        size = struct.calcsize(fmt)
        if not _int(offset, len(self.data)) or offset + size > len(self.data): _fail()
        return struct.unpack_from("<" + fmt, self.data, offset)[0]
    def root(self): return self.ptr(0)
    def ptr(self, offset):
        target = offset + self.num(offset, "I")
        if target <= offset or target >= len(self.data): _fail()
        return target
    def field(self, table, index):
        vtable = table - self.num(table, "i")
        size, objsize = self.num(vtable, "H"), self.num(vtable + 2, "H")
        if size < 4 or size % 2 or vtable + size > len(self.data) or objsize < 4 or table + objsize > len(self.data): _fail()
        delta = self.num(vtable + 4 + 2 * index, "H") if 4 + 2 * index < size else 0
        if delta and not 4 <= delta < objsize: _fail()
        return table + delta if delta else None
    def scalar(self, table, index, fmt, default=0):
        pos = self.field(table, index)
        return self.num(pos, fmt) if pos is not None else default
    def table(self, table, index):
        pos = self.field(table, index)
        return self.ptr(pos) if pos is not None else None
    def vector(self, table, index, width, maximum):
        pos = self.table(table, index)
        if pos is None: return []
        count = self.num(pos, "I")
        if count > maximum or pos + 4 + count * width > len(self.data): _fail()
        return [pos + 4 + width * i for i in range(count)]
    def string(self, table, index):
        pos = self.table(table, index)
        if pos is None: return b""
        n = self.num(pos, "I")
        if n > 65536 or pos + 5 + n > len(self.data) or self.data[pos + 4 + n] != 0: _fail()
        return self.data[pos + 4:pos + 4 + n]


def _flat_schema(flat, schema):
    if schema is None or flat.scalar(schema, 0, "h") != 0: _fail()
    fields = flat.vector(schema, 1, 4, MAX_COLUMNS)
    if not fields: _fail()
    for entry in fields:
        field = flat.ptr(entry)
        typ = flat.scalar(field, 2, "B")
        if (typ not in {2, 3, 5, 6, 7}
            or flat.table(field, 4) is not None or flat.vector(field, 5, 4, 0)): _fail()
        detail = flat.table(field, 3)
        if detail is None or flat.scalar(field, 1, "B") not in (0, 1): _fail()
        if typ == 2 and (flat.scalar(detail, 0, "i") not in (8, 16, 32, 64) or flat.scalar(detail, 1, "B") not in (0, 1)): _fail()
        if typ == 3 and flat.scalar(detail, 0, "h") not in (1, 2): _fail()
        if typ == 7:
            precision, scale = flat.scalar(detail, 0, "i"), flat.scalar(detail, 1, "i")
            if not _int(precision, 38, 1) or not _int(scale, precision) or flat.scalar(detail, 2, "i", 128) != 128: _fail()
        if not 1 <= len(flat.string(field, 0)) <= 1024: _fail()
        for item in flat.vector(field, 6, 4, 64):
            if flat.string(flat.ptr(item), 0).startswith(b"ARROW:extension:"): _fail()
    return len(fields)


def _label(value, index):
    if (not isinstance(value, str) or not value or len(value) > 128
        or re.search(r"[<>\x00-\x1f\x7f]|(?:/Users/|/home/|/private/|/tmp/|https?://|file:|[A-Za-z]:\\)", value, re.I)):
        return "column " + str(index)
    return value


def _columns(schema):
    import pyarrow as pa
    if not 1 <= len(schema) <= MAX_COLUMNS or len(set(schema.names)) != len(schema): _fail()
    result = []
    for i, field in enumerate(schema):
        dtype = field.type
        if pa.types.is_dictionary(dtype): dtype = dtype.value_type
        if field.metadata and any(k.startswith(b"ARROW:extension:") for k in field.metadata): _fail()
        precision = scale = None
        if pa.types.is_boolean(dtype): typ = "bool"
        elif pa.types.is_integer(dtype): typ = str(dtype)
        elif pa.types.is_float32(dtype): typ = "float32"
        elif pa.types.is_float64(dtype): typ = "float64"
        elif pa.types.is_string(dtype): typ = "string"
        elif pa.types.is_decimal128(dtype) and 1 <= dtype.precision <= 38 and 0 <= dtype.scale <= dtype.precision:
            typ, precision, scale = "decimal128", dtype.precision, dtype.scale
        else: _fail()
        result.append({"id": i, "label": _label(field.name, i), "type": typ, "nullable": field.nullable,
                       "precision": precision, "scale": scale})
    return result


def _groups(counts, selected):
    offset, end = selected["row_offset"], selected["row_offset"] + selected["row_limit"]
    total = sum(counts)
    if offset > total or offset == total and total != 0: _fail()
    end = min(end, total)  # final page explicitly contains the remaining rows.
    result, start = [], 0
    for index, count in enumerate(counts):
        if start < end and start + count > offset:
            result.append((index, max(offset - start, 0), min(end - start, count)))
        start += count
    if sum(counts[i] for i, _, _ in result) > MAX_SCAN_ROWS or len(result) > 32: _fail()
    return result


def _raw_length(data, codec, expected):
    """Count Snappy/LZ4 block expansion without allocating decoded values."""
    pos = total = 0
    def take(n):
        nonlocal pos
        if n < 0 or pos + n > len(data): _fail()
        value = data[pos:pos+n]; pos += n; return value
    if codec == "snappy":
        length = 0
        for shift in range(0, 35, 7):
            value = take(1)[0]; length |= (value & 127) << shift
            if value < 128: break
        else: _fail()
        if length != expected: _fail()
    while pos < len(data):
        token = take(1)[0]
        if codec == "snappy":
            typ = token & 3
            if typ == 0:
                n = token >> 2
                if n >= 60: n = int.from_bytes(take(n - 59), "little")
                n += 1; take(n)
            else:
                n = 4 + ((token >> 2) & 7) if typ == 1 else 1 + (token >> 2)
                offset = ((token & 224) << 3) | take(1)[0] if typ == 1 else int.from_bytes(take(2 if typ == 2 else 4), "little")
                if not 0 < offset <= total: _fail()
            total += n
        else:
            def extended(n):
                if n == 15:
                    while True:
                        x = take(1)[0]; n += x
                        if n > expected: _fail()
                        if x < 255: break
                return n
            n = extended(token >> 4); take(n); total += n
            if pos == len(data): break
            offset = int.from_bytes(take(2), "little")
            if not 0 < offset <= total: _fail()
            total += extended(token & 15) + 4
        if total > expected: _fail()
    if total != expected: _fail()


def _frame_bounds(data, codec, expected):
    """One complete frame; bounded native decoder window; no external dictionary."""
    pos = 0
    def take(n):
        nonlocal pos
        if n < 0 or pos + n > len(data): _fail()
        value = data[pos:pos+n]; pos += n; return value
    if codec == "zstd":
        if take(4) != b"\x28\xb5\x2f\xfd": _fail()
        descriptor = take(1)[0]
        if descriptor & 27: _fail()  # reserved/unused bits and dictionary ID
        single, flag = bool(descriptor & 32), descriptor >> 6
        if not single:
            window = take(1)[0]; base = 1 << (10 + (window >> 3))
            if base + (base // 8) * (window & 7) > MAX_BLOCK: _fail()
        n = (1 if single else 0) if flag == 0 else (2, 4, 8)[flag - 1]
        if n:
            size = int.from_bytes(take(n), "little") + (256 if n == 2 else 0)
            if size != expected: _fail()
        blocks = 0
        while True:
            header = int.from_bytes(take(3), "little"); typ, size = (header >> 1) & 3, header >> 3
            if typ == 3 or size > 128 * 1024: _fail()
            take(1 if typ == 1 else size); blocks += 1
            if blocks > 1024: _fail()
            if header & 1: break
        if descriptor & 4: take(4)
    else:
        if take(4) != b"\x04\x22\x4d\x18": _fail()
        flags, descriptor = take(1)[0], take(1)[0]
        block_id = descriptor >> 4
        if flags >> 6 != 1 or flags & 3 or descriptor & 143 or block_id not in (4, 5, 6, 7): _fail()
        block_limit = 1 << (8 + 2 * block_id)
        if flags & 8 and int.from_bytes(take(8), "little") != expected: _fail()
        take(1); blocks = 0  # header checksum, independently verified by Arrow
        while True:
            value = int.from_bytes(take(4), "little")
            if not value: break
            size = value & 0x7fffffff
            if not 0 < size <= block_limit: _fail()
            take(size)
            if flags & 16: take(4)
            blocks += 1
            if blocks > 1024: _fail()
        if flags & 4: take(4)
    if pos != len(data): _fail()


def _decompressed(data, expected, codec):
    """Validate the compressed stream into an explicitly bounded destination."""
    if not _int(expected, MAX_BLOCK): _fail()
    if codec is None:
        if len(data) != expected: _fail()
        return
    if codec == "gzip":
        decoder = zlib.decompressobj(31)
        value = decoder.decompress(data, expected + 1)
        if len(value) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail: _fail()
    else:
        import pyarrow as pa
        if codec in ("snappy", "lz4_raw"):
            _raw_length(data, codec, expected)
            pa.Codec(codec).decompress(data, decompressed_size=expected, asbytes=True)
        else:
            _frame_bounds(data, codec, expected)
            with pa.CompressedInputStream(pa.BufferReader(data), codec) as stream:
                if len(stream.read(expected + 1)) != expected: _fail()


def _logical_bytes(columns, count):
    total = 0
    for column in columns:
        typ = column["type"]
        width = 16 if typ == "decimal128" else 8 if typ == "string" else 1 if typ == "bool" else int(re.search(r"\d+$", typ)[0]) // 8
        total += count * width + (count + 7) // 8
    return total


def _parquet(source, kind, selected):
    import pyarrow.parquet as pq
    trailer = source.at(source.size - 8, 8)
    length = struct.unpack("<I", trailer[:4])[0]
    if trailer[4:] != b"PAR1" or source.at(0, 4) != b"PAR1" or not 1 <= length <= min(MAX_METADATA, source.size - 12): _fail()
    footer_start = source.size - 8 - length
    footer = source.at(footer_start, length)
    parser = Compact(footer); raw = parser.value(12)
    if parser.pos != length or not isinstance(raw, dict) or 8 in raw or 9 in raw: _fail()
    schema = raw.get(2)
    if (not isinstance(schema, list) or not 2 <= len(schema) <= MAX_COLUMNS + 1
        or schema[0].get(5) != len(schema) - 1
        or any(not isinstance(c, dict) or 1 not in c or c.get(3) not in (0, 1) or 5 in c for c in schema[1:])): _fail()
    groups = raw.get(4)
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS or not _int(raw.get(3)): _fail()
    counts = []
    for group in groups:
        if not isinstance(group, dict) or not _int(group.get(3)) or not isinstance(group.get(1), list) or len(group[1]) != len(schema) - 1: _fail()
        counts.append(group[3])
        for c in group[1]:
            if not isinstance(c, dict) or c.get(1) not in (None, b"") or 8 in c or 9 in c or not isinstance(c.get(3), dict): _fail()
            m = c[3]
            if not isinstance(m.get(3), list) or len(m[3]) != 1: _fail()
    if sum(counts) != raw[3]: _fail()
    # Footer-only synthetic wrapper avoids PyArrow's default 64KiB tail read
    # fetching source data during tree inspection. Offsets remain source offsets.
    metadata = pq.ParquetFile(io.BytesIO(b"PAR1" + footer + struct.pack("<I", length) + b"PAR1"),
        thrift_string_size_limit=MAX_METADATA, thrift_container_size_limit=65536,
        arrow_extensions_enabled=False).metadata
    reader = pq.ParquetFile(source, metadata=metadata, pre_buffer=False, memory_map=False,
                            thrift_string_size_limit=MAX_METADATA, thrift_container_size_limit=65536,
                            page_checksum_verification=True, arrow_extensions_enabled=False)
    columns = _columns(reader.schema_arrow)
    if metadata.num_rows != raw[3] or metadata.num_row_groups != len(groups): _fail()
    state = {"container": "Parquet", "rows": raw[3], "groups": len(groups), "metadata_bytes": length,
             "groups_read": 0, "scan_rows": 0, "decoded_bytes": 0, "blocks": 0}
    if kind == "tree": return columns, state, []
    if any(c >= len(columns) for c in selected["columns"]): _fail()
    selection = _groups(counts, selected)
    if sum(_logical_bytes([columns[c] for c in selected["columns"]], counts[g]) for g, _, _ in selection) > MAX_DECODED: _fail()
    for g, _, _ in selection:
        for c in selected["columns"]:
            m = groups[g][1][c][3]
            if m.get(4) not in {0, 1, 2, 6, 7} or m.get(5) != counts[g]: _fail()
            size, decoded = m.get(7), m.get(6)
            begin = min(m.get(9, -1), m.get(11, m.get(9, -1)))
            if not _int(size, MAX_BLOCK, 1) or not _int(decoded, MAX_BLOCK, 1) or not _int(begin, footer_start, 4) or begin + size > footer_start: _fail()
            state["decoded_bytes"] += decoded
            if state["decoded_bytes"] > MAX_DECODED: _fail()
            block = source.at(begin, size)
            cursor, uncompressed, values, pages = 0, 0, 0, 0
            while cursor < size:
                page = Compact(block[cursor:cursor + min(65536, size - cursor)])
                header = page.value(12); ptype = header.get(1)
                compressed, unencoded = header.get(3), header.get(2)
                if ptype not in (0, 2, 3) or not _int(compressed, MAX_BLOCK) or not _int(unencoded, MAX_BLOCK) or cursor + page.pos + compressed > size: _fail()
                detail = header.get({0: 5, 2: 7, 3: 8}[ptype])
                if not isinstance(detail, dict) or not _int(detail.get(1), MAX_SCAN_ROWS): _fail()
                page_data = block[cursor + page.pos:cursor + page.pos + compressed]
                codec = {0: None, 1: "snappy", 2: "gzip", 6: "zstd", 7: "lz4_raw"}[m[4]]
                if ptype == 3:
                    # V2 levels precede the compressed value section.
                    if not _int(detail.get(5), compressed) or not _int(detail.get(6), compressed): _fail()
                    levels = detail[5] + detail[6]
                    if levels > min(compressed, unencoded): _fail()
                    is_compressed = detail.get(7, True)
                    if type(is_compressed) is not bool: _fail()
                    _decompressed(page_data[levels:], unencoded - levels, codec if is_compressed else None)
                else:
                    _decompressed(page_data, unencoded, codec)
                if ptype in (0, 3): values += detail[1]
                cursor += page.pos + compressed; uncompressed += page.pos + unencoded; pages += 1
                if pages > 1024: _fail()
            if uncompressed != decoded or values != counts[g]: _fail()
            state["blocks"] += 1
    state["scan_rows"] = sum(counts[g] for g, _, _ in selection); state["groups_read"] = len(selection)
    # Dictionary-preserving reads prevent repeated long strings expanding to a
    # huge dense array. Only the final <=200 rows become Python scalar values.
    reader = pq.ParquetFile(source, metadata=metadata, pre_buffer=False, memory_map=False,
        read_dictionary=[f.name for f in reader.schema_arrow if str(f.type) == "string"],
        page_checksum_verification=True, arrow_extensions_enabled=False)
    tables = []
    for g, start, stop in selection:
        table = reader.read_row_group(g, columns=[reader.schema_arrow.names[c] for c in selected["columns"]], use_threads=False)
        if table.num_rows != counts[g] or table.nbytes > MAX_DECODED: _fail()
        tables.append(table.slice(start, stop - start))
    return columns, state, tables


def _ipc(source, kind, selected):
    import pyarrow as pa
    trailer = source.at(source.size - 10, 10)
    length = struct.unpack("<I", trailer[:4])[0]
    if source.at(0, 8) != b"ARROW1\0\0" or trailer[4:] != b"ARROW1" or not 1 <= length <= min(MAX_METADATA, source.size - 18): _fail()
    footer_start = source.size - 10 - length
    footer = Flat(source.at(footer_start, length)); root = footer.root()
    if footer.scalar(root, 0, "h") not in (3, 4) or footer.vector(root, 2, 24, 0): _fail()
    ncols = _flat_schema(footer, footer.table(root, 1))
    blocks, counts, previous = [], [], 8
    metadata_bytes = length
    for item in footer.vector(root, 3, 24, MAX_IPC_BATCHES):
        offset, meta_size, body_size = footer.num(item, "q"), footer.num(item + 8, "i"), footer.num(item + 16, "q")
        if not _int(offset, footer_start, previous) or not _int(meta_size, 65536, 8) or not _int(body_size, MAX_BLOCK) or offset + meta_size + body_size > footer_start: _fail()
        previous = offset + meta_size + body_size
        metadata_bytes += meta_size
        if metadata_bytes > MAX_METADATA: _fail()
        data = source.at(offset, meta_size)
        if data[:4] != b"\xff" * 4 or struct.unpack("<I", data[4:8])[0] != meta_size - 8: _fail()
        message = Flat(data[8:]); m = message.root()
        if message.scalar(m, 1, "B") != 3 or message.scalar(m, 3, "q") != body_size: _fail()
        batch = message.table(m, 2)
        count = message.scalar(batch, 0, "q")
        if not _int(count, MAX_SCAN_ROWS) or message.vector(batch, 4, 8, 0): _fail()
        nodes = message.vector(batch, 1, 16, MAX_COLUMNS)
        if len(nodes) != ncols or any(message.num(n, "q") != count or not _int(message.num(n + 8, "q"), count) for n in nodes): _fail()
        compression = message.table(batch, 3)
        if compression is not None and (message.scalar(compression, 0, "B") not in (0, 1) or message.scalar(compression, 1, "B") != 0): _fail()
        buffers, last = [], 0
        for n in message.vector(batch, 2, 16, 3 * MAX_COLUMNS):
            start, size = message.num(n, "q"), message.num(n + 8, "q")
            if not _int(start, body_size, last) or not _int(size, MAX_BLOCK) or start + size > body_size: _fail()
            buffers.append((start, size)); last = start + size
        blocks.append((offset + meta_size, body_size, buffers, None if compression is None else ("lz4" if message.scalar(compression, 0, "B") == 0 else "zstd"))); counts.append(count)
    # The footer schema has already passed a small flat-type/extension preflight.
    reader = pa.ipc.open_file(source, options=pa.ipc.IpcReadOptions(use_threads=False))
    columns = _columns(reader.schema)
    if len(columns) != ncols or reader.num_record_batches != len(blocks): _fail()
    state = {"container": "Arrow IPC file", "rows": sum(counts), "groups": len(blocks), "metadata_bytes": metadata_bytes,
             "groups_read": 0, "scan_rows": 0, "decoded_bytes": 0, "blocks": 0}
    if kind == "tree": return columns, state, []
    if any(c >= len(columns) for c in selected["columns"]): _fail()
    selection = _groups(counts, selected)
    for g, _, _ in selection:
        offset, body_size, buffers, compression = blocks[g]
        body = source.at(offset, body_size)
        decoded = 0
        for start, size in buffers:
            raw_size = size
            if compression is not None and size:
                if size < 8: _fail()
                raw_size = struct.unpack_from("<q", body, start)[0]
                uncompressed = raw_size == -1
                if uncompressed: raw_size = size - 8
                if not _int(raw_size, MAX_BLOCK): _fail()
                _decompressed(body[start + 8:start + size], raw_size, None if uncompressed else compression)
            if not _int(raw_size, MAX_BLOCK): _fail()
            decoded += raw_size
        if decoded > MAX_BLOCK: _fail()
        state["decoded_bytes"] += decoded; state["blocks"] += len(buffers)
        if state["decoded_bytes"] > MAX_DECODED: _fail()
    state["scan_rows"] = sum(counts[g] for g, _, _ in selection); state["groups_read"] = len(selection)
    tables = []
    for g, start, stop in selection:
        batch = reader.get_batch(g)
        batch.validate(full=True)
        if batch.num_rows != counts[g] or batch.nbytes > MAX_BLOCK: _fail()
        tables.append(pa.Table.from_batches([batch.select(selected["columns"]).slice(start, stop - start)]))
    return columns, state, tables


def columnar_window_preview(read_range, size, fmt, kind="tree", options=None, limits=None):
    selected = validate_columnar_window_options(kind, {} if options is None else options)
    if not isinstance(fmt, str) or fmt not in FORMATS: _fail()
    source = RangeFile(read_range, size, limits)
    try:
        columns, state, tables = (_parquet if fmt in {"parquet", "parq"} else _ipc)(source, kind, selected)
        rows, nonfinite = [], 0
        if kind == "table":
            for table in tables:
                for row in range(table.num_rows):
                    values = []
                    for j, c in enumerate(selected["columns"]):
                        value = table.column(j)[row].as_py(); typ = columns[c]["type"]
                        if value is not None:
                            if typ.startswith(("int", "uint")): value = str(value)
                            elif typ == "decimal128": value = format(value, "." + str(columns[c]["scale"]) + "f")
                            elif typ.startswith("float") and not math.isfinite(value): value = None; nonfinite += 1
                            elif typ == "string" and len(value.encode("utf-8")) > 2048: _fail()
                        values.append(value)
                    rows.append(values)
        metadata = {"format": fmt, "container": state["container"], "input_mode": "window", "value_semantics": SEMANTICS,
            "source_bytes": size, "read_bytes": source.read_bytes, "read_requests": source.read_requests,
            "total_rows": state["rows"], "total_columns": len(columns), "total_groups": state["groups"],
            "metadata_bytes": state["metadata_bytes"], "groups_read": state["groups_read"], "scan_rows": state["scan_rows"],
            "decoded_bytes": state["decoded_bytes"], "blocks_checked": state["blocks"], "nonfinite_values": nonfinite, "limits": LIMITS}
        result = {"contract_version": 2, "type": "columnar-window", "reader": "columnar-window", "kind": kind,
            "media_type": "application/json", "choices": {"columns": columns}, "selected": selected,
            "metadata": metadata, "warnings": [WARNING], "sampled": kind == "table" and (len(rows) != state["rows"] or len(selected["columns"]) != len(columns))}
        if kind == "tree":
            result["tree"] = [{"path": "/column-" + str(c["id"]), "node_type": "column", "attributes": {"label": c["label"], "type": c["type"]}} for c in columns]
        else:
            result["table"] = {"columns": [columns[c]["label"] for c in selected["columns"]], "rows": rows,
                               "row_offset": selected["row_offset"], "total_rows": state["rows"], "total_columns": len(columns)}
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT: _fail()
        return result
    except ColumnarWindowError:
        raise
    except Exception:
        _fail()
    finally:
        source.close()
