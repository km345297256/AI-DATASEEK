import hashlib
import importlib.util
import json
from pathlib import Path
import struct

import pytest

from app.services.bson_reader import bson_preview
from app.services.database_records_payload import DatabaseRecordsError, group_id
from database_records_fixtures import bson_documents, empty_bson_document


def page(data, offset=0, limit=2):
    return bson_preview(data, "bson", "table", {"group_id": group_id("bson", None), "offset": offset, "limit": limit})


def doc(elements): return struct.pack("<i", len(elements) + 5) + elements + b"\0"


def test_native_pymongo_fixture_all_scalars_and_nested():
    import pymongo
    assert pymongo.version == "4.17.0"
    data = bson_documents(); digest = hashlib.sha256(data).hexdigest()
    tree = bson_preview(data)
    assert tree["choices"]["groups"][0]["record_count"] == 3
    assert "9007199254740993" not in json.dumps(tree)
    first = page(data); record = first["table"]["records"][0]
    fields = {node["key"]: node["cell"] for node in record["nodes"]}
    assert fields["integer"] == {"type": "integer", "value": "9223372036854775807"}
    assert fields["negative"]["value"] == "-9223372036854775808"
    assert fields["decimal"]["value"] == "123456789012345678901234567890.1234"
    assert fields["scaled"]["value"] == "1.2300"
    assert fields["extreme"]["value"] == "1.000000000000000000000000000000000E+6144"
    assert fields["date"] == {"type": "date-ms", "value": "-9223372036854775808"}
    assert fields["stamp"] == {"type": "timestamp", "seconds": "4294967295", "increment": "4294967295"}
    assert fields["null"]["type"] == "null" and fields["empty"]["value"] == ""
    assert fields["boolean"]["value"] is False
    assert fields["binary"] == {"type": "binary", "bytes": 2, "subtype": "04"}
    assert fields["scope"] == {"type": "unsupported", "name": "javascript-scope"}
    assert fields["long"]["reason"] == "text-budget" and fields["host"]["reason"] == "unsafe-text"
    assert record["truncated"] is True and "/Users/" not in json.dumps(first)
    assert "while(true)" not in json.dumps(first) and "onerror" in json.dumps(first)
    assert first["table"]["has_more"] is True
    last = page(data, 2); assert last["table"]["records"][0]["index"] == "2"
    assert last["table"]["has_more"] is False
    assert hashlib.sha256(data).hexdigest() == digest


def test_empty_document_and_page_past_end():
    result = page(empty_bson_document())
    assert result["table"]["records"][0]["nodes"][0]["cell"] == {"type": "document", "count": 0}
    assert page(empty_bson_document(), 1)["table"]["records"] == []


@pytest.mark.parametrize("data", [b"", b"\0"*5, b"\xff"*5, b"\x06\0\0\0\0", doc(b"\x08b\0\x02"),
    doc(b"\x10x\0\x01\0\0\0\x10x\0\x02\0\0\0"), doc(b"\x09d\0\x00"),
    doc(b"\x02s\0\xff\xff\xff\xff"), doc(b"\x02s\0\x02\0\0\0\xff\0"),
    doc(b"\x05b\0\x01\0\0\0\x02\0"), doc(b"\x04a\0" + doc(b"\x10bad\0\x01\0\0\0")),
    doc(b"\x03a\0\xff\xff\xff\x7f"), doc(b"\x30a\0"),
    doc(b"\x0fr\0\x0e\0\0\0\x02\0\0\0x\0\x05\0\0\0\0"),
    doc(b"\x0bp\0pattern\0mi\0"), doc(b"\x0bp\0pattern\0ii\0")])
def test_malformed_fails_closed(data):
    with pytest.raises(DatabaseRecordsError): bson_preview(data)


@pytest.mark.parametrize("kind,options", [("sql", {}), ("tree", {"offset": 0}), ("table", {}),
    ("table", {"group_id": "g-" + "f"*24, "offset": 0, "limit": 2}),
    ("table", {"group_id": group_id("bson", None), "offset": True, "limit": 2}),
    ("table", {"group_id": group_id("bson", None), "offset": 0, "limit": 51})])
def test_invalid_request(kind, options):
    with pytest.raises(DatabaseRecordsError): bson_preview(empty_bson_document(), "bson", kind, options)


def test_every_record_is_validated_not_only_page():
    with pytest.raises(DatabaseRecordsError): page(empty_bson_document() + doc(b"\x30a\0"), limit=1)


def test_invalid_decimal128_bid_is_sanitized_not_driver_exception():
    invalid = doc(b"\x13amount\0" + bytes.fromhex("9af24c187304af93c0b81376f7facdd3"))
    for preview in (bson_preview, page):
        with pytest.raises(DatabaseRecordsError, match="BSON 文件不符合受限只读预览要求"):
            preview(invalid)


def test_structure_budgets_and_no_full_bson_decode(monkeypatch):
    from bson import BSON
    monkeypatch.setattr(BSON, "decode", lambda *a, **kw: pytest.fail("full decoder prohibited"))
    assert bson_preview(empty_bson_document())["metadata"]["total_records"] == 1
    nested = doc(b"")
    for _ in range(9): nested = doc(b"\x03a\0" + nested)
    with pytest.raises(DatabaseRecordsError): bson_preview(nested)
    too_many = doc(b"".join(b"\x0a" + str(i).encode() + b"\0" for i in range(128)))
    with pytest.raises(DatabaseRecordsError): bson_preview(too_many)
    with pytest.raises(DatabaseRecordsError): bson_preview(empty_bson_document() * 100001)


def test_timeout(monkeypatch):
    times = iter([0, 13]); monkeypatch.setattr("app.services.bson_reader.time.monotonic", lambda: next(times))
    with pytest.raises(DatabaseRecordsError): bson_preview(empty_bson_document())


def test_no_database_client_file_or_network_calls(monkeypatch):
    import builtins
    import socket
    import pymongo
    fixture = bson_documents()
    monkeypatch.setattr(pymongo, "MongoClient", lambda *a, **k: pytest.fail("Mongo client forbidden"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("file IO forbidden"))
    assert page(fixture)["metadata"]["records_returned"] == 2
