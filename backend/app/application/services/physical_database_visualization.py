"""Exact, bounded physical-file contract. No logical database state claims."""
from __future__ import annotations

import json
import re

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
READERS = {"mysql-sdi": {"ibd"}, "sst-records": {"sst", "ldb"}}
VERSIONS = {"mysql-sdi": "8.0.46", "sst-records": "6.11.4"}
COLUMNS = {
    "mysql-sdi": ["category", "table", "name", "definition"],
    "sst-records": ["key_hex", "key_bytes", "sequence", "record_type", "value_hex", "value_bytes", "omitted"],
}
WARNINGS = {
    "mysql-sdi": "仅预览未提交的 SDI 结构；未读取表行、redo 或 undo，不代表事务一致或可恢复。",
    "sst-records": "仅预览单文件的点记录；不合并其他 SST、WAL 或删除范围，不代表数据库最新逻辑状态。",
}
ERROR = "物理数据库文件的格式、版本、校验或读取预算不符合受限只读协议。"
LIMITS = {"max_input_bytes": MAX_INPUT, "max_output_bytes": MAX_OUTPUT,
          "max_rows": 1024, "max_key_bytes": 128, "max_value_bytes": 512}


def need(ok):
    if not ok: raise ValueError(ERROR)


def exact(value, fields):
    return type(value) is dict and set(value) == set(fields.split())


def integer(v, lo=0, hi=MAX_INPUT):
    return type(v) is int and lo <= v <= hi


def uint(v, hi):
    return type(v) is str and len(v) <= 20 and re.fullmatch(r"0|[1-9][0-9]*", v) is not None and int(v) <= hi


def label(v):
    return type(v) is str and 1 <= len(v) <= 128 and v == v.strip() and re.fullmatch(r"[\w .()-]+", v) is not None


def display(v):
    return v if label(v) else "名称已省略"


def options_for(reader, kind, options):
    need(type(reader) is str and reader in READERS and kind == "tree" and exact(options, ""))
    return {}


def validate_physical_payload(value, *, reader=None, kind=None, options=None, fmt=None, size=None, limit=MAX_OUTPUT):
    try:
        need(exact(value, "type reader kind contract_version media_type table metadata warnings sampled choices selected"))
        r = value["reader"]
        options_for(r, value["kind"], value["selected"])
        need(value["type"] == r and (reader is None or reader == r) and (kind is None or kind == "tree"))
        if options is not None: options_for(r, "tree", options)
        need(type(value["contract_version"]) is int and value["contract_version"] == 2)
        need(value["media_type"] == "application/json" and exact(value["choices"], ""))
        need(type(value["warnings"]) is list and value["warnings"] == [WARNINGS[r]])
        m = value["metadata"]
        need(exact(m, "engine format source_bytes input_mode tool_version rows_returned logical_state_verified limits"))
        need(m["engine"] == r and type(m["format"]) is str and m["format"] in READERS[r])
        need(fmt is None or m["format"] == fmt)
        need(integer(m["source_bytes"], 48) and (size is None or type(size) is int and m["source_bytes"] == size))
        need(m["input_mode"] == "whole" and m["tool_version"] == VERSIONS[r] and m["logical_state_verified"] is False)
        need(exact(m["limits"], " ".join(LIMITS)) and m["limits"] == LIMITS and all(type(x) is int for x in m["limits"].values()))
        table = value["table"]
        need(exact(table, "columns rows") and type(table["columns"]) is list and table["columns"] == COLUMNS[r])
        rows = table["rows"]
        need(type(rows) is list and len(rows) <= 1024 and integer(m["rows_returned"], len(rows), len(rows)))
        omitted = False
        for row in rows:
            need(type(row) is list and len(row) == len(COLUMNS[r]) and all(type(x) is str for x in row))
            if r == "mysql-sdi":
                category, parent, name, definition = row
                need(category in {"table", "column", "index"} and label(parent) and label(name))
                need(len(definition) <= 512)
                if category == "table": need(parent == "-" and definition == "InnoDB")
                elif category == "column":
                    need(re.fullmatch(r"type=[a-z0-9(), _]+;nullable=(true|false);ordinal=[1-9][0-9]{0,3};hidden=[1-4]", definition) is not None)
                else:
                    need(re.fullmatch(r"type=[1-5];columns=[0-9]{1,3}(,[0-9]{1,3}){0,127}", definition) is not None)
                omitted |= "名称已省略" in row
            else:
                key, kb, seq, rt, val, vb, omission = row
                need(uint(kb, MAX_INPUT) and uint(vb, MAX_INPUT) and uint(seq, 2**56-1) and rt in {"deletion", "value", "merge", "single-deletion"})
                need(omission in {"none", "key", "value", "both"})
                key_missing, val_missing = int(kb) > 128, int(vb) > 512
                need(omission == ("both" if key_missing and val_missing else "key" if key_missing else "value" if val_missing else "none"))
                for text, length, missing in ((key, int(kb), key_missing), (val, int(vb), val_missing)):
                    need(len(text) == (0 if missing else 2*length) and re.fullmatch(r"[0-9a-f]*", text) is not None)
                if rt in {"deletion", "single-deletion"}: need(val == "" and vb == "0")
                omitted |= omission != "none"
        need(type(value["sampled"]) is bool and value["sampled"] == omitted)
        need(integer(limit, 1, MAX_OUTPUT) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= limit)
        return value
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        raise ValueError(ERROR) from None


def build_payload(reader, fmt, size, rows):
    sampled = any(("名称已省略" in row) if reader == "mysql-sdi" else row[-1] != "none" for row in rows)
    return validate_physical_payload({"type": reader, "reader": reader, "kind": "tree", "contract_version": 2,
        "media_type": "application/json", "choices": {}, "selected": {}, "warnings": [WARNINGS[reader]], "sampled": sampled,
        "table": {"columns": list(COLUMNS[reader]), "rows": rows}, "metadata": {"engine": reader, "format": fmt,
        "source_bytes": size, "input_mode": "whole", "tool_version": VERSIONS[reader], "rows_returned": len(rows),
        "logical_state_verified": False, "limits": dict(LIMITS)}})
