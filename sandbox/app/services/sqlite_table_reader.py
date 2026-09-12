"""One isolated SQLite snapshot; fixed queries only, never user SQL or paths.

APSW exposes SQLite's public security controls on the sandbox's Python 3.10.
This is deliberately a whole-file reader, not a remote SQLite VFS or a database
server. A fresh private directory contains exactly one immutable database.
"""
from __future__ import annotations

import math
from pathlib import Path
import tempfile
import time

from .sqlite_table_payload import (
    ERROR, FORMATS, LIMITS, MAX_INPUT, SEMANTICS, WARNING, SqliteTableError,
    integer, name, need, safe_text, table_id, validate_sqlite_table_options,
    validate_sqlite_table_payload,
)


def _header(data):
    need(type(data) is bytes and 512 <= len(data) <= MAX_INPUT and data[:16] == b"SQLite format 3\x00")
    page = int.from_bytes(data[16:18], "big"); page = 65536 if page == 1 else page
    need(512 <= page <= 65536 and page & (page-1) == 0 and len(data) % page == 0)
    # A copied WAL main file can be stale even with immutable=1. Never read it.
    need(data[18:20] == b"\x01\x01" and data[20] == 0 and data[21:24] == b"\x40\x20\x20")
    need(data[72:92] == bytes(20) and int.from_bytes(data[44:48], "big") in (1,2,3,4))
    need(int.from_bytes(data[56:60], "big") == 1)  # Explicit UTF-8 subset.
    need(data[24:28] == data[92:96] and int.from_bytes(data[28:32], "big") == len(data)//page)
    return page


def _affinity(declaration):
    need(type(declaration) is str and len(declaration) <= 128)
    upper = declaration.upper()
    if "INT" in upper: return "INTEGER"
    if any(v in upper for v in ("CHAR", "CLOB", "TEXT")): return "TEXT"
    if not upper or "BLOB" in upper: return "BLOB"
    if any(v in upper for v in ("REAL", "FLOA", "DOUB")): return "REAL"
    return "NUMERIC"


def _quoted(label):
    need(name(label)); return '"' + label + '"'


class _Guard:
    def __init__(self, apsw):
        self.apsw = apsw
        self.allowed_tables = {"sqlite_master", "sqlite_schema"}
        self.allowed_columns = None
        self.allowed_pragmas = {"table_list", "table_xinfo"}
        self.progress_calls = 0
        self.deadline = time.monotonic() + 15

    def progress(self):
        self.progress_calls += 1
        return self.progress_calls > LIMITS["max_progress_callbacks"] or time.monotonic() >= self.deadline

    def authorize(self, action, first, second, database, source):
        a = self.apsw
        if source is not None: return a.SQLITE_DENY
        if action == a.SQLITE_SELECT: return a.SQLITE_OK
        if action == a.SQLITE_FUNCTION:
            return a.SQLITE_OK if second in {"typeof", "length"} else a.SQLITE_DENY
        if action == a.SQLITE_READ:
            ok = database == "main" and first in self.allowed_tables
            if self.allowed_columns is not None: ok = ok and second in self.allowed_columns
            return a.SQLITE_OK if ok else a.SQLITE_DENY
        if action == a.SQLITE_PRAGMA:
            return a.SQLITE_OK if first in self.allowed_pragmas and (first != "table_xinfo" or second in self.allowed_tables) else a.SQLITE_DENY
        return a.SQLITE_DENY


def _configure(connection, apsw):
    # All controls precede the first schema-reading statement. Unknown PRAGMAs
    # can be silently ignored by SQLite, so verify their returned values.
    settings = {"SQLITE_DBCONFIG_DEFENSIVE": 1, "SQLITE_DBCONFIG_TRUSTED_SCHEMA": 0,
                "SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION": 0, "SQLITE_DBCONFIG_ENABLE_TRIGGER": 0,
                "SQLITE_DBCONFIG_ENABLE_VIEW": 0, "SQLITE_DBCONFIG_DQS_DDL": 0,
                "SQLITE_DBCONFIG_DQS_DML": 0}
    for setting, value in settings.items(): need(connection.config(getattr(apsw, setting), value) == value)
    native_limits = {"LENGTH": 65536, "SQL_LENGTH": 8192, "COLUMN": 128,
        "EXPR_DEPTH": 10, "COMPOUND_SELECT": 1, "VDBE_OP": 10000,
        "FUNCTION_ARG": 2, "ATTACHED": 0, "LIKE_PATTERN_LENGTH": 64,
        "VARIABLE_NUMBER": 2, "TRIGGER_DEPTH": 0, "WORKER_THREADS": 0}
    for setting, value in native_limits.items():
        code = getattr(apsw, "SQLITE_LIMIT_" + setting)
        connection.limit(code, value); need(connection.limit(code) == value)
    connection.drop_modules(None)
    connection.enable_load_extension(False)
    for pragma, value in (("trusted_schema", 0), ("query_only", 1), ("mmap_size", 0), ("temp_store", 2), ("cache_size", -1024), ("cell_size_check", 1), ("automatic_index", 0)):
        connection.execute(f"PRAGMA {pragma}={value}")
        need(connection.execute(f"PRAGMA {pragma}").fetchone() == (value,))
    connection.set_busy_timeout(0)


def _catalog(connection, guard):
    entries = []
    schema_bytes = 0
    # LIMIT applies to every schema entry, not just approved table names.
    for entry in connection.execute("SELECT type,name,tbl_name,rootpage,sql FROM main.sqlite_schema LIMIT 129"):
        need(len(entries) < 128)
        typ, label, owner, root, sql = entry
        need(typ in {"table", "index"} and name(label) and name(owner) and integer(root, 1, MAX_INPUT//512))
        need(sql is None or type(sql) is str and len(sql.encode("utf8")) <= 8192)
        schema_bytes += len(label) + len(owner) + len(sql.encode("utf8") if sql is not None else b"")
        need(schema_bytes <= 65536)
        entries.append(entry)
    actual = [v for v in entries if v[0] == "table" and not v[1].lower().startswith("sqlite_")]
    need(1 <= len(actual) <= 32)
    # sqlite_sequence / statistics are never offered as user tables.
    guard.allowed_tables.update(v[1] for v in actual)
    table_info = {}
    for schema, label, typ, ncol, without_rowid, strict in connection.execute("PRAGMA main.table_list"):
        if schema != "main" or label.startswith("sqlite_"): continue
        need(typ == "table" and without_rowid == 0 and 1 <= ncol <= 128)
        table_info[label] = ncol
    need(set(table_info) == {v[1] for v in actual})
    choices, internal = [], {}
    for _, label, _, _, _ in sorted(actual, key=lambda v: v[1]):
        columns = []
        for cid, cname, declaration, notnull, default, pk, hidden in connection.execute("PRAGMA main.table_xinfo(" + _quoted(label) + ")"):
            need(integer(cid, len(columns), len(columns)) and name(cname) and hidden == 0 and len(columns) < 128)
            need(type(notnull) is int and notnull in {0,1} and integer(pk, 0, 128))
            columns.append({"id": cid, "label": cname, "affinity": _affinity(declaration), "nullable": not bool(notnull), "primary_key": pk})
        need(len(columns) == table_info[label])
        names = {v["label"].lower() for v in columns}
        rowid = next((v for v in ("_rowid_", "rowid", "oid") if v not in names), None)
        need(rowid is not None)
        item = {"id": table_id(label), "label": label, "columns": columns, "ordering": "rowid-ascending"}
        choices.append(item); internal[item["id"]] = (item, rowid)
    return choices, internal, schema_bytes


def _cell(typ, size, value):
    if typ == "null": need(value is None); return {"type": "null", "value": None}
    if typ == "integer": need(type(value) is int and -(2**63) <= value < 2**63); return {"type": "integer", "value": str(value)}
    if typ == "real":
        need(type(value) is float)
        return {"type": "real", "value": value} if math.isfinite(value) else {"type": "nonfinite", "value": None}
    need(typ in {"text", "blob"} and integer(size, 0, 65536))
    if typ == "blob": need(value is None); return {"type": "blob", "bytes": size}
    if size > 512: need(value is None); return {"type": "text-omitted", "bytes": size, "reason": "cell-budget"}
    need(type(value) is bytes and len(value) == size)
    text = value.decode("utf8", errors="strict")
    if not safe_text(text): return {"type": "text-omitted", "bytes": size, "reason": "unsafe-text"}
    return {"type": "text", "value": text}


def _page(connection, guard, internal, options):
    need(options["table"] in internal)
    target, rowid = internal[options["table"]]
    need(all(c < len(target["columns"]) for c in options["columns"]))
    columns = [target["columns"][c]["label"] for c in options["columns"]]
    guard.allowed_tables = {target["label"]}
    # SQLite reports an INTEGER PRIMARY KEY alias by its declared column name,
    # even when the trusted query asks for the unshadowed hidden rowid alias.
    key_names = {c["label"] for c in target["columns"] if c["primary_key"] == 1 and c["affinity"] == "INTEGER"}
    guard.allowed_columns = set(columns) | {"ROWID", rowid} | key_names
    guard.allowed_pragmas = set()
    fields = [_quoted(rowid)]
    for label in columns:
        c = _quoted(label)
        fields.extend((f"typeof({c})", f"CASE WHEN typeof({c}) IN ('text','blob') THEN length(CAST({c} AS BLOB)) ELSE NULL END",
            f"CASE WHEN typeof({c})='blob' THEN NULL WHEN typeof({c})='text' THEN CASE WHEN length(CAST({c} AS BLOB))<=512 THEN CAST({c} AS BLOB) ELSE NULL END ELSE {c} END"))
    query = "SELECT " + ",".join(fields) + " FROM main." + _quoted(target["label"]) + " NOT INDEXED ORDER BY " + _quoted(rowid) + " ASC LIMIT ? OFFSET ?"
    need(len(query.encode()) <= LIMITS["max_sql_bytes"])
    rows, ids, more = [], [], False
    cursor = connection.execute(query, (options["row_limit"]+1, options["row_offset"]))
    try:
        for row in cursor:
            need(type(row[0]) is int and -(2**63) <= row[0] < 2**63)
            if len(rows) == options["row_limit"]: more = True; break
            ids.append(str(row[0])); rows.append([_cell(*row[i:i+3]) for i in range(1, len(row), 3)])
    finally: cursor.close()
    return {"columns": columns, "column_ids": list(options["columns"]), "rows": rows, "row_ids": ids,
        "row_offset": options["row_offset"], "has_more": more, "ordering": "rowid-ascending"}


def sqlite_table_preview(data, fmt, kind="tree", options=None):
    options = {} if options is None else options
    selected = validate_sqlite_table_options(kind, options)
    need(type(fmt) is str and fmt in FORMATS); _header(data)
    import apsw
    # Dedicated one-shot worker only: do not raise a previously tighter limit.
    previous = apsw.hard_heap_limit(-1)
    apsw.hard_heap_limit(min(previous or LIMITS["sqlite_heap_bytes"], LIMITS["sqlite_heap_bytes"]))
    try:
        with tempfile.TemporaryDirectory(prefix="sqlite-preview-") as directory:
            path = Path(directory) / "snapshot.sqlite"
            with path.open("xb") as stream: stream.write(data)
            path.chmod(0o400)
            connection = None
            try:
                connection = apsw.Connection(path.as_uri()+"?mode=ro&immutable=1&cache=private", flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI, statementcachesize=0)
                _configure(connection, apsw)
                guard = _Guard(apsw); connection.set_authorizer(guard.authorize)
                connection.set_progress_handler(guard.progress, 1000)
                tables, internal, schema_bytes = _catalog(connection, guard)
                result = {"contract_version": 2, "type": "sqlite-table", "reader": "sqlite-table", "kind": kind, "media_type": "application/json",
                    "choices": {"tables": tables}, "selected": selected, "metadata": {}, "warnings": [WARNING], "sampled": False}
                if kind == "tree": result["tree"] = [{"path": "/"+t["id"], "node_type": "table", "attributes": {"label": t["label"], "columns": len(t["columns"])}} for t in tables]
                else:
                    table = _page(connection, guard, internal, selected); result["table"] = table
                    result["sampled"] = bool(selected["row_offset"] or table["has_more"] or len(selected["columns"]) != len(internal[selected["table"]][0]["columns"]))
                cells = [cell for row in result.get("table", {}).get("rows", []) for cell in row]
                result["metadata"] = {"format": fmt, "container": "SQLite 3", "input_mode": "whole", "source_bytes": len(data),
                    "table_count": len(tables), "schema_bytes": schema_bytes, "value_semantics": SEMANTICS, "ordering": "rowid-ascending", "total_rows_known": False,
                    "progress_callbacks": guard.progress_calls, "rows_returned": len(result.get("table", {}).get("rows", [])),
                    "nonfinite_values": sum(c["type"] == "nonfinite" for c in cells), "omitted_texts": sum(c["type"] == "text-omitted" for c in cells),
                    "blob_values": sum(c["type"] == "blob" for c in cells), "limits": dict(LIMITS)}
                need(time.monotonic() < guard.deadline)
                return validate_sqlite_table_payload(result, kind=kind, options=options, fmt=fmt, size=len(data))
            finally:
                if connection is not None: connection.close(force=True)
                # Neither journals, WAL, attached DBs nor decoded BLOB files.
                need(sorted(p.name for p in Path(directory).iterdir()) == ["snapshot.sqlite"])
    except (apsw.Error, ValueError, TypeError, UnicodeError, OSError, OverflowError, MemoryError):
        raise SqliteTableError(ERROR) from None
