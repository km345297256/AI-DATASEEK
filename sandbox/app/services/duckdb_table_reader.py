"""Fixed-query DuckDB reader inside the existing one-shot isolated worker.

All connections are immutable private copies. Security settings are supplied at
connection creation, before an untrusted catalog is opened. No extensions,
Python replacement scans, user SQL, views, macros or generated/default columns
are evaluated. The engine memory limit is not an RSS sandbox: the surrounding
worker process/container limits remain mandatory.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import tempfile
import threading
import time

from .database_table_payload import (
    DatabaseTableError, ERROR, MAX_INPUT, ORDERINGS, build_database_table_payload,
    integer, name, need, safe_text, table_id, validate_database_table_options,
)

ENGINE_VERSION = "1.5.5"
_INT_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}
_TYPES = {**{t: "INTEGER" for t in _INT_TYPES}, "FLOAT": "REAL", "DOUBLE": "REAL", "BOOLEAN": "BOOLEAN",
    "VARCHAR": "TEXT", "BLOB": "BLOB", "DATE": "DATE", "TIME": "TIME", "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP_S": "TIMESTAMP", "TIMESTAMP_MS": "TIMESTAMP", "TIMESTAMP_NS": "TIMESTAMP", "UUID": "UUID"}


def _config(directory):
    # On Linux the native option application order can reject temp_directory=''
    # after external access was disabled. Do not temporarily enable access to
    # work around that: leave its default inside our private snapshot directory
    # and enforce a zero-byte native spill quota instead, before opening the DB.
    return {"enable_external_access": "false", "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
        "allow_community_extensions": "false", "allow_unsigned_extensions": "false", "allow_persistent_secrets": "false",
        "enable_object_cache": "false", "python_enable_replacements": "false", "threads": "1", "memory_limit": "64MB",
        "max_temp_directory_size": "0B", "secret_directory": directory, "extension_directory": directory, "lock_configuration": "true"}


def _quoted(label):
    need(name(label)); return '"' + label + '"'


def _kind(declaration):
    need(type(declaration) is str and len(declaration.encode("utf8")) <= 4096)
    if re.fullmatch(r"DECIMAL\(([1-9]|[12][0-9]|3[0-8]),([0-9]|[12][0-9]|3[0-8])\)", declaration): return "DECIMAL"
    return _TYPES.get(declaration, "UNSUPPORTED")


def _catalog(connection):
    # Qualify every builtin through the immutable system catalog. A persisted
    # user macro named duckdb_tables, encode or octet_length cannot intercept it.
    entries = connection.execute("SELECT schema_name,table_name,column_count,sql FROM system.main.duckdb_tables() WHERE NOT internal AND NOT temporary ORDER BY schema_name,table_name LIMIT 33").fetchall()
    need(1 <= len(entries) <= 32)
    choices, internal, schema_bytes = [], {}, 0
    for schema, label, column_count, sql in entries:
        need(schema == "main" and name(label) and integer(column_count, 1, 128))
        need(type(sql) is str and len(sql.encode("utf8")) <= 8192)
        schema_bytes += len(sql.encode("utf8")); need(schema_bytes <= 65536)
        infos = connection.execute("SELECT column_index,column_name,data_type,is_nullable,column_default FROM system.main.duckdb_columns() WHERE schema_name=? AND table_name=? AND NOT internal ORDER BY column_index LIMIT 129", (schema, label)).fetchall()
        need(len(infos) == column_count)
        constraints = connection.execute("SELECT constraint_column_indexes FROM system.main.duckdb_constraints() WHERE schema_name=? AND table_name=? AND constraint_type='PRIMARY KEY' LIMIT 2", (schema, label)).fetchall()
        need(len(constraints) <= 1)
        pk = constraints[0][0] if constraints else []
        need(isinstance(pk, list) and all(integer(c, 0, column_count-1) for c in pk) and len(set(pk)) == len(pk))
        columns = []
        for i, (position, cname, declaration, nullable, default) in enumerate(infos):
            need(integer(position, i+1, i+1) and name(cname) and type(nullable) is bool and cname.casefold() != "rowid")
            need(default is None or type(default) is str and len(default.encode("utf8")) <= 8192)
            dtype = _kind(declaration)
            # A default can be lazily materialized for old row groups after
            # ALTER TABLE; generated expressions definitely execute on SELECT.
            # Neither is selected, even when the declared type looks scalar.
            columns.append({"id": i, "label": cname, "data_type": dtype, "nullable": nullable,
                "primary_key": pk.index(i)+1 if i in pk else 0, "previewable": dtype != "UNSUPPORTED" and default is None})
            schema_bytes += len(cname.encode("utf8")) + len(declaration.encode("utf8"))
            need(schema_bytes <= 65536)
        target = {"id": table_id("duckdb", label), "label": label, "columns": columns, "ordering": ORDERINGS["duckdb"]}
        choices.append(target); internal[target["id"]] = target
    return choices, internal, schema_bytes


def _cell(dtype, size, value):
    if dtype in {"TEXT", "BLOB"}:
        if size is None: need(value is None); return {"type": "null", "value": None}
        need(integer(size, 0, MAX_INPUT))
        if dtype == "BLOB": need(value is None); return {"type": "blob", "bytes": size}
        if size > 512: need(value is None); return {"type": "text-omitted", "bytes": size, "reason": "cell-budget"}
        need(type(value) is str and len(value.encode("utf8")) == size)
        if not safe_text(value): return {"type": "text-omitted", "bytes": size, "reason": "unsafe-text"}
        return {"type": "text", "value": value}
    need(size is None)
    if value is None: return {"type": "null", "value": None}
    if dtype == "BOOLEAN": need(type(value) is bool); return {"type": "boolean", "value": value}
    if dtype == "REAL":
        need(type(value) is float)
        return {"type": "real", "value": value} if math.isfinite(value) else {"type": "nonfinite", "value": None}
    need(type(value) is str)
    if dtype in {"DATE", "TIME", "TIMESTAMP"} and value in {"infinity", "-infinity"}: return {"type": "nonfinite", "value": None}
    return {"type": dtype.lower(), "value": value}


def _page(connection, internal, selected):
    target = internal.get(selected["table"]); need(target is not None)
    need(all(c < len(target["columns"]) and target["columns"][c]["previewable"] for c in selected["columns"]))
    columns = [target["columns"][i] for i in selected["columns"]]
    fields = ['"rowid"']
    for column in columns:
        ref = _quoted(column["label"]); dtype = column["data_type"]
        if dtype == "TEXT":
            length = f"system.main.octet_length(system.main.encode({ref}))"
            fields.extend((length, f"CASE WHEN {length} <= 512 THEN {ref} ELSE NULL END"))
        elif dtype == "BLOB": fields.extend((f"system.main.octet_length({ref})", "NULL"))
        elif dtype in {"REAL", "BOOLEAN"}: fields.extend(("NULL", ref))
        else: fields.extend(("NULL", f"CAST({ref} AS VARCHAR)"))
    query = "SELECT " + ",".join(fields) + " FROM main." + _quoted(target["label"]) + ' ORDER BY "rowid" ASC LIMIT ? OFFSET ?'
    need(len(query.encode("utf8")) <= 32768)
    cursor = connection.execute(query, (selected["row_limit"]+1, selected["row_offset"]))
    rows, ids, more = [], [], False
    while True:
        record = cursor.fetchone()
        if record is None: break
        need(integer(record[0], 0, 2**63-1))
        if len(rows) == selected["row_limit"]: more = True; break
        cells = [_cell(c["data_type"], record[1+i*2], record[2+i*2]) for i, c in enumerate(columns)]
        rows.append(cells); ids.append(str(record[0]))
    return {"columns": [c["label"] for c in columns], "column_ids": list(selected["columns"]), "rows": rows, "row_ids": ids,
        "row_offset": selected["row_offset"], "has_more": more, "ordering": ORDERINGS["duckdb"]}


def duckdb_table_preview(data, fmt, kind="tree", options=None):
    options = {} if options is None else options
    selected = validate_database_table_options(kind, options)
    need(type(fmt) is str and fmt in {"duckdb", "ddb"} and type(data) is bytes and 4096 <= len(data) <= MAX_INPUT and data[8:12] == b"DUCK")
    import duckdb
    need(duckdb.__version__ == ENGINE_VERSION)
    try:
        with tempfile.TemporaryDirectory(prefix="duckdb-preview-") as directory:
            path = Path(directory) / "snapshot.duckdb"
            with path.open("xb") as stream: stream.write(data)
            path.chmod(0o400)
            connection, timer = None, None
            deadline = time.monotonic() + 15
            try:
                connection = duckdb.connect(str(path), read_only=True, config=_config(directory))
                timer = threading.Timer(max(0.001, deadline-time.monotonic()), connection.interrupt); timer.daemon = True; timer.start()
                tables, internal, schema_bytes = _catalog(connection)
                table = _page(connection, internal, selected) if kind == "table" else None
                need(time.monotonic() < deadline)
                return build_database_table_payload("duckdb", fmt, len(data), kind, selected, tables, schema_bytes=schema_bytes, table=table)
            finally:
                if timer is not None: timer.cancel(); timer.join()
                if connection is not None: connection.close()
                # The native reader cannot silently create a WAL, temp spill,
                # extensions, secrets or sidecar file in its private directory.
                need(sorted(p.name for p in Path(directory).iterdir()) == ["snapshot.duckdb"])
    except (duckdb.Error, ValueError, TypeError, UnicodeError, OSError, OverflowError, MemoryError):
        raise DatabaseTableError(ERROR) from None
