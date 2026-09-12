"""Real PyArrow fixtures; only synthetic values and an explicit virtual padding.

Padding exercises byte-range seeking beyond the 64 MiB whole-file boundary, not
a claim that these four-row test tables have large logical data. No source file
is fetched or uploaded here. fixture_bytes() is small and suitable for HTTP QA.
"""
import io
import json
from decimal import Decimal

import pyarrow as pa
import pyarrow.feather as feather
import pyarrow.parquet as pq

from app.services.columnar_window_reader import columnar_window_preview


def fixture_bytes(fmt="parquet"):
    table = pa.table({"identity": pa.array([2**63-1, -2**63, None, 42], type=pa.int64()),
        "decimal": pa.array([Decimal("1234567890123456.1200"), Decimal("-0.0100"), None, Decimal("0.0000")], type=pa.decimal128(20, 4)),
        "label": ["科学 <data>", "", None, "last"]})
    target = io.BytesIO()
    if fmt == "parquet": pq.write_table(table, target, compression="gzip", row_group_size=2, write_page_checksum=True)
    elif fmt == "feather": feather.write_feather(table, target, compression="lz4", chunksize=2)
    else:
        with pa.ipc.new_file(target, table.schema, options=pa.ipc.IpcWriteOptions(compression="zstd")) as writer:
            writer.write_table(table, max_chunksize=2)
    return target.getvalue()


def generate():
    cases = {}
    for fmt in ("parquet", "arrow", "feather"):
        value = fixture_bytes(fmt); gap = 128 * 1024**2; tail = 8 if fmt == "parquet" else 10
        footer = len(value) - tail - int.from_bytes(value[-tail:-tail+4], "little"); size = len(value) + gap
        def read(offset, length):
            end = offset + length; result = b""
            if offset < footer: result += value[offset:min(end, footer)]
            result += b"\0" * max(0, min(end, footer + gap) - max(offset, footer))
            if end > footer + gap: result += value[max(offset, footer + gap)-gap:end-gap]
            return result
        cases[fmt] = {"tree": columnar_window_preview(read, size, fmt),
            "first": columnar_window_preview(read, size, fmt, "table", {"columns": [0, 1, 2], "row_offset": 0, "row_limit": 2}),
            "next": columnar_window_preview(read, size, fmt, "table", {"columns": [0, 1, 2], "row_offset": 2, "row_limit": 2}),
            "column": columnar_window_preview(read, size, fmt, "table", {"columns": [1], "row_offset": 0, "row_limit": 2})}
    return {"provenance": {"generator": "sandbox/tests/columnar_window_browser_payloads.py", "engine": "pyarrow " + pa.__version__, "synthetic": True, "virtual_padding_bytes": gap, "logical_rows": 4}, "cases": cases}


if __name__ == "__main__": print(json.dumps(generate(), ensure_ascii=False, allow_nan=False, separators=(",", ":")))
