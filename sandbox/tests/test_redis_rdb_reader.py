import hashlib
import json
import struct
from pathlib import Path

import pytest

from app.services.database_records_payload import DatabaseRecordsError, group_id
from app.services.redis_rdb_reader import _intset, _listpack, _lzf, crc64, redis_rdb_preview
from database_records_fixtures import listpack, rdb_file, rdb_len, rdb_string, redis_wire_fixture


def page(data, offset=0, limit=2, database=0):
    return redis_rdb_preview(data, "rdb", "table", {"group_id": group_id("redis-rdb", database), "offset": offset, "limit": limit})


def test_standard_crc_vector(): assert crc64(b"123456789") == 0xe9c6d914c4b8d9ca


def test_native_redis_7_2_7_fixture_independent_encoder():
    data = (Path(__file__).parent / "fixtures/database-records/native-redis-7.2.7.rdb").read_bytes()
    assert hashlib.sha256(data).hexdigest() == "d7853cacc4bbee20f5d632226ac309ce3b913da47a669f2bfa373426431fed69"
    tree = redis_rdb_preview(data)
    assert tree["metadata"]["checksum"] == "verified" and tree["metadata"]["format_version"] == 11
    assert tree["choices"]["groups"][0]["counts"] == {"hash": 1, "list": 1, "string": 2, "set": 1, "zset": 1}
    records = page(data, limit=50)["table"]["records"]; keyed = {r["key"]["value"]: r for r in records}
    assert keyed["precise"]["expires_at_ms"] == "9223372036854775807"
    assert keyed["precise"]["nodes"][0]["cell"]["value"] == "9007199254740993"
    assert keyed["compressed"]["nodes"][0]["cell"] == {"type": "omitted", "bytes": 1024, "reason": "text-budget"}
    assert keyed["list"]["nodes"][2]["cell"]["value"] == "-9223372036854775808"
    assert keyed["hash"]["nodes"][1]["cell"]["value"] == ""
    assert keyed["zset"]["nodes"][3]["cell"]["value"] == 1.25
    assert page(data, database=2)["table"]["records"][0]["nodes"][0]["cell"]["value"] == "第二组"


def test_all_logical_types_precision_groups_and_expiry():
    data = redis_wire_fixture(); digest = hashlib.sha256(data).hexdigest(); tree = redis_rdb_preview(data)
    assert [(g["database"], g["record_count"]) for g in tree["choices"]["groups"]] == [(0, 5), (2, 1)]
    assert tree["choices"]["groups"][0]["counts"] == {"string": 1, "list": 1, "set": 1, "hash": 1, "zset": 1}
    assert "precise" not in json.dumps(tree)
    first = page(data); record = first["table"]["records"][0]
    assert record["nodes"][0]["cell"] == {"type": "text", "value": "9007199254740993"}
    assert record["expires_at_ms"] == "9223372036854775807"
    assert first["table"]["records"][1]["nodes"][2]["cell"]["value"] == "-9223372036854775808"
    rest = page(data, 2, 3)["table"]["records"]
    assert [r["nodes"][0]["cell"]["type"] for r in rest] == ["set", "hash", "zset"]
    assert rest[2]["nodes"][3]["cell"] == {"type": "real", "value": 1.25}
    assert page(data, database=2)["table"]["records"][0]["nodes"][0]["cell"] == {"type": "binary", "bytes": 2, "subtype": None}
    assert hashlib.sha256(data).hexdigest() == digest


def test_empty_and_past_end():
    assert redis_rdb_preview(rdb_file())["metadata"]["total_records"] == 0
    assert page(rdb_file())["table"]["records"] == []
    assert page(redis_wire_fixture(), 100000)["table"]["records"] == []


@pytest.mark.parametrize("values", [[0, 127, 128, -4096, 4095, -32768, 32767, -8388608, 8388607, -2147483648, 2147483647, -9223372036854775808, 9223372036854775807],
    [b"", b"a"*63, b"a"*64, b"b"*4095, b"c"*4096, b"d"*16382], ["科学", "<script>alert(1)</script>"]])
def test_listpack_encodings_roundtrip(values):
    expected = [str(v).encode() if type(v) is int else v.encode() if type(v) is str else v for v in values]
    assert _listpack(listpack(values)) == expected


@pytest.mark.parametrize("width,values", [(2, [-32768, 0, 32767]), (4, [-2147483648, 2147483647]), (8, [-9223372036854775808, 9223372036854775807])])
def test_intset_encodings(width, values):
    data = width.to_bytes(4, "little") + len(values).to_bytes(4, "little") + b"".join(v.to_bytes(width, "little", signed=True) for v in values)
    assert _intset(data) == [str(v).encode() for v in values]
    assert page(rdb_file(b"\x0b" + rdb_string(b"set") + rdb_string(data)))["table"]["records"][0]["nodes"][0]["cell"]["type"] == "set"


@pytest.mark.parametrize("encoded,expected", [(b"\xc0\x80", "-128"), (b"\xc1\x00\x80", "-32768"), (b"\xc2\x00\x00\x00\x80", "-2147483648")])
def test_encoded_integers_remain_redis_strings(encoded, expected):
    assert page(rdb_file(b"\x00" + rdb_string(b"key") + encoded))["table"]["records"][0]["nodes"][0]["cell"] == {"type": "text", "value": expected}


def test_raw_list_hash_set_zset_and_plain_quicklist():
    bodies = [b"\x01" + rdb_string(b"list") + b"\x02" + rdb_string(b"a") + rdb_string(b"b"),
              b"\x02" + rdb_string(b"set") + b"\x02" + rdb_string(b"a") + rdb_string(b"b"),
              b"\x04" + rdb_string(b"hash") + b"\x01" + rdb_string(b"a") + rdb_string(b"b"),
              b"\x03" + rdb_string(b"zset") + b"\x01" + rdb_string(b"a") + b"\x041.25",
              b"\x05" + rdb_string(b"zset2") + b"\x01" + rdb_string(b"a") + struct.pack("<d", 1.25),
              b"\x12" + rdb_string(b"quicklist2") + b"\x01\x01" + rdb_string(b"plain")]
    for body in bodies: assert page(rdb_file(body))["metadata"]["records_returned"] == 1


def test_lzf_bounded_literal_and_backreference():
    # 'a' literal followed by a length-9 overlapping backreference.
    compressed = b"\x00a\xe0\x00\x00"
    assert _lzf(compressed, 10, float("inf")) == b"a"*10
    body = b"\x00" + rdb_string(b"compressed") + b"\xc3" + rdb_len(len(compressed)) + b"\x0a" + compressed
    assert page(rdb_file(body))["table"]["records"][0]["nodes"][0]["cell"]["value"] == "a"*10


@pytest.mark.parametrize("compressed,length", [(b"\xe0\0\0", 9), (b"\0a\xe0\0\0", 9), (b"\0a", 2), (b"\0a", 1048577), (b"\x1fa", 32), (b"\xe0", 9)])
def test_lzf_bad_lengths_and_backrefs(compressed, length):
    with pytest.raises(DatabaseRecordsError): _lzf(compressed, length, float("inf"))


def test_lzf_declared_size_rejected_before_decompression(monkeypatch):
    monkeypatch.setattr("app.services.redis_rdb_reader._lzf", lambda *a: pytest.fail("oversize decompressed"))
    body = b"\0" + rdb_string(b"key") + b"\xc3\x01" + rdb_len(1048577) + b"x"
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(rdb_file(body))


def test_accumulated_decompression_budget(monkeypatch):
    from app.services.redis_rdb_reader import _RDB
    parser = _RDB(b"\xc3\x02\x01\x00x", float("inf"))
    parser.decoded = 33554432
    monkeypatch.setattr("app.services.redis_rdb_reader._lzf", lambda *a: pytest.fail("budget exhausted before allocation"))
    with pytest.raises(DatabaseRecordsError): parser.string()


def test_record_page_group_and_scan_budgets(monkeypatch):
    many_groups = b"".join(b"\xfe" + rdb_len(i) for i in range(33))
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(rdb_file(many_groups))
    many_items = b"\x01" + rdb_string(b"list") + rdb_len(128)
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(rdb_file(many_items))
    many_zset = b"\x05" + rdb_string(b"zset") + rdb_len(43)
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(rdb_file(many_zset))
    times = iter([0, 13]); monkeypatch.setattr("app.services.redis_rdb_reader.time.monotonic", lambda: next(times))
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(rdb_file())


@pytest.mark.parametrize("body", [b"\x15", b"\x0f", b"\x07", b"\x09", b"\xf5", b"\xf6", b"\xf7", b"\x0a", b"\x0e", b"\x81",
    b"\xfe" + rdb_len(1024), b"\xfc" + bytes(8), b"\xf8\x01", b"\xf9\x01", b"\xfb\x01\x02",
    b"\0" + rdb_string(b"key") + b"\xc4", b"\0" + rdb_string(b"key") + rdb_len(1048577),
    b"\x10" + rdb_string(b"hash") + rdb_string(listpack(["one"])),
    b"\x14" + rdb_string(b"set") + rdb_string(listpack(["same", "same"])),
    b"\x10" + rdb_string(b"hash") + rdb_string(listpack(["same", "one", "same", "two"])),
    b"\x05" + rdb_string(b"zset") + b"\x01" + rdb_string(b"x") + struct.pack("<d", float("nan")),
    b"\x12" + rdb_string(b"list") + b"\x01\x03" + rdb_string(b"bad")])
def test_unsupported_and_malformed_fail_closed(body):
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(rdb_file(body))


@pytest.mark.parametrize("data", [b"", rdb_file()[:-1], rdb_file() + b"x", b"REDIS0010" + rdb_file()[9:], rdb_file()[:-8] + bytes(8), rdb_file()[:-8] + bytes([1])*8])
def test_header_checksum_and_trailer(data):
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(data)


def test_duplicate_keys_and_unselected_records_validation():
    key = b"\0" + rdb_string(b"key") + rdb_string(b"value")
    with pytest.raises(DatabaseRecordsError): redis_rdb_preview(rdb_file(key * 2))
    with pytest.raises(DatabaseRecordsError): page(rdb_file(key + b"\x15"), limit=1)


def test_negative_expiry_retained_and_paths_omitted():
    body = b"\xfc" + (-1).to_bytes(8, "little", signed=True) + b"\0" + rdb_string(b"/Users/private/key") + rdb_string(b"/Users/private/value")
    record = page(rdb_file(body))["table"]["records"][0]
    assert record["expires_at_ms"] == "-1" and record["truncated"] is True
    assert "/Users/" not in json.dumps(record)


def test_corrupt_listpack_and_intset_lengths():
    good = listpack([b"abc"])
    for bad in [good[:-1], bytes(4) + good[4:], good[:4] + b"\x02\0" + good[6:], good[:-2] + b"\x03\xff", b"\x08\0\0\0\0\0\xf5\xff"]:
        with pytest.raises(DatabaseRecordsError): _listpack(bad)
    with pytest.raises(DatabaseRecordsError): _intset(b"\x02\0\0\0\xff\xff\xff\xff")


def test_no_io_clients_or_clock_filter(monkeypatch):
    import builtins
    import socket
    import subprocess
    data = redis_wire_fixture()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("file IO forbidden"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("server forbidden"))
    assert page(data)["metadata"]["records_returned"] == 2
