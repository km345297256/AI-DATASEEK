"""Bounded BSON document-stream reader. No MongoDB client or code evaluation.

Wire layout: https://bsonspec.org/spec.html. Only Decimal128's fixed 16-byte
value delegates to official PyMongo 4.17.0, not its unrestricted document
decoder. Duplicate keys and noncanonical array indexes fail closed.
"""
from __future__ import annotations

import struct
import time
from decimal import DecimalException

from .database_records_payload import (
    DatabaseRecordsError, LIMITS, MAX_INPUT, build_database_records_payload,
    float_cell, group_id, need, node_key, text_cell,
    validate_database_records_options,
)


class _Document:
    def __init__(self, data, deadline):
        self.data = data; self.deadline = deadline; self.work = 0; self.nodes = []

    def take(self, pos, size, end):
        need(type(size) is int and size >= 0 and pos + size <= end)
        return self.data[pos:pos + size], pos + size

    def cstring(self, pos, end):
        stop = self.data.find(b"\0", pos, end); need(stop >= pos)
        value = self.data[pos:stop].decode("utf8", "strict")
        return value, stop + 1

    def string(self, pos, end):
        raw, pos = self.take(pos, 4, end); length = int.from_bytes(raw, "little", signed=True)
        need(length >= 1); raw, pos = self.take(pos, length, end); need(raw[-1] == 0)
        return raw[:-1].decode("utf8", "strict"), pos

    def add(self, parent, key, cell, visible):
        self.work += 1
        need(self.work <= 128 and time.monotonic() <= self.deadline)
        if not visible: return None
        key, omitted = node_key(key); index = len(self.nodes)
        self.nodes.append({"id": index, "parent": parent, "key": key, "key_omitted": omitted, "cell": cell})
        return index

    def document(self, pos, end, parent=None, key=None, depth=0, array=False, visible=True):
        need(depth <= 8); start = pos
        raw, pos = self.take(pos, 4, end); length = int.from_bytes(raw, "little", signed=True)
        need(5 <= length <= LIMITS["max_document_bytes"] and start + length <= end)
        stop = start + length; need(self.data[stop - 1] == 0)
        cell = {"type": "array" if array else "document", "count": 0}
        node = self.add(parent, key, cell, visible); names = set(); count = 0
        while pos < stop - 1:
            typ = self.data[pos]; pos += 1; need(typ != 0)
            name, pos = self.cstring(pos, stop - 1); need(name not in names); names.add(name)
            if array: need(name == str(count))
            pos = self.value(typ, pos, stop - 1, node, name, depth + 1, visible)
            count += 1; need(count <= 127)
        need(pos == stop - 1); cell["count"] = count
        return stop

    def value(self, typ, pos, end, parent, key, depth, visible):
        need(depth <= 8)
        if typ in (3, 4): return self.document(pos, end, parent, key, depth, typ == 4, visible)
        if typ == 1:
            raw, pos = self.take(pos, 8, end); cell = float_cell(struct.unpack("<d", raw)[0])
        elif typ == 2:
            value, pos = self.string(pos, end); cell = text_cell(value)
        elif typ == 5:
            raw, pos = self.take(pos, 4, end); length = int.from_bytes(raw, "little", signed=True); need(length >= 0)
            raw, pos = self.take(pos, 1, end); subtype = raw[0]
            if subtype == 2:
                need(length >= 4); raw, pos = self.take(pos, 4, end)
                inner = int.from_bytes(raw, "little", signed=True); need(inner == length - 4); length = inner
            _, pos = self.take(pos, length, end)
            cell = {"type": "binary", "bytes": length, "subtype": f"{subtype:02x}"}
        elif typ == 7:
            raw, pos = self.take(pos, 12, end); cell = {"type": "objectid", "value": raw.hex()}
        elif typ == 8:
            raw, pos = self.take(pos, 1, end); need(raw[0] in (0, 1)); cell = {"type": "boolean", "value": bool(raw[0])}
        elif typ in (9, 16, 18):
            raw, pos = self.take(pos, 4 if typ == 16 else 8, end)
            cell = {"type": "date-ms" if typ == 9 else "integer", "value": str(int.from_bytes(raw, "little", signed=True))}
        elif typ == 10: cell = {"type": "null", "value": None}
        elif typ == 17:
            raw, pos = self.take(pos, 8, end)
            cell = {"type": "timestamp", "seconds": str(int.from_bytes(raw[4:], "little")), "increment": str(int.from_bytes(raw[:4], "little"))}
        elif typ == 19:
            from bson.decimal128 import Decimal128
            raw, pos = self.take(pos, 16, end)
            cell = {"type": "decimal128", "value": str(Decimal128.from_bid(raw)), "bid": raw.hex()}
        elif typ in (6, 255, 127):
            cell = {"type": "unsupported", "name": {6: "undefined", 255: "min-key", 127: "max-key"}[typ]}
        elif typ == 11:
            _, pos = self.cstring(pos, end); flags, pos = self.cstring(pos, end)
            need(set(flags) <= set("ilmsux") and flags == "".join(sorted(set(flags))))
            cell = {"type": "unsupported", "name": "regex"}
        elif typ == 12:
            _, pos = self.string(pos, end); _, pos = self.take(pos, 12, end)
            cell = {"type": "unsupported", "name": "dbpointer"}
        elif typ in (13, 14):
            _, pos = self.string(pos, end); cell = {"type": "unsupported", "name": "javascript" if typ == 13 else "symbol"}
        elif typ == 15:
            start = pos; raw, pos = self.take(pos, 4, end); length = int.from_bytes(raw, "little", signed=True)
            need(length >= 14 and start + length <= end)
            _, pos = self.string(pos, start + length)
            pos = self.document(pos, start + length, depth=depth, visible=False)
            need(pos == start + length); cell = {"type": "unsupported", "name": "javascript-scope"}
        else: need(False)
        self.add(parent, key, cell, visible)
        return pos


def bson_preview(data, fmt="bson", kind="tree", options=None):
    options = {} if options is None else options
    selected = validate_database_records_options(kind, options)
    need(fmt == "bson" and type(data) is bytes and 5 <= len(data) <= MAX_INPUT)
    identifier = group_id("bson", None)
    if kind == "table": need(selected["group_id"] == identifier)
    try:
        deadline = time.monotonic() + 12; position = 0; count = 0; work = 0; records = []; page_nodes = 0
        while position < len(data):
            doc = _Document(data, deadline); position = doc.document(position, len(data))
            work += doc.work; need(work <= LIMITS["max_scan_nodes"] and count < LIMITS["max_records"])
            if kind == "table" and selected["offset"] <= count < selected["offset"] + selected["limit"]:
                page_nodes += len(doc.nodes); need(page_nodes <= 4096)
                records.append({"index": str(count), "key": None, "expires_at_ms": None, "nodes": doc.nodes,
                                "truncated": any(n["key_omitted"] or n["cell"]["type"] == "omitted" for n in doc.nodes)})
            count += 1
        groups = [{"id": identifier, "label": "Documents", "database": None, "record_count": count,
                   "counts": {"document": count} if count else {}}]
        return build_database_records_payload("bson", len(data), kind, selected, groups, records=records)
    except (ValueError, TypeError, IndexError, OverflowError, MemoryError, UnicodeError, ImportError, DecimalException):
        raise DatabaseRecordsError("BSON 文件不符合受限只读预览要求。") from None


# Explicit exported entry point; keep original concise name for test helpers.
bson_records_preview = bson_preview
