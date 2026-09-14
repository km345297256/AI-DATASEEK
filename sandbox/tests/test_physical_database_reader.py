import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.services import physical_database_reader as reader
from app.services.physical_database_payload import build_payload, validate_physical_payload, options_for, MAX_INPUT

FIXTURES = Path(__file__).parent / "fixtures/physical-database"


def fixture(name):
    data = (FIXTURES / name).read_bytes()
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert hashlib.sha256(data).hexdigest() == manifest[name]
    return data


def payload(engine="mysql-sdi"):
    if engine == "mysql-sdi":
        return build_payload(engine, "ibd", len(fixture("mysql-8.0.46.ibd")), reader.sdi_rows(fixture("mysql-sdi-oracle.json")))
    return build_payload(engine, "sst", len(fixture("rocksdb-6.11.4.sst")), reader.sst_rows(fixture("rocksdb-6.11.4.sst.oracle.txt")))


def test_native_oracles_are_mandatory_and_precise():
    p = payload()
    assert p["table"]["rows"][1] == ["column", "measurements", "id", "type=bigint;nullable=false;ordinal=1;hidden=1"]
    assert p["table"]["rows"][3][3].startswith("type=decimal(20,4)")
    assert len(p["table"]["rows"]) == 8
    p = payload("sst-records")
    rows = p["table"]["rows"]
    assert bytes.fromhex(rows[0][4]) == b"9007199254740993"
    assert rows[1][3:] == ["deletion", "", "0", "none"]
    assert bytes.fromhex(rows[2][4]) == b"a\0b"


@pytest.mark.parametrize("engine", ["mysql-sdi", "sst-records"])
@pytest.mark.parametrize("mutate", [
    lambda p: p.update(sql="forbidden"), lambda p: p.update(kind="table"),
    lambda p: p.update(contract_version=True), lambda p: p.update(sampled=True),
    lambda p: p["selected"].update(offset=0), lambda p: p["choices"].update(path="private"),
    lambda p: p["metadata"].update(logical_state_verified=True), lambda p: p["metadata"].update(source_bytes=True),
    lambda p: p["metadata"].update(source_bytes=MAX_INPUT+1), lambda p: p["metadata"].update(rows_returned=True),
    lambda p: p["metadata"].update(rows_returned=100), lambda p: p["metadata"].update(tool_version="unknown"),
    lambda p: p["metadata"].update(format="bak"), lambda p: p["metadata"].update(path="/Users/private"),
    lambda p: p["metadata"]["limits"].update(max_rows=999999), lambda p: p["table"].update(sql="secret"),
    lambda p: p["table"]["rows"][0].append("extra"), lambda p: p["table"]["rows"][0].__setitem__(0,"/Users/private"),
    lambda p: p["table"]["columns"].reverse(), lambda p: p["table"]["rows"].__setitem__(0,{}),
])
def test_payload_rejects_forged_metadata_and_rows(engine, mutate):
    p = payload(engine); mutate(p)
    with pytest.raises(ValueError): validate_physical_payload(p)


@pytest.mark.parametrize("engine", ["mysql-sdi", "sst-records"])
@pytest.mark.parametrize("kwargs", [{"reader":"other"},{"kind":"table"},{"fmt":"dmp"},{"size":1},{"size":True},{"options":{"restore":True}},{"limit":1}])
def test_binding(engine, kwargs):
    with pytest.raises(ValueError): validate_physical_payload(payload(engine), **kwargs)


@pytest.mark.parametrize("kind,options", [("table",{}),("tree",{"sql":"SELECT 1"}),("tree",{"path":"/private"}),("tree",[]),("tree",{"connection":"dsn"}),("series",{})])
def test_options_rejected_before_native(monkeypatch, kind, options):
    monkeypatch.setattr(reader, "run_native", lambda *a: pytest.fail("unexpected native invocation"))
    for engine, name, fmt in [("mysql-sdi","mysql-8.0.46.ibd","ibd"),("sst-records","rocksdb-6.11.4.sst","sst")]:
        with pytest.raises(ValueError): reader.physical_database_preview(fixture(name), engine, fmt, kind, options)


def test_sdi_never_forwards_ddl_paths_owners_or_enum_literals():
    value = json.loads(fixture("mysql-sdi-oracle.json"))
    t = value[1]["object"]["dd_object"]
    t.update(schema_ref="SECRET_DATABASE", filename="/Users/private", owner="PRIVATE_OWNER")
    t["columns"][0]["name"] = "/Users/private"
    t["columns"][0]["column_type_utf8"] = "enum('PRIVATE_TOKEN')"
    output = json.dumps(reader.sdi_rows(json.dumps(value).encode()))
    for private in ["SECRET_DATABASE","PRIVATE_OWNER","PRIVATE_TOKEN","/Users/private"]: assert private not in output
    assert "mysql_type_9" in output


@pytest.mark.parametrize("mutate", [
    lambda d: d[1]["object"].update(mysqld_version_id=80400),
    lambda d: d[1]["object"].update(dd_version=True),
    lambda d: d[1]["object"].update(sdi_version=1),
    lambda d: d[1]["object"].update(dd_object_type="Database"),
    lambda d: d[1]["object"]["dd_object"].update(engine="MyISAM"),
    lambda d: d[1]["object"]["dd_object"]["columns"][0].update(ordinal_position=3),
    lambda d: d[1]["object"]["dd_object"]["indexes"][0]["elements"][0].update(column_opx=128),
])
def test_unsupported_sdi_rejected(mutate):
    value=json.loads(fixture("mysql-sdi-oracle.json"));mutate(value)
    with pytest.raises(ValueError): reader.sdi_rows(json.dumps(value).encode())


@pytest.mark.parametrize("raw", [b"["*33+b"]"*33,b'["ibd2sdi",{"a":1,"a":2}]',b"[]",b"[NaN]",b"\xff"])
def test_json_budgets_and_ambiguity(raw):
    with pytest.raises((ValueError,UnicodeError)): reader.sdi_rows(raw)


@pytest.mark.parametrize("old,new", [
    (b"# entries: 3",b"# entries: 4"),(b"# range deletions: 0",b"# range deletions: 1"),
    (b"# deletions: 1",b"# deletions: 0"),(b"type:1",b"type:15"),
    (b"seq:0",b"seq:72057594037927936"),(b"=> 610062",b"=> 61006"),
    (b"leveldb.BytewiseComparator",b"custom.Comparator"),
    (b"global_seqno: 0x0000000000000000",b"global_seqno: 0x0100000000000000"),
    (b"Table Properties:",b"Table Properties:\n  # entries: 3"),
])
def test_sst_unsafe_semantics_and_output_ambiguity(old,new):
    with pytest.raises(ValueError): reader.sst_rows(fixture("rocksdb-6.11.4.sst.oracle.txt").replace(old,new))


def test_real_range_deletion_is_rejected_not_silently_omitted():
    with pytest.raises(ValueError): reader.sst_rows(fixture("rocksdb-range.sst.oracle.txt"))


def test_sst_long_binary_is_explicitly_omitted():
    row = ["", "129", "72057594037927935", "value", "", "513", "both"]
    p=build_payload("sst-records","sst",2048,[row]);assert p["sampled"] is True
    row[0]="aa"
    with pytest.raises(ValueError): build_payload("sst-records","sst",2048,[row])


def test_sdi_header_rejects_encryption_and_unknown_page_size(monkeypatch):
    monkeypatch.setattr(reader,"run_native",lambda *a:pytest.fail("unexpected native invocation"))
    original=fixture("mysql-8.0.46.ibd")
    for flags in (0,0x4021|0x2000,0x4021|0x40,0x4021|2):
        data=original[:54]+flags.to_bytes(4,"big")+original[58:]
        with pytest.raises(ValueError): reader.physical_database_preview(data,"mysql-sdi","ibd")


def test_pure_payload_backend_copy_is_identical():
    root=Path(__file__).resolve().parents[2]
    backend=root/"backend/app/application/services/physical_database_visualization.py"
    assert backend.read_bytes()==(root/"sandbox/app/services/physical_database_payload.py").read_bytes()
