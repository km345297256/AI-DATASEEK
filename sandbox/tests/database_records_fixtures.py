"""Original tiny BSON/RDB test data, not third-party database backups.

BSON uses the official pinned encoder. RDB builders are wire fixtures; the
separate native Redis generator is the independent compatibility oracle.
"""
from __future__ import annotations

import struct


def bson_documents():
    from bson import BSON, Binary, Code, Decimal128, Int64, MaxKey, MinKey, ObjectId, Regex, Timestamp
    from bson.datetime_ms import DatetimeMS
    documents = [
        {"_id": ObjectId("0123456789abcdef01234567"), "integer": Int64(9223372036854775807),
         "negative": Int64(-9223372036854775808), "decimal": Decimal128("123456789012345678901234567890.1234"),
         "scaled": Decimal128("1.2300"), "extreme": Decimal128("1E+6144"), "real": 1.25,
         "date": DatetimeMS(-9223372036854775808), "stamp": Timestamp(4294967295, 4294967295),
         "empty": "", "null": None, "boolean": False, "binary": Binary(b"\x00\xff", 4),
         "nan": float("nan"), "infinity": float("inf"), "negative_infinity": float("-inf"),
         "decimal_nan": Decimal128("NaN"), "regex": Regex("^unsafe.*$", "im"),
         "javascript": Code("while(true){}"), "scope": Code("throw 42", {"binding": Int64(9007199254740993)}),
         "minimum": MinKey(), "maximum": MaxKey(), "html": "<img src=x onerror=alert(1)>",
         "long": "x" * 513, "host": "/Users/private/secret", "/Users/private/field": "hidden name"},
        {"name": "科学数据", "nested": {"array": [1, {"decimal": Decimal128("-0.0000")}, [], {}]}, "bytes": Binary(b"", 0)},
        {"name": "第三条", "value": Int64(9007199254740993)},
    ]
    return b"".join(BSON.encode(document) for document in documents)


def empty_bson_document():
    from bson import BSON
    return bytes(BSON.encode({}))


def rdb_len(value):
    if value < 64: return bytes([value])
    if value < 16384: return bytes([0x40 | value >> 8, value & 255])
    if value <= 2**32 - 1: return b"\x80" + value.to_bytes(4, "big")
    return b"\x81" + value.to_bytes(8, "big")


def rdb_string(value): return rdb_len(len(value)) + value


def _test_crc(data):
    # Bitwise test oracle, separate from the reader's table implementation.
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8): crc = (crc >> 1) ^ (0x95AC9329AC4BC9B5 if crc & 1 else 0)
    return crc


def rdb_file(body=b""):
    content = b"REDIS0011" + body + b"\xff"
    return content + _test_crc(content).to_bytes(8, "little")


def listpack(values):
    from app.services.redis_rdb_reader import _backlen
    content = bytearray()
    for value in values:
        value = value.encode("utf8") if type(value) is str else value
        if type(value) is int:
            if 0 <= value < 128: entry = bytes([value])
            elif -4096 <= value <= 4095:
                unsigned = value & 8191; entry = bytes([0xC0 | unsigned >> 8, unsigned & 255])
            else:
                width, prefix = next((w, p) for w, p in [(2, 0xF1), (3, 0xF2), (4, 0xF3), (8, 0xF4)] if -(1 << (w * 8 - 1)) <= value < (1 << (w * 8 - 1)))
                entry = bytes([prefix]) + value.to_bytes(width, "little", signed=True)
        elif len(value) < 64: entry = bytes([0x80 | len(value)]) + value
        elif len(value) < 4096: entry = bytes([0xE0 | len(value) >> 8, len(value) & 255]) + value
        else: entry = b"\xf0" + len(value).to_bytes(4, "little") + value
        content.extend(entry); content.extend(_backlen(len(entry)))
    return (len(content) + 7).to_bytes(4, "little") + len(values).to_bytes(2, "little") + bytes(content) + b"\xff"


def redis_wire_fixture():
    body = b"\xfe\x00"
    body += b"\xfc" + (9223372036854775807).to_bytes(8, "little", signed=True)
    body += b"\x00" + rdb_string(b"precise") + rdb_string(b"9007199254740993")
    body += b"\x12" + rdb_string(b"list") + b"\x01\x02" + rdb_string(listpack(["first", -9223372036854775808, "科学"]))
    body += b"\x14" + rdb_string(b"set") + rdb_string(listpack(["alpha", "beta"]))
    body += b"\x10" + rdb_string(b"hash") + rdb_string(listpack(["a", "", "b", "<img src=x onerror=alert(1)>"]))
    body += b"\x11" + rdb_string(b"zset") + rdb_string(listpack(["member", "1.25", "large", "1e308"]))
    body += b"\xfe\x02\x00" + rdb_string(b"binary") + rdb_string(b"\xff\x00")
    return rdb_file(body)
