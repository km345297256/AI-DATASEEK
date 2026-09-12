"""Strict offline Redis RDB 11 subset; never RESTORE or start a server.

The adapter interprets the documented wire encodings in Redis 7.2 rdb.h,
listpack.c and intset.c. This is a purpose-built bounded Python parser, not
redis-rdb-tools' unbounded JSON/decompression path or a database driver.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
import time

from .database_records_payload import (
    DatabaseRecordsError, LIMITS, MAX_INPUT, binary_text_cell,
    build_database_records_payload, float_cell, group_id, need, node_key,
    validate_database_records_options,
)

_TYPES = {0: "string", 1: "list", 2: "set", 3: "zset", 4: "hash", 5: "zset",
          11: "set", 16: "hash", 17: "zset", 18: "list", 20: "set"}


def _crc_table():
    table = []
    for value in range(256):
        for _ in range(8): value = (value >> 1) ^ (0x95AC9329AC4BC9B5 if value & 1 else 0)
        table.append(value)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc64(data, deadline=None):
    value = 0
    for index, byte in enumerate(data):
        value = _CRC_TABLE[(value ^ byte) & 255] ^ (value >> 8)
        if deadline is not None and index % 65536 == 0: need(time.monotonic() <= deadline)
    return value


class _Cursor:
    def __init__(self, data): self.data = data; self.pos = 0

    def take(self, count):
        need(type(count) is int and count >= 0 and self.pos + count <= len(self.data))
        result = self.data[self.pos:self.pos + count]; self.pos += count
        return result

    def byte(self): return self.take(1)[0]


def _backlen(length):
    size = 1 if length <= 127 else 2 if length < 16383 else 3 if length < 2097151 else 4 if length < 268435455 else 5
    return bytes(((length >> (7 * i)) & 127) | (0 if i == size - 1 else 128) for i in range(size - 1, -1, -1))


def _listpack(data):
    need(7 <= len(data) <= LIMITS["max_decompressed_block_bytes"])
    cur = _Cursor(data)
    need(int.from_bytes(cur.take(4), "little") == len(data))
    count = int.from_bytes(cur.take(2), "little"); need(count <= 127 or count == 65535)
    result = []
    while cur.pos < len(data) - 1:
        need(len(result) < 127); start = cur.pos; byte = cur.byte()
        if byte < 128: value = str(byte).encode("ascii")
        elif byte & 0xC0 == 0x80: value = cur.take(byte & 63)
        elif byte & 0xE0 == 0xC0:
            integer = ((byte & 31) << 8) | cur.byte()
            value = str(integer - 8192 if integer & 4096 else integer).encode("ascii")
        elif byte & 0xF0 == 0xE0: value = cur.take(((byte & 15) << 8) | cur.byte())
        elif byte == 0xF0: value = cur.take(int.from_bytes(cur.take(4), "little"))
        elif byte in (0xF1, 0xF2, 0xF3, 0xF4):
            length = {0xF1: 2, 0xF2: 3, 0xF3: 4, 0xF4: 8}[byte]
            value = str(int.from_bytes(cur.take(length), "little", signed=True)).encode("ascii")
        else: need(False)
        back = _backlen(cur.pos - start); need(cur.take(len(back)) == back); result.append(value)
    need(cur.byte() == 255 and cur.pos == len(data) and (count == len(result) or count == 65535))
    return result


def _intset(data):
    cur = _Cursor(data); width = int.from_bytes(cur.take(4), "little"); count = int.from_bytes(cur.take(4), "little")
    need(width in (2, 4, 8) and 1 <= count <= 127 and len(data) == 8 + width * count)
    values = [int.from_bytes(cur.take(width), "little", signed=True) for _ in range(count)]
    need(all(a < b for a, b in zip(values, values[1:])))
    return [str(value).encode("ascii") for value in values]


def _lzf(data, length, deadline):
    # Declared size is checked BEFORE allocation, and every write must fit it.
    need(0 < length <= LIMITS["max_decompressed_block_bytes"] and len(data) <= LIMITS["max_decompressed_block_bytes"])
    cur = _Cursor(data); result = bytearray()
    while cur.pos < len(data):
        need(time.monotonic() <= deadline); control = cur.byte()
        if control < 32:
            count = control + 1; need(len(result) + count <= length); result.extend(cur.take(count))
        else:
            count = control >> 5; high = (control & 31) << 8
            if count == 7: count += cur.byte()
            distance = high + cur.byte() + 1; count += 2
            need(distance <= len(result) and len(result) + count <= length)
            for _ in range(count): result.append(result[-distance])
    need(len(result) == length)
    return bytes(result)


def _score(value):
    need(type(value) is bytes and len(value) <= 64)
    need(re.fullmatch(rb"(?:[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?inf)", value, re.I) is not None)
    number = float(value); need(not math.isnan(number))
    return float_cell(number)


def _nodes(typ, value):
    nodes = []

    def add(parent, key, cell):
        need(len(nodes) < 128); key, omitted = node_key(key); index = len(nodes)
        nodes.append({"id": index, "parent": parent, "key": key, "key_omitted": omitted, "cell": cell})
        return index

    if typ == "string": add(None, None, binary_text_cell(value)); return nodes
    root = add(None, None, {"type": typ, "count": len(value)})
    if typ in {"list", "set"}:
        for index, item in enumerate(value): add(root, str(index), binary_text_cell(item))
    elif typ == "hash":
        for key, item in value:
            try: label = key.decode("utf8", "strict")
            except UnicodeError: label = "\0"
            add(root, label, binary_text_cell(item))
    else:
        for index, (member, score) in enumerate(value):
            entry = add(root, str(index), {"type": "entry", "count": 2})
            add(entry, "member", binary_text_cell(member)); add(entry, "score", score)
    return nodes


class _RDB(_Cursor):
    def __init__(self, data, deadline):
        super().__init__(data); self.deadline = deadline; self.decoded = 0

    def length(self, encoded=False):
        byte = self.byte(); high = byte >> 6
        if high == 0: return byte
        if high == 1: return ((byte & 63) << 8) | self.byte()
        if high == 2:
            need(byte in (0x80, 0x81)); return int.from_bytes(self.take(4 if byte == 0x80 else 8), "big")
        need(encoded and byte & 63 <= 3)
        return (byte & 63,)

    def string(self):
        need(time.monotonic() <= self.deadline); length = self.length(True)
        if type(length) is int:
            need(length <= LIMITS["max_decompressed_block_bytes"]); result = self.take(length)
        elif length[0] < 3:
            result = str(int.from_bytes(self.take((1, 2, 4)[length[0]]), "little", signed=True)).encode("ascii")
        else:
            compressed = self.length(); uncompressed = self.length()
            need(compressed <= LIMITS["max_decompressed_block_bytes"] and 0 < uncompressed <= LIMITS["max_decompressed_block_bytes"])
            need(self.decoded + uncompressed <= LIMITS["max_decompressed_bytes"])
            result = _lzf(self.take(compressed), uncompressed, self.deadline)
        self.decoded += len(result); need(self.decoded <= LIMITS["max_decompressed_bytes"])
        return result

    def count(self, maximum=127):
        count = self.length(); need(1 <= count <= maximum); return count

    def object(self, encoding):
        typ = _TYPES[encoding]
        if encoding == 0: return typ, self.string()
        if encoding in (1, 2): value = [self.string() for _ in range(self.count())]
        elif encoding in (3, 5):
            value = []
            for _ in range(self.count(42)):
                member = self.string()
                if encoding == 5:
                    score = struct.unpack("<d", self.take(8))[0]; need(not math.isnan(score)); score = float_cell(score)
                else:
                    length = self.byte()
                    need(length != 253)
                    score = {"type": "nonfinite", "value": "Infinity" if length == 254 else "-Infinity"} if length in (254, 255) else _score(self.take(length))
                value.append((member, score))
        elif encoding == 4: value = [(self.string(), self.string()) for _ in range(self.count())]
        elif encoding == 11: value = _intset(self.string())
        elif encoding in (16, 17, 20):
            packed = _listpack(self.string()); need(packed)
            if encoding == 20: value = packed
            else:
                need(len(packed) % 2 == 0)
                value = [(packed[i], _score(packed[i + 1]) if encoding == 17 else packed[i + 1]) for i in range(0, len(packed), 2)]
                if encoding == 17: need(len(value) <= 42)
        elif encoding == 18:
            value = []
            for _ in range(self.count()):
                container = self.length(); need(container in (1, 2)); packed = self.string()
                value.extend([packed] if container == 1 else _listpack(packed))
                need(1 <= len(value) <= 127)
        else: need(False)
        if typ == "set": need(len(set(value)) == len(value))
        if typ in {"hash", "zset"}: need(len({key for key, _ in value}) == len(value))
        return typ, value


def redis_rdb_preview(data, fmt="rdb", kind="tree", options=None):
    options = {} if options is None else options
    selected = validate_database_records_options(kind, options)
    need(fmt == "rdb" and type(data) is bytes and 18 <= len(data) <= MAX_INPUT and data[:9] == b"REDIS0011")
    try:
        deadline = time.monotonic() + 12
        stored_crc = int.from_bytes(data[-8:], "little"); need(stored_crc != 0 and crc64(data[:-8], deadline) == stored_crc)
        parser = _RDB(data[:-8], deadline); parser.pos = 9
        database = 0; groups = {}; records = []; seen = set(); expiry = None; idle = False; frequency = False; total = 0; scan_nodes = 0; page_nodes = 0; aux_count = 0

        def group(number):
            if number not in groups:
                need(len(groups) < 32)
                groups[number] = {"id": group_id("redis-rdb", number), "label": "Redis DB " + str(number), "database": number, "record_count": 0, "counts": {}}
            return groups[number]

        while parser.pos < len(parser.data):
            opcode = parser.byte(); need(time.monotonic() <= deadline)
            if opcode == 255:
                need(parser.pos == len(parser.data) and expiry is None and not idle and not frequency); break
            if opcode in (252, 253):
                need(expiry is None)
                expiry = int.from_bytes(parser.take(8 if opcode == 252 else 4), "little", signed=True)
                if opcode == 253: expiry *= 1000
                continue
            if opcode == 248: need(not idle); parser.length(); idle = True; continue
            if opcode == 249: need(not frequency); parser.byte(); frequency = True; continue
            if opcode in (250, 251, 254):
                need(expiry is None and not idle and not frequency)
                if opcode == 250:
                    aux_count += 1; need(aux_count <= 128); parser.string(); parser.string()
                elif opcode == 251:
                    dbsize, expires = parser.length(), parser.length(); need(dbsize <= 100000 and expires <= dbsize)
                else:
                    database = parser.length(); need(database <= 1023); group(database)
                continue
            need(opcode in _TYPES and total < 100000)
            key = parser.string(); identity = (database, hashlib.sha256(key).digest()); need(identity not in seen); seen.add(identity)
            typ, value = parser.object(opcode); nodes = _nodes(typ, value)
            scan_nodes += len(nodes); need(scan_nodes <= 100000)
            current = group(database); index = current["record_count"]; current["record_count"] += 1
            current["counts"][typ] = current["counts"].get(typ, 0) + 1
            if kind == "table" and selected["group_id"] == current["id"] and selected["offset"] <= index < selected["offset"] + selected["limit"]:
                page_nodes += len(nodes); need(page_nodes <= 4096); typed_key = binary_text_cell(key)
                records.append({"index": str(index), "key": typed_key, "expires_at_ms": None if expiry is None else str(expiry), "nodes": nodes,
                                "truncated": typed_key["type"] == "omitted" or any(n["key_omitted"] or n["cell"]["type"] == "omitted" for n in nodes)})
            total += 1; expiry = None; idle = False; frequency = False
        else: need(False)
        if not groups: group(0)
        catalog = [groups[db] for db in sorted(groups)]
        if kind == "table": need(any(g["id"] == selected["group_id"] for g in catalog))
        return build_database_records_payload("redis-rdb", len(data), kind, selected, catalog, version=11, records=records)
    except (ValueError, TypeError, IndexError, OverflowError, MemoryError, UnicodeError):
        raise DatabaseRecordsError("Redis RDB 文件不符合受限只读预览要求。") from None


redis_rdb_records_preview = redis_rdb_preview
