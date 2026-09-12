"""Single-file dBASE III table preview using dbfread, never a memo sidecar.

The deliberately narrow 0x03 dialect has no nullable FoxPro bitmap, binary
fields, database links or executable objects. Unsupported dialects fail closed
instead of interpreting their null flags as ordinary values.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import tempfile

from .database_table_payload import (
    DatabaseTableError, MAX_INPUT, build_database_table_payload, name, need,
    safe_text, table_id, validate_database_table_options,
)

ENGINE = "dbf"
TYPES = {"C": "TEXT", "N": "DECIMAL", "F": "REAL", "L": "BOOLEAN", "D": "DATE"}


def _header(data):
    need(type(data) is bytes and 65 <= len(data) <= MAX_INPUT)
    need(data[0] == 0x03)  # dBASE III without DBT; not Visual FoxPro/null bitmap.
    records = int.from_bytes(data[4:8], "little")
    header = int.from_bytes(data[8:10], "little")
    width = int.from_bytes(data[10:12], "little")
    need(header >= 65 and (header - 33) % 32 == 0 and data[header - 1:header] == b"\r")
    count = (header - 33) // 32
    need(1 <= count <= 128 and 2 <= width <= 32768 and data[14:16] == b"\0\0")
    need(data[28] == 0 and data[30:32] == b"\0\0")
    end = header + width * records
    need(end == len(data) or end + 1 == len(data) and data[-1] == 0x1a)
    total = 1
    for offset in range(32, header - 1, 32):
        field = data[offset:offset + 32]
        typ, size, decimals = chr(field[11]), field[16], field[17]
        need(typ in TYPES and field[18:32] == bytes(14))
        need(1 <= size <= 254)
        if typ == "C": need(decimals == 0)
        elif typ == "N": need(size <= 38 and decimals <= size - 1)
        elif typ == "F": need(size <= 38 and decimals <= size - 1)
        elif typ == "D": need(size == 8 and decimals == 0)
        elif typ == "L": need(size == 1 and decimals == 0)
        total += size
    need(total == width)
    # dbfread skips unknown record markers. Reject them and unexpected trailing
    # records before its streaming iterator, rather than silently hiding damage.
    need(all(data[i] in (0x20, 0x2a) for i in range(header, end, width)))
    return header, count


def _cell(field, raw, parser):
    if field.type == "N":
        text = raw.strip(b" ").decode("ascii")
        if not text: return {"type": "null", "value": None}
        need(re.fullmatch(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text) is not None)
        need(sum(c.isdigit() for c in text) <= 38)
        # Keep the source decimal digits, including trailing fractional zeroes.
        if text.startswith("-."): text = "-0" + text[1:]
        elif text.startswith("."): text = "0" + text
        if text.endswith("."): text += "0"
        sign = "-" if text.startswith("-") else ""
        integer, dot, fraction = text.lstrip("-").partition(".")
        text = sign + (integer.lstrip("0") or "0") + dot + fraction
        return {"type": "decimal", "value": text}
    if field.type == "C":
        # dbfread also strips NUL padding. Keep NUL visible to the unsafe-text
        # policy instead of silently turning corrupted data into clean text.
        value = raw.rstrip(b" ").decode(parser.encoding, errors="strict")
        size = len(value.encode("utf8"))
        if size > 512: return {"type": "text-omitted", "bytes": size, "reason": "cell-budget"}
        if not safe_text(value): return {"type": "text-omitted", "bytes": size, "reason": "unsafe-text"}
        return {"type": "text", "value": value}
    if field.type == "F":
        text = raw.strip(b" ")
        need(not text or re.fullmatch(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text) is not None)
    if field.type == "D": need(raw in {b"        ", b"00000000"} or re.fullmatch(rb"[0-9]{8}", raw) is not None)
    if field.type == "L": need(raw in (b"T", b"t", b"Y", b"y", b"F", b"f", b"N", b"n", b"?", b" "))
    value = parser.parse(field, raw)
    if value is None: return {"type": "null", "value": None}
    if field.type == "L": return {"type": "boolean", "value": value}
    if field.type == "D": return {"type": "date", "value": value.isoformat()}
    need(field.type == "F" and type(value) is float)
    return {"type": "real", "value": value} if math.isfinite(value) else {"type": "nonfinite", "value": None}


def dbf_table_preview(data, fmt, kind="tree", options=None):
    options = {} if options is None else options
    selected = validate_database_table_options(kind, options)
    need(fmt == "dbf")
    header_size, count = _header(data)
    try:
        from dbfread import DBF, FieldParser
        from dbfread.codepages import guess_encoding
        # An unknown declared codepage must not fall back to lossy replacement.
        encoding = "ascii" if data[29] == 0 else guess_encoding(data[29])
        with tempfile.TemporaryDirectory(prefix="dbf-preview-") as directory:
            path = Path(directory) / "snapshot.dbf"
            with path.open("xb") as stream: stream.write(data)
            path.chmod(0o400)
            dbf = DBF(str(path), encoding=encoding, ignorecase=False, raw=True,
                      load=False, recfactory=list, char_decode_errors="strict")
            need(dbf.memofilename is None and len(dbf.fields) == count)
            need(all(name(f.name) for f in dbf.fields))
            need(len({f.name.casefold() for f in dbf.fields}) == count)
            columns = [{"id": i, "label": f.name, "data_type": TYPES[f.type],
                        "nullable": None, "primary_key": None, "previewable": True} for i, f in enumerate(dbf.fields)]
            catalog = {"id": table_id(ENGINE, "DBF table"), "label": "DBF table", "columns": columns,
                       "ordering": "dbf-record-order"}
            page = None
            if kind == "table":
                need(selected["table"] == catalog["id"])
                need(all(c < count for c in selected["columns"]))
                parser = FieldParser(dbf)
                rows, ids, more = [], [], False
                for ordinal, record in enumerate(dbf):
                    if ordinal < selected["row_offset"]: continue
                    if len(rows) == selected["row_limit"]: more = True; break
                    need(len(record) == count)
                    rows.append([_cell(dbf.fields[i], record[i][1], parser) for i in selected["columns"]])
                    ids.append(str(ordinal))
                page = {"columns": [columns[i]["label"] for i in selected["columns"]],
                        "column_ids": list(selected["columns"]), "rows": rows, "row_ids": ids,
                        "row_offset": selected["row_offset"], "has_more": more, "ordering": "dbf-record-order"}
            need(sorted(p.name for p in Path(directory).iterdir()) == ["snapshot.dbf"])
            return build_database_table_payload(ENGINE, fmt, len(data), kind, selected, [catalog],
                                                schema_bytes=header_size, table=page)
    except (ValueError, TypeError, LookupError, OSError, OverflowError, MemoryError):
        raise DatabaseTableError("数据库文件不符合受限只读预览要求。") from None
