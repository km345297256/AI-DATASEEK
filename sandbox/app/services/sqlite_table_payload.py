"""Pure contract for an explicitly selected, read-only SQLite table page."""
from __future__ import annotations

import hashlib
import json
import math
import re

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
FORMATS = {"sqlite", "sqlite3", "db"}
LIMITS = {"max_tables": 32, "max_schema_columns": 128, "max_columns": 16,
          "max_rows": 200, "max_offset": 100000, "max_cell_bytes": 512,
          "max_record_bytes": 65536, "max_schema_bytes": 65536,
          "max_sql_bytes": 8192, "max_progress_callbacks": 2000,
          "progress_interval": 1000, "sqlite_heap_bytes": 33554432}
SEMANTICS = "SQLite storage classes; exact integer text; rowid ascending; no user SQL, joins, aggregation or type coercion"
WARNING = "整文件只读预览（最多 16 MiB），不是大库分块查询；按 rowid 升序分页，不提供用户 SQL。整数保留精确文本，BLOB 只显示长度；超长或不安全文本明确省略，非有限实数单独标记。"
ERROR = "SQLite 文件、目录、分页或读取预算不符合受限只读协议。"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_INT = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")
_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)


class SqliteTableError(ValueError): pass


def need(ok):
    if not ok: raise SqliteTableError(ERROR)


def integer(value, lo=0, hi=2**53-1): return type(value) is int and lo <= value <= hi
def keys(value, names): return isinstance(value, dict) and set(value) == set(names.split())
def name(value): return type(value) is str and _NAME.fullmatch(value) is not None
def table_id(label): return "t-" + hashlib.sha256(label.encode("ascii")).hexdigest()[:24]
def exact_integer(value): return type(value) is str and len(value) <= 20 and _INT.fullmatch(value) is not None and -(2**63) <= int(value) < 2**63
def safe_text(value):
    return type(value) is str and len(value.encode("utf-8", errors="strict")) <= 512 and _UNSAFE.search(value) is None


def validate_sqlite_table_options(kind, options):
    need(type(kind) is str and kind in {"tree", "table"} and isinstance(options, dict))
    if kind == "tree": need(not options); return {}
    need(keys(options, "table columns row_offset row_limit"))
    need(type(options["table"]) is str and re.fullmatch(r"t-[0-9a-f]{24}", options["table"]) is not None)
    c = options["columns"]
    need(isinstance(c, list) and 1 <= len(c) <= 16 and all(integer(v, 0, 127) for v in c) and len(set(c)) == len(c))
    need(integer(options["row_offset"], 0, 100000) and integer(options["row_limit"], 1, 200))
    return {"table": options["table"], "columns": list(c), "row_offset": options["row_offset"], "row_limit": options["row_limit"]}


def validate_cell(cell):
    need(isinstance(cell, dict)); typ = cell.get("type")
    if typ in {"integer", "real", "text", "null", "nonfinite"}:
        need(keys(cell, "type value")); value = cell["value"]
        if typ == "integer": need(exact_integer(value))
        elif typ == "real": need(type(value) in {int, float} and math.isfinite(value) and (type(value) is float or abs(value) <= 2**53-1))
        elif typ == "text": need(safe_text(value))
        else: need(value is None)
    elif typ == "blob": need(keys(cell, "type bytes") and integer(cell["bytes"], 0, 65536))
    elif typ == "text-omitted":
        need(keys(cell, "type bytes reason") and integer(cell["bytes"], 0, 65536))
        need(cell["reason"] == ("cell-budget" if cell["bytes"] > 512 else "unsafe-text"))
    else: need(False)


def validate_sqlite_table_payload(value, *, kind=None, options=None, fmt=None, size=None, limit=MAX_OUTPUT):
    try:
        need(isinstance(value, dict)); view = value.get("kind")
        selected = validate_sqlite_table_options(view, value.get("selected"))
        need(kind is None or view == kind)
        need(options is None or selected == validate_sqlite_table_options(view, options))
        need(keys(value, "contract_version type reader kind media_type choices selected metadata warnings sampled " + view))
        need(type(value["contract_version"]) is int and value["contract_version"] == 2 and value["type"] == value["reader"] == "sqlite-table")
        need(value["media_type"] == "application/json" and value["warnings"] == [WARNING] and type(value["sampled"]) is bool)
        need(keys(value["choices"], "tables")); tables = value["choices"]["tables"]
        need(isinstance(tables, list) and 1 <= len(tables) <= 32)
        labels = []
        for table in tables:
            need(keys(table, "id label columns ordering")); label = table["label"]
            need(name(label) and not label.lower().startswith("sqlite_") and table["id"] == table_id(label) and table["ordering"] == "rowid-ascending")
            labels.append(label); columns = table["columns"]
            need(isinstance(columns, list) and 1 <= len(columns) <= 128)
            names = []
            for i, column in enumerate(columns):
                need(keys(column, "id label affinity nullable primary_key"))
                need(integer(column["id"], i, i) and name(column["label"]) and column["affinity"] in {"INTEGER", "REAL", "TEXT", "BLOB", "NUMERIC"})
                need(type(column["nullable"]) is bool and integer(column["primary_key"], 0, len(columns)))
                names.append(column["label"].lower())
            need(len(set(names)) == len(names) and not {"rowid", "_rowid_", "oid"}.issubset(names))
            pks = sorted(c["primary_key"] for c in columns if c["primary_key"])
            need(pks == list(range(1, len(pks)+1)))
        need(labels == sorted(labels) and len(set(s.lower() for s in labels)) == len(labels))
        m = value["metadata"]
        need(keys(m, "format container input_mode source_bytes table_count schema_bytes value_semantics ordering total_rows_known progress_callbacks rows_returned nonfinite_values omitted_texts blob_values limits"))
        need(m["format"] in FORMATS and (fmt is None or m["format"] == fmt) and m["container"] == "SQLite 3" and m["input_mode"] == "whole")
        need(integer(m["source_bytes"], 512, MAX_INPUT) and (size is None or type(size) is int and size == m["source_bytes"]))
        need(integer(m["table_count"], len(tables), len(tables)) and integer(m["schema_bytes"], 1, min(65536, m["source_bytes"])))
        need(m["value_semantics"] == SEMANTICS and m["ordering"] == "rowid-ascending" and m["total_rows_known"] is False)
        need(integer(m["progress_callbacks"], 0, 2000) and integer(m["rows_returned"], 0, 200))
        need(keys(m["limits"], " ".join(LIMITS)) and m["limits"] == LIMITS and all(type(n) is int for n in m["limits"].values()))
        need(all(integer(m[n], 0, 3200) for n in ("nonfinite_values", "omitted_texts", "blob_values")))
        if view == "tree":
            need(value["tree"] == [{"path": "/" + t["id"], "node_type": "table", "attributes": {"label": t["label"], "columns": len(t["columns"])}} for t in tables])
            need(all(type(node["attributes"]["columns"]) is int for node in value["tree"]))
            need(value["sampled"] is False and all(m[n] == 0 for n in ("rows_returned", "nonfinite_values", "omitted_texts", "blob_values")))
        else:
            matches = [t for t in tables if t["id"] == selected["table"]]; need(len(matches) == 1); target = matches[0]
            need(all(c < len(target["columns"]) for c in selected["columns"]))
            table = value["table"]; need(keys(table, "columns column_ids rows row_ids row_offset has_more ordering"))
            need(table["columns"] == [target["columns"][c]["label"] for c in selected["columns"]] and table["column_ids"] == selected["columns"])
            need(isinstance(table["column_ids"], list) and all(type(c) is int for c in table["column_ids"]))
            need(integer(table["row_offset"], selected["row_offset"], selected["row_offset"]) and table["ordering"] == "rowid-ascending" and type(table["has_more"]) is bool)
            rows = table["rows"]; ids = table["row_ids"]
            need(isinstance(rows, list) and len(rows) <= selected["row_limit"] and m["rows_returned"] == len(rows))
            need(isinstance(ids, list) and len(ids) == len(rows) and all(exact_integer(v) for v in ids) and all(int(a) < int(b) for a,b in zip(ids,ids[1:])))
            need(not table["has_more"] or len(rows) == selected["row_limit"])
            count = {"nonfinite": 0, "text-omitted": 0, "blob": 0}
            for row in rows:
                need(isinstance(row, list) and len(row) == len(selected["columns"]))
                for cell in row:
                    validate_cell(cell)
                    if cell["type"] in count: count[cell["type"]] += 1
            need(m["nonfinite_values"] == count["nonfinite"] and m["omitted_texts"] == count["text-omitted"] and m["blob_values"] == count["blob"])
            need(value["sampled"] == bool(selected["row_offset"] or table["has_more"] or len(selected["columns"]) != len(target["columns"])))
        need(integer(limit, 1, MAX_OUTPUT) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8")) <= limit)
        return value
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError, AttributeError):
        raise SqliteTableError(ERROR) from None
