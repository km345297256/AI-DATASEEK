"""Pure fail-closed contract for BSON / Redis file records; no database client.

Keep this file byte-identical to the backend validation copy.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from .decimal128_value import validate_decimal128

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
READERS = {"bson": "bson", "redis-rdb": "rdb"}
CONTAINERS = {"bson": "BSON document stream", "redis-rdb": "Redis RDB"}
ORDERINGS = {"bson": "bson-document-order", "redis-rdb": "rdb-file-order"}
LIMITS = {"max_groups": 32, "max_records": 100000, "max_rows": 50, "max_offset": 100000,
          "max_depth": 8, "max_record_nodes": 128, "max_page_nodes": 4096,
          "max_scan_nodes": 100000, "max_text_bytes": 512, "max_document_bytes": 1048576,
          "max_decompressed_block_bytes": 1048576, "max_decompressed_bytes": 33554432}
CONTAINER_TYPES = {"document", "array", "hash", "list", "set", "zset", "entry"}
BSON_TYPES = {"document", "array", "null", "boolean", "integer", "real", "nonfinite", "decimal128",
              "text", "date-ms", "timestamp", "objectid", "binary", "omitted", "unsupported"}
REDIS_BYTES_TYPES = {"text", "binary", "omitted"}
UNSUPPORTED = {"regex", "javascript", "javascript-scope", "dbpointer", "symbol", "undefined", "min-key", "max-key"}
RECORD_TYPES = {"bson": {"document"}, "redis-rdb": {"string", "list", "set", "hash", "zset"}}
WARNING = "整文件受限只读预览（最多 16 MiB），不是数据库连接或恢复接口。仅显示手动选择的一页；整数、Decimal128 和毫秒时间保留精确表示，二进制仅显示长度。过长或不安全文本明确省略，不执行代码、正则、脚本或数据库命令。Redis 过期键保留文件中的原始过期时刻，不按当前时间过滤。"
ERROR = "记录文件、分组、分页或读取预算不符合受限只读协议。"
_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)
_INT = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_DECIMAL = re.compile(r"(?:-?(?:Infinity|NaN)|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:E[+-][0-9]{1,4})?)\Z")


class DatabaseRecordsError(ValueError): pass


def need(value):
    if not value: raise DatabaseRecordsError(ERROR)


def keys(value, fields): return isinstance(value, dict) and set(value) == set(fields.split())
def integer(value, low=0, high=2**53-1): return type(value) is int and low <= value <= high
def int_text(value, low=-(2**63), high=2**63-1):
    return type(value) is str and len(value) <= 21 and _INT.fullmatch(value) is not None and low <= int(value) <= high
def safe_text(value):
    return type(value) is str and len(value.encode("utf8", "strict")) <= 512 and not _UNSAFE.search(value)
def group_id(reader, database):
    return "g-" + hashlib.sha256((reader + "\0" + str(database)).encode("ascii")).hexdigest()[:24]


def validate_database_records_options(kind, options):
    need(type(kind) is str and kind in {"tree", "table"} and isinstance(options, dict))
    if kind == "tree": need(not options); return {}
    need(keys(options, "group_id offset limit"))
    need(type(options["group_id"]) is str and re.fullmatch(r"g-[0-9a-f]{24}", options["group_id"]) is not None)
    need(integer(options["offset"], 0, 100000) and integer(options["limit"], 1, 50))
    return dict(options)


def validate_cell(cell):
    need(isinstance(cell, dict)); typ = cell.get("type"); need(type(typ) is str)
    if typ in CONTAINER_TYPES:
        need(keys(cell, "type count") and integer(cell["count"], 0, 127))
    elif typ == "binary":
        need(keys(cell, "type bytes subtype") and integer(cell["bytes"], 0, MAX_INPUT))
        need(cell["subtype"] is None or type(cell["subtype"]) is str and re.fullmatch(r"[0-9a-f]{2}", cell["subtype"]) is not None)
    elif typ == "omitted":
        need(keys(cell, "type bytes reason") and integer(cell["bytes"], 0, MAX_INPUT))
        need(cell["reason"] == ("text-budget" if cell["bytes"] > 512 else "unsafe-text"))
    elif typ == "unsupported": need(keys(cell, "type name") and type(cell["name"]) is str and cell["name"] in UNSUPPORTED)
    elif typ == "timestamp":
        need(keys(cell, "type seconds increment") and int_text(cell["seconds"], 0, 2**32-1) and int_text(cell["increment"], 0, 2**32-1))
    elif typ == "decimal128":
        need(keys(cell, "type value bid") and type(cell["value"]) is str and len(cell["value"]) <= 50 and _DECIMAL.fullmatch(cell["value"]) is not None)
        need(type(cell["bid"]) is str and re.fullmatch(r"[0-9a-f]{32}", cell["bid"]) is not None)
        need(validate_decimal128(cell["value"], cell["bid"]))
    else:
        need(keys(cell, "type value")); value = cell["value"]
        if typ == "null": need(value is None)
        elif typ == "boolean": need(type(value) is bool)
        elif typ in {"integer", "date-ms"}: need(int_text(value))
        elif typ == "real": need(type(value) in {int, float} and math.isfinite(value) and (type(value) is float or abs(value) <= 2**53-1))
        elif typ == "nonfinite": need(type(value) is str and value in {"NaN", "Infinity", "-Infinity"})
        elif typ == "objectid": need(type(value) is str and re.fullmatch(r"[0-9a-f]{24}", value) is not None)
        elif typ == "text": need(safe_text(value))
        else: need(False)


def text_cell(value):
    need(type(value) is str)
    length = len(value.encode("utf8", "strict"))
    if length > 512: return {"type": "omitted", "bytes": length, "reason": "text-budget"}
    if not safe_text(value): return {"type": "omitted", "bytes": length, "reason": "unsafe-text"}
    return {"type": "text", "value": value}


def binary_text_cell(value):
    need(type(value) is bytes)
    try: return text_cell(value.decode("utf8", "strict"))
    except UnicodeError: return {"type": "binary", "bytes": len(value), "subtype": None}


def float_cell(value):
    if math.isfinite(value): return {"type": "real", "value": value}
    return {"type": "nonfinite", "value": "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"}


def node_key(value):
    if value is None: return None, False
    need(type(value) is str)
    return (value, False) if safe_text(value) else ("字段名已省略", True)


def validate_record(record, reader):
    need(keys(record, "index key expires_at_ms nodes truncated") and int_text(record["index"], 0, 99999))
    need(type(record["truncated"]) is bool)
    if reader == "bson": need(record["key"] is None and record["expires_at_ms"] is None)
    else:
        validate_cell(record["key"]); need(record["key"]["type"] in {"text", "omitted", "binary"})
        if record["key"]["type"] == "binary": need(record["key"]["subtype"] is None)
        need(record["expires_at_ms"] is None or int_text(record["expires_at_ms"]))
    nodes = record["nodes"]; need(isinstance(nodes, list) and 1 <= len(nodes) <= 128)
    depths, children = [], [0] * len(nodes)
    active, visible_fields = [], {}
    omitted = int(record["key"] is not None and record["key"]["type"] == "omitted"); unsupported = 0
    for index, node in enumerate(nodes):
        need(keys(node, "id parent key key_omitted cell") and integer(node["id"], index, index) and type(node["key_omitted"]) is bool)
        if index == 0:
            need(node["parent"] is None and node["key"] is None and node["key_omitted"] is False); depths.append(0); active.append(0)
        else:
            need(integer(node["parent"], 0, index - 1) and nodes[node["parent"]]["cell"]["type"] in CONTAINER_TYPES)
            need(node["parent"] in active)
            active = active[:active.index(node["parent"]) + 1] + [index]
            depth = depths[node["parent"]] + 1; need(depth <= 8); depths.append(depth)
            child_index = children[node["parent"]]; children[node["parent"]] += 1
            need(safe_text(node["key"]) and (not node["key_omitted"] or node["key"] == "字段名已省略"))
        validate_cell(node["cell"])
        typ = node["cell"]["type"]
        if reader == "bson":
            need(typ in BSON_TYPES)
            if typ == "binary": need(node["cell"]["subtype"] is not None)
            if index:
                parent_type = nodes[node["parent"]]["cell"]["type"]
                need(parent_type in {"document", "array"})
                if parent_type == "array": need(node["key"] == str(child_index) and not node["key_omitted"])
                elif not node["key_omitted"]:
                    fields = visible_fields.setdefault(node["parent"], set())
                    need(node["key"] not in fields); fields.add(node["key"])
        else:
            if typ == "binary": need(node["cell"]["subtype"] is None)
            if index:
                parent_type = nodes[node["parent"]]["cell"]["type"]
                if parent_type in {"list", "set", "hash"}:
                    need(node["parent"] == 0 and typ in REDIS_BYTES_TYPES)
                    if parent_type != "hash": need(node["key"] == str(child_index) and not node["key_omitted"])
                    elif not node["key_omitted"]:
                        fields = visible_fields.setdefault(0, set()); need(node["key"] not in fields); fields.add(node["key"])
                elif parent_type == "zset":
                    need(node["parent"] == 0 and typ == "entry" and node["cell"]["count"] == 2)
                    need(node["key"] == str(child_index) and not node["key_omitted"])
                else:
                    need(parent_type == "entry" and nodes[0]["cell"]["type"] == "zset" and child_index in (0, 1))
                    need(node["key"] == ("member" if child_index == 0 else "score") and not node["key_omitted"])
                    need(typ in REDIS_BYTES_TYPES if child_index == 0 else typ in {"real", "nonfinite"})
                    if child_index == 1 and typ == "nonfinite": need(node["cell"]["value"] != "NaN")
        omitted += int(node["key_omitted"]) + int(node["cell"]["type"] == "omitted")
        unsupported += int(node["cell"]["type"] == "unsupported")
    for index, node in enumerate(nodes):
        need(children[index] == (node["cell"]["count"] if node["cell"]["type"] in CONTAINER_TYPES else 0))
    if reader == "bson": need(nodes[0]["cell"]["type"] == "document")
    else: need(nodes[0]["cell"]["type"] in {"text", "binary", "omitted", "list", "set", "hash", "zset"})
    need(record["truncated"] == bool(omitted))
    return len(nodes), omitted, unsupported


def build_database_records_payload(reader, size, kind, selected, groups, *, version=None, records=None):
    result = {"contract_version": 2, "type": reader, "reader": reader, "kind": kind,
              "media_type": "application/json", "choices": {"groups": groups}, "selected": selected,
              "metadata": {}, "warnings": [WARNING], "sampled": False}
    records = [] if records is None else records
    if kind == "tree":
        result["tree"] = [{"path": "/" + g["id"], "node_type": "group", "attributes": {"label": g["label"], "records": g["record_count"]}} for g in groups]
    else:
        target = next((g for g in groups if g["id"] == selected["group_id"]), None); need(target is not None)
        more = selected["offset"] + len(records) < target["record_count"]
        result["table"] = {**selected, "has_more": more, "records": records}
        result["sampled"] = bool(selected["offset"] or more or any(r["truncated"] for r in records))
    stats = [validate_record(record, reader) for record in records]
    result["metadata"] = {"engine": reader, "format": READERS[reader], "container": CONTAINERS[reader], "input_mode": "whole", "source_bytes": size,
                          "format_version": version, "ordering": ORDERINGS[reader], "total_records": sum(g["record_count"] for g in groups),
                          "groups_count": len(groups), "records_returned": len(records), "nodes_returned": sum(s[0] for s in stats),
                          "omitted_values": sum(s[1] for s in stats), "unsupported_values": sum(s[2] for s in stats),
                          "checksum": "not-applicable" if reader == "bson" else "verified", "limits": dict(LIMITS)}
    return validate_database_records_payload(result, reader=reader, kind=kind, options=selected, fmt=READERS[reader], size=size)


def validate_database_records_payload(value, *, reader=None, kind=None, options=None, fmt=None, size=None, limit=MAX_OUTPUT):
    try:
        need(isinstance(value, dict)); engine = value.get("reader"); view = value.get("kind")
        need(type(engine) is str and engine in READERS and (reader is None or reader == engine))
        selected = validate_database_records_options(view, value.get("selected"))
        need(kind is None or view == kind); need(options is None or selected == validate_database_records_options(view, options))
        need(keys(value, "contract_version type reader kind media_type choices selected metadata warnings sampled " + view))
        need(type(value["contract_version"]) is int and value["contract_version"] == 2 and value["type"] == engine and value["media_type"] == "application/json")
        need(value["warnings"] == [WARNING] and type(value["sampled"]) is bool)
        m = value["metadata"]
        need(keys(m, "engine format container input_mode source_bytes format_version ordering total_records groups_count records_returned nodes_returned omitted_values unsupported_values checksum limits"))
        need(m["engine"] == engine and m["format"] == READERS[engine] and (fmt is None or fmt == m["format"]) and m["container"] == CONTAINERS[engine] and m["input_mode"] == "whole")
        need(integer(m["source_bytes"], 5 if engine == "bson" else 18, MAX_INPUT) and (size is None or type(size) is int and size == m["source_bytes"]))
        need(m["ordering"] == ORDERINGS[engine] and m["checksum"] == ("not-applicable" if engine == "bson" else "verified"))
        need(m["format_version"] is None if engine == "bson" else integer(m["format_version"], 11, 11))
        need(keys(m["limits"], " ".join(LIMITS)) and m["limits"] == LIMITS and all(type(v) is int for v in m["limits"].values()))
        need(keys(value["choices"], "groups")); groups = value["choices"]["groups"]
        need(isinstance(groups, list) and 1 <= len(groups) <= 32 and integer(m["groups_count"], len(groups), len(groups)))
        databases = []; total = 0
        for group in groups:
            need(keys(group, "id label database record_count counts")); database = group["database"]
            need(database is None if engine == "bson" else integer(database, 0, 1023))
            need(group["id"] == group_id(engine, database) and group["label"] == ("Documents" if engine == "bson" else "Redis DB " + str(database)))
            need(integer(group["record_count"], 0, 100000) and isinstance(group["counts"], dict))
            need(all(type(k) is str and k in RECORD_TYPES[engine] and integer(v, 1, 100000) for k, v in group["counts"].items()))
            need(sum(group["counts"].values()) == group["record_count"]); total += group["record_count"]; databases.append(database)
        need(databases == [None] if engine == "bson" else databases == sorted(set(databases)))
        need(integer(m["total_records"], total, total) and total <= 100000)
        need(integer(m["records_returned"], 0, 50) and integer(m["nodes_returned"], 0, 4096) and integer(m["omitted_values"], 0, 8192) and integer(m["unsupported_values"], 0, 4096))
        if view == "tree":
            need(value["tree"] == [{"path": "/" + g["id"], "node_type": "group", "attributes": {"label": g["label"], "records": g["record_count"]}} for g in groups])
            need(all(type(n["attributes"]["records"]) is int for n in value["tree"]))
            need(value["sampled"] is False and all(m[k] == 0 for k in ("records_returned", "nodes_returned", "omitted_values", "unsupported_values")))
        else:
            matches = [g for g in groups if g["id"] == selected["group_id"]]; need(len(matches) == 1); group = matches[0]
            page = value["table"]; need(keys(page, "group_id offset limit has_more records"))
            need({k: page[k] for k in selected} == selected and type(page["offset"]) is int and type(page["limit"]) is int and type(page["has_more"]) is bool)
            records = page["records"]; expected = min(selected["limit"], max(0, group["record_count"] - selected["offset"]))
            need(isinstance(records, list) and len(records) == expected and m["records_returned"] == expected)
            stats = [validate_record(record, engine) for record in records]
            need([r["index"] for r in records] == [str(i) for i in range(selected["offset"], selected["offset"] + expected)])
            need(m["nodes_returned"] == sum(s[0] for s in stats) and m["omitted_values"] == sum(s[1] for s in stats) and m["unsupported_values"] == sum(s[2] for s in stats))
            need(page["has_more"] == (selected["offset"] + expected < group["record_count"]))
            need(value["sampled"] == bool(selected["offset"] or page["has_more"] or any(r["truncated"] for r in records)))
        need(integer(limit, 1, MAX_OUTPUT) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf8")) <= limit)
        return value
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, AttributeError):
        raise DatabaseRecordsError(ERROR) from None
