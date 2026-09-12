import struct
import pytest

from app.services.dbf_table_reader import dbf_table_preview
from app.services.database_table_payload import DatabaseTableError, table_id


def dbf_bytes(rows=None, *, driver=0, fields=None):
    fields = fields or [("Label", "C", 24, 0), ("Amount", "N", 26, 4), ("Enabled", "L", 1, 0), ("Observed", "D", 8, 0)]
    rows = rows if rows is not None else [
        (b" ", [b"alpha", b"9007199254740993.0001", b"T", b"20240229"]),
        (b"*", [b"deleted", b"77", b"F", b"20000101"]),
        (b" ", [b"", b"", b"?", b"        "]),
        (b" ", [b"last", b"-0.0001", b"F", b"20260911"]),
    ]
    header = bytearray(32); header[0] = 3; header[1:4] = bytes([126, 9, 11]); header[29] = driver
    width = 1 + sum(f[2] for f in fields)
    struct.pack_into("<IHH", header, 4, len(rows), 33 + 32 * len(fields), width)
    descriptors = bytearray()
    for label, typ, size, decimal in fields:
        field = bytearray(32); field[:len(label)] = label.encode("ascii"); field[11] = ord(typ); field[16] = size; field[17] = decimal
        descriptors.extend(field)
    body = bytearray()
    for flag, values in rows:
        body.extend(flag)
        for value, field in zip(values, fields): body.extend(value.rjust(field[2], b" ") if field[1] == "N" else value.ljust(field[2], b" "))
    return bytes(header + descriptors + b"\r" + body + b"\x1a")


def page(data, offset=0, limit=2, columns=None):
    return dbf_table_preview(data, "dbf", "table", {"table": table_id("dbf", "DBF table"),
        "columns": [0, 1, 2, 3] if columns is None else columns, "row_offset": offset, "row_limit": limit})


def test_dbf_native_dbfread_exact_decimal_null_blank_and_deleted_record():
    data = dbf_bytes(); tree = dbf_table_preview(data, "dbf")
    assert tree["choices"]["tables"][0]["columns"][1]["data_type"] == "DECIMAL"
    first = page(data)
    assert first["table"]["row_ids"] == ["0", "1"] and first["table"]["has_more"] is True
    assert first["table"]["rows"][0] == [{"type": "text", "value": "alpha"}, {"type": "decimal", "value": "9007199254740993.0001"}, {"type": "boolean", "value": True}, {"type": "date", "value": "2024-02-29"}]
    assert first["table"]["rows"][1] == [{"type": "text", "value": ""}, *[{"type": "null", "value": None}] * 3]
    last = page(data, 2)
    assert last["table"]["row_ids"] == ["2"] and last["table"]["has_more"] is False
    assert last["table"]["rows"][0][1] == {"type": "decimal", "value": "-0.0001"}
    assert page(data, 100)["table"]["rows"] == []


def test_dbf_codepage_strict_chinese_and_empty_table():
    chinese = dbf_bytes([(b" ", ["科学数据".encode("gbk"), b"1.0000", b"T", b"20240229"])], driver=0x7a)
    assert page(chinese)["table"]["rows"][0][0]["value"] == "科学数据"
    assert page(dbf_bytes([]))["table"]["rows"] == []
    malformed = dbf_bytes([(b" ", [b"\xff", b"1", b"T", b"20240229"])])
    with pytest.raises(DatabaseTableError): page(malformed)


@pytest.mark.parametrize("offset,value", [(0,b"\x83"),(0,b"\x30"),(4,b"\xff\xff\xff\xff"),(8,b"\x20\x00"),(10,b"\x01\x00"),(15,b"\x01"),(28,b"\x01"),(29,b"\xff"),(43,b"M"),(48,b"\x00"),(50,b"\x01"),(160,b"\x00"),(161,b"!")])
def test_dbf_reject_unsupported_memo_vfp_null_flags_bad_headers(offset, value):
    data = bytearray(dbf_bytes()); data[offset:offset + len(value)] = value
    with pytest.raises(DatabaseTableError): dbf_table_preview(bytes(data), "dbf")


@pytest.mark.parametrize("value", [b"9,123", b"*******", b"NaN", b"1e9", b"+1.0000"])
def test_dbf_no_ambiguous_numeric_coercion(value):
    data = dbf_bytes([(b" ", [b"label", value, b"T", b"20240229"])])
    with pytest.raises(DatabaseTableError): page(data)


def test_dbf_projection_does_not_parse_unselected_invalid_scalar():
    data = dbf_bytes([(b" ", [b"label", b"not a decimal", b"T", b"20240229"])])
    assert page(data, columns=[0])["table"]["rows"] == [[{"type": "text", "value": "label"}]]


def test_dbf_nul_text_never_disappears_into_padding():
    data = dbf_bytes([(b" ", [b"safe\0", b"000001.2000", b"T", b"20240229"])])
    rows = page(data)["table"]["rows"]
    assert rows[0][0] == {"type": "text-omitted", "bytes": 5, "reason": "unsafe-text"}
    assert rows[0][1] == {"type": "decimal", "value": "1.2000"}


@pytest.mark.parametrize("typ,size,value", [("F",12,b"*1*"),("D",8,b"\0"*8),("L",1,b"\0")])
def test_dbf_no_implicit_cleanup_of_invalid_scalar(typ, size, value):
    with pytest.raises(DatabaseTableError): page(dbf_bytes([(b" ", [value])], fields=[("Value",typ,size,0)]), columns=[0])


@pytest.mark.parametrize("change", [{"table": "DBF table"}, {"columns": [True]}, {"columns": [0, 0]}, {"columns": [127]}, {"row_limit": 201}, {"row_offset": 100001}, {"sql": "DELETE"}])
def test_dbf_options_are_opaque_bounded_no_sql(change):
    options = {"table": table_id("dbf", "DBF table"), "columns": [0], "row_offset": 0, "row_limit": 2, **change}
    with pytest.raises(DatabaseTableError): dbf_table_preview(dbf_bytes(), "dbf", "table", options)


def test_dbf_refuses_extra_truncated_duplicate_and_unsafe_metadata():
    for data in (dbf_bytes() + b"x", dbf_bytes()[:-3], dbf_bytes(fields=[("Dup", "C", 24, 0), ("Dup", "C", 24, 0)]), dbf_bytes(fields=[("../bad", "C", 24, 0)])):
        with pytest.raises(DatabaseTableError): dbf_table_preview(data, "dbf")


def test_dbf_private_snapshot_cleanup_and_source_unchanged(monkeypatch, tmp_path):
    from app.services import dbf_table_reader as reader
    original = reader.tempfile.TemporaryDirectory; paths = []
    def temporary(*args, **kwargs):
        obj = original(*args, dir=tmp_path, **kwargs); paths.append(obj.name); return obj
    monkeypatch.setattr(reader.tempfile, "TemporaryDirectory", temporary)
    data = dbf_bytes(); before = bytes(data); page(data)
    assert data == before and not list(tmp_path.iterdir()) and paths
