"""Strict, pure contract for bounded database file previews (not a SQL API)."""
from __future__ import annotations

import hashlib
import json
import math
import re

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
FORMATS = {"duckdb": "duckdb", "ddb": "duckdb", "dbf": "dbf", "mdb": "access", "accdb": "access"}
CONTAINERS = {"duckdb": "DuckDB", "dbf": "dBASE DBF", "access": "Microsoft Access"}
ORDERINGS = {"duckdb": "duckdb-rowid-ascending", "dbf": "dbf-record-order", "access": "access-export-order"}
LIMITS = {"max_tables": 32, "max_schema_columns": 128, "max_columns": 16,
          "max_rows": 200, "max_offset": 100000, "max_cell_bytes": 512,
          "max_schema_bytes": 65536}
DATA_TYPES = {"INTEGER", "DECIMAL", "REAL", "BOOLEAN", "TEXT", "BLOB", "DATE", "TIME", "TIMESTAMP", "UUID", "UNSUPPORTED"}
SEMANTICS = "Engine-native scalar values; exact integer and decimal text; no user SQL, views, macros, joins or type coercion"
WARNING = "整文件只读预览（最多 16 MiB），不是大库分块查询；仅浏览实体表，不执行用户 SQL、视图或宏。整数及定点小数保留精确文本，BLOB 只显示长度；超长或不安全文本明确省略。分页顺序以各引擎标注为准，不等同于业务主键顺序。"
ERROR = "数据库文件、目录、分页或读取预算不符合受限只读协议。"
_NAME = re.compile(r"\w[\w .()-]{0,127}\Z", re.UNICODE)
_INT = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)


class DatabaseTableError(ValueError): pass


def need(ok):
    if not ok: raise DatabaseTableError(ERROR)


def integer(value, lo=0, hi=2**53-1): return type(value) is int and lo <= value <= hi
def keys(value, names): return isinstance(value, dict) and set(value) == set(names.split())
def safe_text(value):
    return type(value) is str and len(value.encode("utf8", errors="strict")) <= 512 and _UNSAFE.search(value) is None
def name(value):
    return safe_text(value) and len(value.encode("utf8")) <= 128 and _NAME.fullmatch(value) is not None and value == value.strip()
def table_id(engine, label): return "t-" + hashlib.sha256((engine + "\0" + label).encode("utf8")).hexdigest()[:24]
def exact_integer(value):
    return type(value) is str and len(value) <= 40 and _INT.fullmatch(value) is not None and -(2**127) <= int(value) < 2**128
def exact_decimal(value):
    return type(value) is str and len(value) <= 41 and _DECIMAL.fullmatch(value) is not None and len(value.replace("-", "").replace(".", "").lstrip("0")) <= 38
# Keep this module byte-identical to the backend contract copy.
# Canonical scalar text from the approved engines, never parsed/reformatted.
# DuckDB BC precedes timestamp time; DATE years may exceed four digits.
# This is a lexical/component guard, not a full calendar or timezone model.
_DATE = re.compile(r"([0-9]{4,10})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])(?: \(BC\))?\Z")
_TIME = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?\Z")
_TIMESTAMP = re.compile(r"(.+)[ T]([0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?)\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def scalar_text(kind, value):
    if type(value) is not str or len(value) > 64: return False
    if kind == "date":
        match = _DATE.fullmatch(value)
        if not match: return False
        year, month, day = map(int, match.groups())
        return year > 0 and day <= (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    if kind == "time":
        match = _TIME.fullmatch(value)
        if not match: return False
        hour, minute, second = map(int, match.groups()[:3])
        fraction = match.group(4) or ""
        return hour <= 24 and minute <= 59 and second <= 59 and (hour != 24 or minute == second == 0 and not fraction.strip("0"))
    if kind == "timestamp":
        match = _TIMESTAMP.fullmatch(value)
        return bool(match and scalar_text("date", match.group(1)) and scalar_text("time", match.group(2)))
    return kind == "uuid" and _UUID.fullmatch(value) is not None


def validate_database_table_options(kind, options):
    need(type(kind) is str and kind in {"tree", "table"} and isinstance(options, dict))
    if kind == "tree": need(not options); return {}
    need(keys(options, "table columns row_offset row_limit"))
    need(type(options["table"]) is str and re.fullmatch(r"t-[0-9a-f]{24}", options["table"]) is not None)
    columns = options["columns"]
    need(isinstance(columns, list) and 1 <= len(columns) <= 16 and all(integer(v, 0, 127) for v in columns) and len(set(columns)) == len(columns))
    need(integer(options["row_offset"], 0, 100000) and integer(options["row_limit"], 1, 200))
    return {"table": options["table"], "columns": list(columns), "row_offset": options["row_offset"], "row_limit": options["row_limit"]}


def validate_cell(cell):
    need(isinstance(cell, dict)); typ = cell.get("type"); need(type(typ) is str)
    if typ in {"integer", "decimal", "real", "boolean", "text", "date", "time", "timestamp", "uuid", "null", "nonfinite"}:
        need(keys(cell, "type value")); value = cell["value"]
        if typ == "integer": need(exact_integer(value))
        elif typ == "decimal": need(exact_decimal(value))
        elif typ == "real": need(type(value) in {int, float} and math.isfinite(value) and (type(value) is float or abs(value) <= 2**53-1))
        elif typ == "boolean": need(type(value) is bool)
        elif typ in {"null", "nonfinite"}: need(value is None)
        else:
            need(safe_text(value))
            if typ != "text": need(scalar_text(typ, value))
    elif typ == "blob": need(keys(cell, "type bytes") and integer(cell["bytes"], 0, MAX_INPUT))
    elif typ == "text-omitted":
        need(keys(cell, "type bytes reason") and integer(cell["bytes"], 0, MAX_INPUT))
        need(cell["reason"] == ("cell-budget" if cell["bytes"] > 512 else "unsafe-text"))
    else: need(False)


def build_database_table_payload(engine, fmt, size, kind, selected, tables, *, schema_bytes, table=None):
    need(engine in ORDERINGS and FORMATS.get(fmt) == engine)
    result = {"contract_version": 2, "type": "database-table", "reader": "database-table", "kind": kind,
        "media_type": "application/json", "choices": {"tables": tables}, "selected": selected,
        "metadata": {}, "warnings": [WARNING], "sampled": False}
    if kind == "tree":
        result["tree"] = [{"path": "/" + t["id"], "node_type": "table", "attributes": {"label": t["label"], "columns": len(t["columns"])}} for t in tables]
    else:
        need(table is not None); result["table"] = table
        target = next((t for t in tables if t["id"] == selected["table"]), None); need(target is not None)
        result["sampled"] = bool(selected["row_offset"] or table["has_more"] or len(selected["columns"]) != len(target["columns"]))
    cells = [c for row in result.get("table", {}).get("rows", []) for c in row]
    result["metadata"] = {"engine": engine, "format": fmt, "container": CONTAINERS[engine], "input_mode": "whole", "source_bytes": size,
        "table_count": len(tables), "schema_bytes": schema_bytes, "value_semantics": SEMANTICS, "ordering": ORDERINGS[engine],
        "total_rows_known": False, "rows_returned": len(result.get("table", {}).get("rows", [])),
        "nonfinite_values": sum(c["type"] == "nonfinite" for c in cells), "omitted_texts": sum(c["type"] == "text-omitted" for c in cells),
        "blob_values": sum(c["type"] == "blob" for c in cells), "limits": dict(LIMITS)}
    return validate_database_table_payload(result, kind=kind, options=selected, fmt=fmt, size=size)


def validate_database_table_payload(value, *, kind=None, options=None, fmt=None, size=None, limit=MAX_OUTPUT):
    try:
        need(isinstance(value, dict)); view = value.get("kind")
        selected = validate_database_table_options(view, value.get("selected"))
        need(kind is None or view == kind)
        need(options is None or selected == validate_database_table_options(view, options))
        need(keys(value, "contract_version type reader kind media_type choices selected metadata warnings sampled " + view))
        need(type(value["contract_version"]) is int and value["contract_version"] == 2 and value["type"] == value["reader"] == "database-table")
        need(value["media_type"] == "application/json" and value["warnings"] == [WARNING] and type(value["sampled"]) is bool)
        m = value["metadata"]
        need(keys(m, "engine format container input_mode source_bytes table_count schema_bytes value_semantics ordering total_rows_known rows_returned nonfinite_values omitted_texts blob_values limits"))
        engine = m["engine"]; need(type(engine) is str and engine in ORDERINGS)
        need(type(m["format"]) is str and FORMATS.get(m["format"]) == engine and (fmt is None or m["format"] == fmt))
        need(m["container"] == CONTAINERS[engine] and m["input_mode"] == "whole")
        need(integer(m["source_bytes"], 32, MAX_INPUT) and (size is None or type(size) is int and size == m["source_bytes"]))
        need(integer(m["schema_bytes"], 1, 65536) and m["value_semantics"] == SEMANTICS and m["ordering"] == ORDERINGS[engine] and m["total_rows_known"] is False)
        need(integer(m["rows_returned"], 0, 200) and all(integer(m[n], 0, 3200) for n in ("nonfinite_values", "omitted_texts", "blob_values")))
        need(keys(m["limits"], " ".join(LIMITS)) and m["limits"] == LIMITS and all(type(n) is int for n in m["limits"].values()))
        need(keys(value["choices"], "tables")); tables = value["choices"]["tables"]
        need(isinstance(tables, list) and 1 <= len(tables) <= 32 and integer(m["table_count"], len(tables), len(tables)))
        labels = []
        for item in tables:
            need(keys(item, "id label columns ordering")); label = item["label"]
            need(name(label) and item["id"] == table_id(engine, label) and item["ordering"] == ORDERINGS[engine]); labels.append(label)
            columns = item["columns"]; need(isinstance(columns, list) and 1 <= len(columns) <= 128)
            names = []
            for i, column in enumerate(columns):
                need(keys(column, "id label data_type nullable primary_key previewable"))
                need(integer(column["id"], i, i) and name(column["label"]) and type(column["data_type"]) is str and column["data_type"] in DATA_TYPES)
                need(column["nullable"] is None or type(column["nullable"]) is bool)
                need(column["primary_key"] is None or integer(column["primary_key"], 0, len(columns)))
                need(type(column["previewable"]) is bool and (column["data_type"] != "UNSUPPORTED" or column["previewable"] is False))
                names.append(column["label"].casefold())
            need(len(set(names)) == len(names))
            pks = sorted(c["primary_key"] for c in columns if c["primary_key"])
            need(pks == list(range(1, len(pks) + 1)))
        need(labels == sorted(labels) and len(set(s.casefold() for s in labels)) == len(labels))
        if view == "tree":
            need(value["tree"] == [{"path": "/" + t["id"], "node_type": "table", "attributes": {"label": t["label"], "columns": len(t["columns"])}} for t in tables])
            need(all(type(node["attributes"]["columns"]) is int for node in value["tree"]))
            need(value["sampled"] is False and all(m[n] == 0 for n in ("rows_returned", "nonfinite_values", "omitted_texts", "blob_values")))
        else:
            matches = [t for t in tables if t["id"] == selected["table"]]; need(len(matches) == 1); target = matches[0]
            need(all(c < len(target["columns"]) for c in selected["columns"]))
            need(all(target["columns"][c]["previewable"] for c in selected["columns"]))
            table = value["table"]; need(keys(table, "columns column_ids rows row_ids row_offset has_more ordering"))
            need(table["columns"] == [target["columns"][c]["label"] for c in selected["columns"]] and table["column_ids"] == selected["columns"])
            need(isinstance(table["column_ids"], list) and all(type(c) is int for c in table["column_ids"]))
            need(integer(table["row_offset"], selected["row_offset"], selected["row_offset"]) and table["ordering"] == ORDERINGS[engine] and type(table["has_more"]) is bool)
            rows = table["rows"]; ids = table["row_ids"]
            need(isinstance(rows, list) and len(rows) <= selected["row_limit"] and m["rows_returned"] == len(rows))
            need(isinstance(ids, list) and len(ids) == len(rows) and all(exact_integer(v) and int(v) >= 0 for v in ids) and all(int(a) < int(b) for a, b in zip(ids, ids[1:])))
            need(not table["has_more"] or len(rows) == selected["row_limit"])
            count = {"nonfinite": 0, "text-omitted": 0, "blob": 0}
            for row in rows:
                need(isinstance(row, list) and len(row) == len(selected["columns"]))
                for column_id, cell in zip(selected["columns"], row):
                    validate_cell(cell)
                    dtype = target["columns"][column_id]["data_type"]
                    allowed = {dtype.lower(), "null"}
                    if dtype == "TEXT": allowed.add("text-omitted")
                    if dtype in {"REAL", "DATE", "TIME", "TIMESTAMP"}: allowed.add("nonfinite")
                    need(cell["type"] in allowed)
                    if cell["type"] in count: count[cell["type"]] += 1
            need(m["nonfinite_values"] == count["nonfinite"] and m["omitted_texts"] == count["text-omitted"] and m["blob_values"] == count["blob"])
            need(value["sampled"] == bool(selected["row_offset"] or table["has_more"] or len(selected["columns"]) != len(target["columns"])))
        need(integer(limit, 1, MAX_OUTPUT) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8")) <= limit)
        return value
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError, AttributeError):
        raise DatabaseTableError(ERROR) from None
