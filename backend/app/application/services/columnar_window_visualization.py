"""Pure validation of the approved columnar window result; no Arrow import."""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation

MAX_SOURCE = 8 * 1024**3
MAX_TOTAL = 8 * 1024**2
MAX_OUTPUT = 2 * 1024**2
SEMANTICS = "source order; integers and decimals are exact strings; nonfinite floats are null; no SQL or aggregation"
WARNING = "仅显示所选列和行窗口，不排序、不聚合；整数和小数以精确文本保留，非有限浮点值显示为空。"
LIMITS = {"max_rows": 200, "max_columns": 32, "max_schema_columns": 128,
          "max_metadata_bytes": 1048576, "max_block_bytes": 4194304,
          "max_decoded_bytes": 16777216, "max_scan_rows": 262144}
TYPES = {"bool", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "float32", "float64", "string", "decimal128"}


class ColumnarWindowError(ValueError): pass


def _fail(): raise ColumnarWindowError("列式窗口响应、选择或读取预算无效。")


def _int(value, maximum=2**53 - 1, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def _keys(value, keys): return isinstance(value, dict) and set(value) == set(keys)


def validate_columnar_window_options(kind, options):
    if kind not in ("tree", "table") or not isinstance(options, dict): _fail()
    if kind == "tree":
        if options: _fail()
        return {}
    if (not _keys(options, {"columns", "row_offset", "row_limit"})
        or not isinstance(options["columns"], list) or not 1 <= len(options["columns"]) <= 32
        or any(not _int(c, 127) for c in options["columns"])
        or len(set(options["columns"])) != len(options["columns"])
        or not _int(options["row_offset"]) or not _int(options["row_limit"], 200, 1)): _fail()
    return {"columns": list(options["columns"]), "row_offset": options["row_offset"], "row_limit": options["row_limit"]}


def _label(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and not re.search(r"[<>\x00-\x1f\x7f]|(?:/Users/|/home/|/private/|/tmp/|https?://|file:|[A-Za-z]:\\)", value, re.I)


def _cell(value, column):
    if value is None: return column["nullable"] or column["type"].startswith("float")
    typ = column["type"]
    if typ == "bool": return type(value) is bool
    if typ.startswith(("int", "uint")):
        if not isinstance(value, str) or len(value) > 21 or not re.fullmatch(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)", value): return False
        bits = int(typ.removeprefix("uint").removeprefix("int")); number = int(value)
        return 0 <= number < 2**bits if typ.startswith("u") else -(2**(bits-1)) <= number < 2**(bits-1)
    if typ.startswith("float"):
        return type(value) in (int, float) and (type(value) is not int or abs(value) <= 2**53-1) and math.isfinite(value)
    if typ == "string":
        try: return isinstance(value, str) and len(value.encode("utf-8")) <= 2048
        except UnicodeError: return False
    if typ == "decimal128":
        if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value): return False
        if (len(value.split(".")[1]) if "." in value else 0) != column["scale"]: return False
        try:
            number = Decimal(value)
            return number.is_finite() and len(number.as_tuple().digits) <= column["precision"]
        except InvalidOperation: return False
    return False


def validate_columnar_window_payload(result, *, kind=None, options=None, fmt=None,
                                     source_bytes=None, read_bytes=None, read_requests=None):
    if not isinstance(result, dict) or result.get("kind") not in ("tree", "table"): _fail()
    view = result["kind"]
    if (not _keys(result, {"contract_version", "type", "reader", "kind", "media_type", "choices", "selected", "metadata", "warnings", "sampled", view})
        or type(result["contract_version"]) is not int or result["contract_version"] != 2
        or result["type"] != "columnar-window" or result["reader"] != "columnar-window"
        or result["media_type"] != "application/json" or result["warnings"] != [WARNING]
        or type(result["sampled"]) is not bool or kind is not None and kind != view): _fail()
    selected = validate_columnar_window_options(view, result["selected"])
    if options is not None and selected != validate_columnar_window_options(view, options): _fail()
    choices = result["choices"]
    if not _keys(choices, {"columns"}) or not isinstance(choices["columns"], list) or not 1 <= len(choices["columns"]) <= 128: _fail()
    columns = choices["columns"]
    for index, column in enumerate(columns):
        if (not _keys(column, {"id", "label", "type", "nullable", "precision", "scale"})
            or not _int(column["id"], 127) or column["id"] != index or not _label(column["label"])
            or not isinstance(column["type"], str) or column["type"] not in TYPES or type(column["nullable"]) is not bool): _fail()
        if column["type"] == "decimal128":
            if not _int(column["precision"], 38, 1) or not _int(column["scale"], column["precision"]): _fail()
        elif column["precision"] is not None or column["scale"] is not None: _fail()
    m = result["metadata"]
    if (not _keys(m, {"format", "container", "input_mode", "value_semantics", "source_bytes", "read_bytes", "read_requests", "total_rows", "total_columns", "total_groups", "metadata_bytes", "groups_read", "scan_rows", "decoded_bytes", "blocks_checked", "nonfinite_values", "limits"})
        or not isinstance(m["format"], str) or m["format"] not in {"parquet", "parq", "arrow", "feather"}
        or m["container"] != ("Parquet" if m["format"] in {"parquet", "parq"} else "Arrow IPC file")
        or m["input_mode"] != "window" or m["value_semantics"] != SEMANTICS
        or not _int(m["source_bytes"], MAX_SOURCE, 16) or not _int(m["read_bytes"], MAX_TOTAL, 1)
        or not _int(m["read_requests"], 128, 1) or m["read_bytes"] < m["read_requests"]
        or not _int(m["total_rows"]) or not _int(m["total_columns"], 128, 1) or m["total_columns"] != len(columns)
        or not _int(m["total_groups"], 1024 if m["container"] == "Parquet" else 64)
        or not _int(m["metadata_bytes"], 1048576, 1) or m["metadata_bytes"] > m["source_bytes"]
        or not _int(m["groups_read"], min(32, m["total_groups"])) or not _int(m["scan_rows"], min(m["total_rows"], 262144))
        or not _int(m["decoded_bytes"], 16777216) or not _int(m["blocks_checked"], 32 * 3 * 128)
        or not _int(m["nonfinite_values"], 200 * 32)
        or not _keys(m["limits"], LIMITS) or any(type(v) is not int or v != LIMITS[k] for k, v in m["limits"].items())): _fail()
    for key, expected in {"source_bytes": source_bytes, "read_bytes": read_bytes, "read_requests": read_requests}.items():
        if expected is not None and (type(expected) is not int or m[key] != expected): _fail()
    if fmt is not None and m["format"] != fmt: _fail()
    if view == "tree":
        expected = [{"path": "/column-" + str(c["id"]), "node_type": "column", "attributes": {"label": c["label"], "type": c["type"]}} for c in columns]
        if result["tree"] != expected or result["sampled"] or any(m[k] for k in ("groups_read", "scan_rows", "decoded_bytes", "blocks_checked", "nonfinite_values")): _fail()
    else:
        if any(c >= len(columns) for c in selected["columns"]) or selected["row_offset"] > m["total_rows"] or selected["row_offset"] == m["total_rows"] != 0: _fail()
        rows_count = min(selected["row_limit"], m["total_rows"] - selected["row_offset"])
        t = result["table"]
        if (not _keys(t, {"columns", "rows", "row_offset", "total_rows", "total_columns"})
            or t["columns"] != [columns[c]["label"] for c in selected["columns"]]
            or not _int(t["row_offset"]) or t["row_offset"] != selected["row_offset"]
            or not _int(t["total_rows"]) or t["total_rows"] != m["total_rows"]
            or not _int(t["total_columns"]) or t["total_columns"] != len(columns)
            or not isinstance(t["rows"], list) or len(t["rows"]) != rows_count
            or result["sampled"] != (rows_count != m["total_rows"] or len(selected["columns"]) != len(columns))
            or m["scan_rows"] < rows_count or rows_count and not m["groups_read"]): _fail()
        float_nulls = 0
        for row in t["rows"]:
            if not isinstance(row, list) or len(row) != len(selected["columns"]): _fail()
            for value, c in zip(row, selected["columns"]):
                if not _cell(value, columns[c]): _fail()
                float_nulls += value is None and columns[c]["type"].startswith("float")
        if m["nonfinite_values"] > float_nulls: _fail()
    try:
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT: _fail()
    except (ValueError, TypeError, UnicodeError): _fail()
    return result
