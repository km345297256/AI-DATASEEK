"""Pure, strict contract for SQL *literal* previews; never restored DB values."""
from __future__ import annotations

import hashlib
import json
import re

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
DIALECTS = {"postgres", "mysql", "sqlite"}
LIMITS = {"max_statements": 4096, "max_statement_bytes": 1048576, "max_depth": 32,
          "max_tables": 32, "max_schema_columns": 128, "max_schema_bytes": 131072,
          "max_columns": 16, "max_rows": 200, "max_offset": 100000,
          "max_cell_bytes": 512, "max_number_bytes": 128, "max_source_rows": 1000000}
SEMANTICS = "Source SQL literals only; COPY fields remain text; no execution, type coercion or restored database state"
WARNING = "仅显示受支持 CREATE TABLE、INSERT VALUES 和 PostgreSQL COPY 文本中的结构与字面量；不执行任何 SQL，也不计算类型转换、默认值、约束、触发器或其他语句。行数与顺序来自转储片段，不代表恢复后的数据库状态。未支持的结构或行语法会拒绝，其他跳过语句只计数。"
ERROR = "SQL 转储方言、语法子集、目录、分页或读取预算不符合受限只读协议。"
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)
_NAME = re.compile(r"\w[\w .()-]{0,127}\Z", re.UNICODE)
_TYPE = re.compile(r"[A-Z][A-Z0-9_ ]{0,95}(?:\([0-9]{1,5}(?:, ?[0-9]{1,5})?\))?\Z")


class SqlDumpError(ValueError): pass


def need(ok):
    if not ok: raise SqlDumpError(ERROR)


def integer(value, lo=0, hi=2**53-1): return type(value) is int and lo <= value <= hi
def keys(value, names): return isinstance(value, dict) and set(value) == set(names.split())
def safe_text(value): return type(value) is str and len(value.encode("utf8", errors="strict")) <= 512 and _UNSAFE.search(value) is None
def name(value): return safe_text(value) and len(value.encode("utf8")) <= 128 and _NAME.fullmatch(value) is not None and value == value.strip()
def table_id(dialect, label): return "s-" + hashlib.sha256((dialect + "\0" + label).encode("utf8")).hexdigest()[:24]


def validate_sql_dump_options(kind, options):
    need(type(kind) is str and kind in {"tree", "table"} and isinstance(options, dict))
    need(keys(options, "dialect" if kind == "tree" else "dialect table columns row_offset row_limit"))
    need(type(options["dialect"]) is str and options["dialect"] in DIALECTS)
    if kind == "tree": return {"dialect": options["dialect"]}
    need(type(options["table"]) is str and re.fullmatch(r"s-[0-9a-f]{24}", options["table"]) is not None)
    columns = options["columns"]
    need(isinstance(columns, list) and 1 <= len(columns) <= 16 and all(integer(v, 0, 127) for v in columns) and len(set(columns)) == len(columns))
    need(integer(options["row_offset"], 0, 100000) and integer(options["row_limit"], 1, 200))
    return {"dialect": options["dialect"], "table": options["table"], "columns": list(columns), "row_offset": options["row_offset"], "row_limit": options["row_limit"]}


def validate_cell(cell):
    need(isinstance(cell, dict)); typ = cell.get("type")
    need(type(typ) is str)
    if typ in {"number-literal", "text", "null", "boolean"}:
        need(keys(cell, "type value")); val = cell["value"]
        if typ == "number-literal": need(type(val) is str and len(val) <= 128 and NUMBER.fullmatch(val) is not None)
        elif typ == "text": need(safe_text(val))
        elif typ == "null": need(val is None)
        else: need(type(val) is bool)
    elif typ == "blob": need(keys(cell, "type bytes") and integer(cell["bytes"], 0, MAX_INPUT))
    elif typ == "text-omitted":
        need(keys(cell, "type bytes reason") and integer(cell["bytes"], 0, MAX_INPUT))
        need(cell["reason"] == ("cell-budget" if cell["bytes"] > 512 else "unsafe-text"))
    else: need(False)


def build_sql_dump_payload(size, kind, selected, tables, stats, page=None):
    payload = {"contract_version": 2, "type": "sql-dump", "reader": "sql-dump", "kind": kind,
        "media_type": "application/json", "choices": {"tables": tables}, "selected": selected,
        "metadata": {"dialect": selected["dialect"], "format": "sql", "input_mode": "whole", "source_bytes": size,
            **stats, "value_semantics": SEMANTICS, "ordering": "source-literal-order", "limits": dict(LIMITS)},
        "warnings": [WARNING], "sampled": False}
    if kind == "tree":
        payload["tree"] = [{"path": "/" + t["id"], "node_type": "table", "attributes": {"label": t["label"], "columns": len(t["columns"])}} for t in tables]
    else:
        payload["table"] = page
        target = next((t for t in tables if t["id"] == selected["table"]), None); need(target is not None)
        payload["sampled"] = bool(selected["row_offset"] or page["has_more"] or len(selected["columns"]) != len(target["columns"]))
    return validate_sql_dump_payload(payload, kind=kind, options=selected, fmt="sql", size=size)


def validate_sql_dump_payload(value, *, kind=None, options=None, fmt=None, size=None, limit=MAX_OUTPUT):
    try:
        need(isinstance(value, dict)); view = value.get("kind")
        selected = validate_sql_dump_options(view, value.get("selected"))
        need(kind is None or view == kind)
        need(options is None or selected == validate_sql_dump_options(view, options))
        need(fmt is None or fmt == "sql")
        need(keys(value, "contract_version type reader kind media_type choices selected metadata warnings sampled " + view))
        need(type(value["contract_version"]) is int and value["contract_version"] == 2 and value["type"] == value["reader"] == "sql-dump")
        need(value["media_type"] == "application/json" and value["warnings"] == [WARNING] and type(value["sampled"]) is bool)
        m = value["metadata"]
        need(keys(m, "dialect format input_mode source_bytes statement_count ignored_statement_count source_rows schema_bytes value_semantics ordering limits"))
        need(m["dialect"] == selected["dialect"] and m["format"] == "sql" and m["input_mode"] == "whole")
        need(integer(m["source_bytes"], 1, MAX_INPUT) and (size is None or type(size) is int and m["source_bytes"] == size))
        need(integer(m["statement_count"], 1, 4096) and integer(m["ignored_statement_count"], 0, m["statement_count"]))
        need(integer(m["source_rows"], 0, 1000000) and integer(m["schema_bytes"], 1, 131072))
        need(m["value_semantics"] == SEMANTICS and m["ordering"] == "source-literal-order")
        need(keys(m["limits"], " ".join(LIMITS)) and m["limits"] == LIMITS and all(type(n) is int for n in m["limits"].values()))
        need(keys(value["choices"], "tables")); tables = value["choices"]["tables"]
        need(isinstance(tables, list) and 1 <= len(tables) <= 32)
        labels = []
        for item in tables:
            need(keys(item, "id label columns")); label = item["label"]
            need(name(label) and item["id"] == table_id(selected["dialect"], label)); labels.append(label)
            columns = item["columns"]; need(isinstance(columns, list) and 1 <= len(columns) <= 128)
            names = []
            for i, column in enumerate(columns):
                need(keys(column, "id label declared_type"))
                need(integer(column["id"], i, i) and name(column["label"]))
                need(type(column["declared_type"]) is str and _TYPE.fullmatch(column["declared_type"]) is not None)
                names.append(column["label"].casefold())
            need(len(set(names)) == len(names))
        need(labels == sorted(labels) and len(set(s.casefold() for s in labels)) == len(labels))
        if view == "tree":
            expected = [{"path": "/" + t["id"], "node_type": "table", "attributes": {"label": t["label"], "columns": len(t["columns"])}} for t in tables]
            need(value["tree"] == expected and all(type(n["attributes"]["columns"]) is int for n in value["tree"]))
            need(value["sampled"] is False)
        else:
            target = next((t for t in tables if t["id"] == selected["table"]), None); need(target is not None)
            need(all(c < len(target["columns"]) for c in selected["columns"]))
            t = value["table"]; need(keys(t, "column_ids columns rows row_ids row_offset has_more"))
            need(t["column_ids"] == selected["columns"] and all(type(c) is int for c in t["column_ids"]))
            need(t["columns"] == [target["columns"][c]["label"] for c in selected["columns"]])
            need(integer(t["row_offset"], selected["row_offset"], selected["row_offset"]) and type(t["has_more"]) is bool)
            rows = t["rows"]; ids = t["row_ids"]
            need(isinstance(rows, list) and len(rows) <= selected["row_limit"] and len(rows) <= m["source_rows"])
            need(isinstance(ids, list) and ids == [str(i) for i in range(selected["row_offset"], selected["row_offset"] + len(rows))])
            need(not t["has_more"] or len(rows) == selected["row_limit"])
            for row in rows:
                need(isinstance(row, list) and len(row) == len(selected["columns"]))
                for cell in row: validate_cell(cell)
            need(value["sampled"] == bool(selected["row_offset"] or t["has_more"] or len(selected["columns"]) != len(target["columns"])))
        need(integer(limit, 1, MAX_OUTPUT) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8")) <= limit)
        return value
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError, AttributeError):
        raise SqlDumpError(ERROR) from None
