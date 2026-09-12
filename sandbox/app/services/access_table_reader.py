"""Bounded Access native-table preview; no SQL, queries, macros or exports.

libmdb's convenience CSV/JSON exporters collapse NULL and empty strings and
can materialize OLE. The fixed native helper instead reads the row null bitmap
and projects only approved scalar columns. Long text/OLE/complex columns remain
visible in the schema, explicitly unselectable; they never become fake NULLs.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time

from .database_table_payload import (
    DatabaseTableError, ERROR, MAX_INPUT, MAX_OUTPUT, build_database_table_payload,
    name, need, safe_text, table_id, validate_cell, validate_database_table_options,
)

NATIVE_READER = "/usr/local/bin/dataseek-access-reader"
_TYPES = {1: "BOOLEAN", 2: "INTEGER", 3: "INTEGER", 4: "INTEGER", 5: "DECIMAL",
          6: "REAL", 7: "REAL", 8: "TIMESTAMP", 9: "BLOB", 10: "TEXT",
          11: "BLOB", 12: "UNSUPPORTED", 15: "UUID", 16: "DECIMAL", 18: "UNSUPPORTED"}
_PREVIEWABLE = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16}


def _header(data, fmt):
    need(type(data) is bytes and 4096 <= len(data) <= MAX_INPUT and len(data) % 4096 == 0)
    need(data[:4] == b"\x00\x01\x00\x00")
    if fmt == "mdb": need(data[4:20] == b"Standard Jet DB\0" and data[20] == 1)
    elif fmt == "accdb": need(data[4:20] == b"Standard ACE DB\0" and data[20] in {2, 3})
    else: need(False)


def _run(path, arguments):
    """Both output streams have byte ceilings before buffering; no shell/env.

    The C helper also limits CPU/address-space and runs inside the worker's
    existing networkless read-only container, never in the API host process.
    """
    process = subprocess.Popen([NATIVE_READER, str(path), *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        cwd=str(path.parent), env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C", "MDBICONV": "UTF-8"})
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + 15
    try:
        with selectors.DefaultSelector() as selector:
            for name_, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False); selector.register(stream, selectors.EVENT_READ, name_)
            while selector.get_map():
                need(time.monotonic() < deadline)
                for key, _ in selector.select(min(0.1, max(0.0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk: selector.unregister(key.fileobj); continue
                    target = buffers[key.data]
                    need(len(target) + len(chunk) <= (MAX_OUTPUT if key.data == "stdout" else 8192))
                    target.extend(chunk)
        need(process.wait(timeout=max(0.001, deadline - time.monotonic())) == 0)
        # libmdb's corruption diagnostics are neither returned nor treated as
        # harmless successful output. They can include filenames/table names.
        need(not buffers["stderr"])
        return json.loads(buffers["stdout"].decode("utf8"))
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=2)
        if process.stdout is not None: process.stdout.close()
        if process.stderr is not None: process.stderr.close()


def _catalog(raw):
    need(isinstance(raw, dict) and set(raw) == {"version", "tables"} and raw["version"] == "1.0.1")
    need(isinstance(raw["tables"], list) and 1 <= len(raw["tables"]) <= 32)
    tables, internal, schema_bytes = [], {}, 0
    for expected, item in enumerate(raw["tables"]):
        need(isinstance(item, dict) and set(item) == {"index", "label", "columns"})
        need(type(item["index"]) is int and item["index"] == expected and name(item["label"]))
        need(isinstance(item["columns"], list) and 1 <= len(item["columns"]) <= 128)
        columns = []
        for i, col in enumerate(item["columns"]):
            need(isinstance(col, dict) and set(col) == {"label", "type", "previewable"})
            need(name(col["label"]) and type(col["type"]) is int and 0 <= col["type"] <= 255)
            need(type(col["previewable"]) is bool and col["previewable"] == (col["type"] in _PREVIEWABLE))
            columns.append({"id": i, "label": col["label"], "data_type": _TYPES.get(col["type"], "UNSUPPORTED"),
                            "nullable": False if col["type"] == 1 else None, "primary_key": None,
                            "previewable": col["previewable"]})
            schema_bytes += len(col["label"].encode("utf8")) + 16
        schema_bytes += len(item["label"].encode("utf8"))
        need(schema_bytes <= 65536)
        entry = {"id": table_id("access", item["label"]), "label": item["label"], "columns": columns,
                 "ordering": "access-export-order"}
        need(entry["id"] not in internal)
        tables.append(entry); internal[entry["id"]] = (expected, entry)
    return sorted(tables, key=lambda t: t["label"]), internal, schema_bytes


def _cell(cell):
    need(isinstance(cell, dict))
    if cell.get("type") == "access-date":
        need(set(cell) == {"type", "value"} and type(cell["value"]) in {float, int})
        value = cell["value"]
        need(-657434 <= value <= 2958465)
        days = int(value)
        # Access/OLE negative dates use an absolute fractional day. Preserve
        # subsecond information rather than libmdb's strftime-second rounding.
        instant = datetime(1899, 12, 30) + timedelta(days=days, microseconds=round(abs(value - days) * 86400 * 1_000_000))
        return {"type": "timestamp", "value": instant.isoformat()}
    if cell.get("type") == "text":
        need(set(cell) == {"type", "value"} and type(cell["value"]) is str)
        size = len(cell["value"].encode("utf8"))
        if size > 512: return {"type": "text-omitted", "bytes": size, "reason": "cell-budget"}
        if not safe_text(cell["value"]): return {"type": "text-omitted", "bytes": size, "reason": "unsafe-text"}
    validate_cell(cell)
    return cell


def access_table_preview(data, fmt, kind="tree", options=None):
    options = {} if options is None else options
    selected = validate_database_table_options(kind, options)
    _header(data, fmt)
    try:
        with tempfile.TemporaryDirectory(prefix="access-preview-") as directory:
            path = Path(directory) / ("snapshot." + fmt)
            with path.open("xb") as stream: stream.write(data)
            path.chmod(0o400)
            tables, internal, schema_bytes = _catalog(_run(path, ["catalog"]))
            page = None
            if kind == "table":
                need(selected["table"] in internal)
                index, target = internal[selected["table"]]
                need(all(c < len(target["columns"]) and target["columns"][c]["previewable"] for c in selected["columns"]))
                raw = _run(path, ["page", str(index), ",".join(str(c) for c in selected["columns"]),
                                  str(selected["row_offset"]), str(selected["row_limit"])])
                need(isinstance(raw, dict) and set(raw) == {"rows", "has_more"})
                need(isinstance(raw["rows"], list) and len(raw["rows"]) <= selected["row_limit"])
                need(type(raw["has_more"]) is bool)
                for row in raw["rows"]: need(isinstance(row, list) and len(row) == len(selected["columns"]))
                page = {"columns": [target["columns"][i]["label"] for i in selected["columns"]],
                        "column_ids": list(selected["columns"]), "rows": [[_cell(c) for c in row] for row in raw["rows"]],
                        "row_ids": [str(selected["row_offset"] + i) for i in range(len(raw["rows"]))],
                        "row_offset": selected["row_offset"], "has_more": raw["has_more"], "ordering": "access-export-order"}
            need(sorted(p.name for p in Path(directory).iterdir()) == [path.name])
            return build_database_table_payload("access", fmt, len(data), kind, selected, tables,
                                                schema_bytes=schema_bytes, table=page)
    except (ValueError, TypeError, OSError, OverflowError, MemoryError, subprocess.SubprocessError):
        raise DatabaseTableError(ERROR) from None
