import copy
import json
import sqlite3
from pathlib import Path
import pytest
from app.services import sqlite_table_reader as reader
from app.services.sqlite_table_payload import SqliteTableError, validate_sqlite_table_payload, validate_sqlite_table_options
from sqlite_table_fixtures import sqlite_bytes, sqlite_options, sqlite_payloads


def test_system_sqlite_writer_actual_storage_classes_rowids_and_pages(tmp_path):
    data=sqlite_bytes(); path=tmp_path/"oracle.sqlite";path.write_bytes(data)
    connection=sqlite3.connect(path.as_uri()+"?immutable=1&mode=ro",uri=True)
    expected=connection.execute("SELECT rowid,count,signal,label,payload,optional FROM measurements ORDER BY rowid").fetchall();connection.close()
    result=sqlite_payloads(); assert [t["label"] for t in result["tree"]["choices"]["tables"]]==["empty","measurements"]
    first=result["first"]; assert first["table"]["row_ids"]==[str(row[0]) for row in expected[:2]]
    assert [row[1]["value"] for row in first["table"]["rows"]]==[str(row[1]) for row in expected[:2]]
    assert first["table"]["rows"][0][4]=={"type":"blob","bytes":3}
    assert first["table"]["rows"][1][3]=={"type":"text","value":"测试"}
    assert result["second"]["table"]["rows"][0][3]=={"type":"text-omitted","bytes":513,"reason":"cell-budget"}
    assert result["second"]["table"]["rows"][1][2]=={"type":"nonfinite","value":None}
    assert result["second"]["table"]["rows"][1][3]["reason"]=="unsafe-text"
    assert result["last"]["table"]["has_more"] is False and len(result["last"]["table"]["rows"])==1
    assert result["empty"]["table"]["rows"]==[] and result["empty"]["table"]["has_more"] is False
    assert all("CREATE" not in json.dumps(v) and "/private/hidden" not in json.dumps(v) for v in result.values())


@pytest.mark.parametrize("fmt",["sqlite","sqlite3","db"])
def test_formats_signature_and_exact_selection(fmt):
    data=sqlite_bytes();options=sqlite_options();value=reader.sqlite_table_preview(data,fmt,"table",options)
    assert validate_sqlite_table_payload(value,kind="table",options=options,fmt=fmt,size=len(data)) is value
    for bad in ({"row_offset":1},{"row_limit":3},{"columns":[1]},{"table":"t-"+"0"*24}):
        with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value,options={**options,**bad})
    with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value,size=len(data)+1)


@pytest.mark.parametrize("offset,value",[(0,b"not sqlite bytes"),(16,b"\x00\x03"),(18,b"\x02"),(19,b"\x02"),(20,b"\x01"),(21,b"\x00"),(28,b"\xff\xff\xff\xff"),(44,b"\x00\x00\x00\x05"),(56,b"\x00\x00\x00\x02"),(72,b"\x01"),(92,b"\xff\xff\xff\xff")])
def test_bad_header_rejected_before_sqlite_open(monkeypatch,offset,value):
    import apsw
    data=bytearray(sqlite_bytes());data[offset:offset+len(value)]=value
    monkeypatch.setattr(apsw,"Connection",lambda *a,**kw:pytest.fail("native parser opened"))
    with pytest.raises(SqliteTableError):reader.sqlite_table_preview(bytes(data),"db")


@pytest.mark.parametrize("script",[
 "CREATE TABLE x(a);CREATE VIEW v AS SELECT a FROM x;",
 "CREATE TABLE x(a);CREATE TRIGGER tr AFTER INSERT ON x BEGIN SELECT 1;END;",
 "CREATE TABLE x(a,b GENERATED ALWAYS AS (a+1) VIRTUAL);",
 "CREATE TABLE x(a,b GENERATED ALWAYS AS (a+1) STORED);",
 "CREATE TABLE x(a PRIMARY KEY) WITHOUT ROWID;",
 "CREATE VIRTUAL TABLE x USING fts5(a);",
 "CREATE TABLE x(rowid,oid,_rowid_);",
 'CREATE TABLE "unsafe;name"(a);',
 'CREATE TABLE x("bad column");',
 "CREATE TABLE x("+",".join(f"x{i}" for i in range(129))+");",
 ";".join(f"CREATE TABLE t{i}(a)" for i in range(33))+";",
])
def test_unsupported_schema_is_explicitly_rejected(script):
    with pytest.raises(SqliteTableError):reader.sqlite_table_preview(sqlite_bytes(script),"sqlite")


def test_user_table_not_read_during_catalog(monkeypatch):
    import apsw
    calls=[];original=reader._Guard.authorize
    def trace(self,action,first,second,database,source):
        calls.append((action,first,second,database,source));return original(self,action,first,second,database,source)
    monkeypatch.setattr(reader._Guard,"authorize",trace)
    value=reader.sqlite_table_preview(sqlite_bytes(),"sqlite")
    assert not any(v[0]==apsw.SQLITE_READ and v[1] in {"empty","measurements"} for v in calls)
    assert value["metadata"]["rows_returned"]==0


def test_authorizer_fixed_functions_read_scope_no_side_effects():
    import apsw
    g=reader._Guard(apsw)
    for action,first,second,db,source in [
        (apsw.SQLITE_ATTACH,"evil",None,None,None),(apsw.SQLITE_PRAGMA,"writable_schema","1",None,None),
        (apsw.SQLITE_INSERT,"x",None,"main",None),(apsw.SQLITE_READ,"x","a","main",None),
        (apsw.SQLITE_READ,"sqlite_schema","sql","temp",None),(apsw.SQLITE_SELECT,None,None,None,"view"),
        (apsw.SQLITE_FUNCTION,None,"load_extension",None,None),(apsw.SQLITE_FUNCTION,None,"readfile",None,None),
    ]: assert g.authorize(action,first,second,db,source)==apsw.SQLITE_DENY


def test_native_settings_memory_and_limits(tmp_path):
    import apsw
    path=tmp_path/"synthetic.sqlite";path.write_bytes(sqlite_bytes());connection=apsw.Connection(path.as_uri()+"?immutable=1",flags=apsw.SQLITE_OPEN_READONLY|apsw.SQLITE_OPEN_URI)
    try:
        reader._configure(connection,apsw)
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH)==65536
        assert connection.limit(apsw.SQLITE_LIMIT_ATTACHED)==0
        assert connection.config(apsw.SQLITE_DBCONFIG_TRUSTED_SCHEMA,-1)==0
        assert connection.config(apsw.SQLITE_DBCONFIG_DEFENSIVE,-1)==1
        with pytest.raises(apsw.Error):connection.execute("SELECT zeroblob(65537)").fetchone()
        with pytest.raises(apsw.Error):connection.execute("ATTACH ':memory:' AS other")
        with pytest.raises(apsw.Error):connection.execute("INSERT INTO empty VALUES ('changed')")
    finally:connection.close()


@pytest.mark.parametrize("value",[b"x"*65537,"a"*65537])
def test_giant_cell_never_reaches_python_payload(value):
    data=sqlite_bytes(rows=[(1,2,3.0,"label",value,None)])
    with pytest.raises(SqliteTableError):reader.sqlite_table_preview(data,"sqlite","table",sqlite_options(0,1))


def test_invalid_utf8_text_rejected_not_replaced():
    data=sqlite_bytes("CREATE TABLE measurements (a);INSERT INTO measurements VALUES(CAST(x'80ff' AS TEXT));")
    with pytest.raises(SqliteTableError):reader.sqlite_table_preview(data,"sqlite","table",sqlite_options(0,1,[0]))


def test_source_unchanged_and_dedicated_temp_cleanup(monkeypatch,tmp_path):
    data=sqlite_bytes();before=bytes(data)
    original=reader.tempfile.TemporaryDirectory;paths=[]
    def capture(*args,**kwargs):
        obj=original(*args,dir=tmp_path,**kwargs);paths.append(Path(obj.name));return obj
    monkeypatch.setattr(reader.tempfile,"TemporaryDirectory",capture)
    reader.sqlite_table_preview(data,"sqlite")
    with pytest.raises(SqliteTableError):reader.sqlite_table_preview(data,"sqlite","table",sqlite_options(columns=[127]))
    assert data==before and len(paths)==2 and all(not path.exists() for path in paths)


@pytest.mark.parametrize("change",[{"table":"measurements"},{"columns":[True]},{"columns":[]},{"columns":[0,0]},{"columns":list(range(17))},{"row_offset":100001},{"row_offset":True},{"row_limit":201},{"row_limit":0},{"sql":"SELECT 1"},{"path":"/tmp/file"}])
def test_options_fail_before_native(monkeypatch,change):
    import apsw
    data=sqlite_bytes();monkeypatch.setattr(apsw,"Connection",lambda *a,**kw:pytest.fail("native parser opened"))
    with pytest.raises(SqliteTableError):reader.sqlite_table_preview(data,"sqlite","table",{**sqlite_options(),**change})


def test_progress_budget_aborts_and_hides_native_error(monkeypatch):
    data=sqlite_bytes(rows=[(i,i,float(i),"x",None,None) for i in range(2000)])
    monkeypatch.setattr(reader._Guard,"progress",lambda self:True)
    with pytest.raises(SqliteTableError,match="受限只读协议"):
        reader.sqlite_table_preview(data,"sqlite","table",sqlite_options(1999,1))


def test_rowid_shadow_uses_unshadowed_alias_and_expression_index_never_runs():
    data=sqlite_bytes("CREATE TABLE measurements(rowid TEXT,a);INSERT INTO measurements VALUES('not a key',7);CREATE INDEX unusual ON measurements(abs(a));")
    value=reader.sqlite_table_preview(data,"sqlite","table",sqlite_options(0,1,[0,1]))
    assert value["table"]["row_ids"]==["1"] and value["table"]["rows"][0][0]["value"]=="not a key"


def test_maximum_page_and_columns_remain_under_real_output_budget():
    script="CREATE TABLE measurements("+",".join(f"c{i} TEXT" for i in range(16))+");"
    script+="INSERT INTO measurements VALUES("+",".join("'"+"x"*512+"'" for _ in range(16))+");"
    script+="INSERT INTO measurements SELECT * FROM measurements;"*8
    value=reader.sqlite_table_preview(sqlite_bytes(script),"sqlite","table",sqlite_options(0,200,list(range(16))))
    assert len(value["table"]["rows"])==200 and value["table"]["has_more"] is True
    assert len(json.dumps(value,ensure_ascii=False).encode())<2097152


def test_table_rowid_signed_boundaries_and_nullable_affinity_are_storage_facts():
    script="CREATE TABLE measurements(a);INSERT INTO measurements(rowid,a) VALUES(-9223372036854775808,'001'),(9223372036854775807,9.25);"
    value=reader.sqlite_table_preview(sqlite_bytes(script),"sqlite","table",sqlite_options(0,2,[0]))
    assert value["table"]["row_ids"]==["-9223372036854775808","9223372036854775807"]
    assert value["table"]["rows"]==[[{"type":"text","value":"001"}],[{"type":"real","value":9.25}]]
    assert value["table"]["has_more"] is False


@pytest.mark.parametrize("text",["\x00hidden","\x01control","https://secret.invalid/a","file:private","C:\\secret\\file"])
def test_unsafe_text_never_crosses_payload(text):
    value=reader.sqlite_table_preview(sqlite_bytes(rows=[(1,2,3.0,text,b"",None)]),"sqlite","table",sqlite_options(0,1))
    assert value["table"]["rows"][0][3]=={"type":"text-omitted","bytes":len(text.encode()),"reason":"unsafe-text"}
    assert text not in json.dumps(value)


def test_header_allocation_limit_and_unsupported_format_precede_native(monkeypatch):
    import apsw
    data=sqlite_bytes();monkeypatch.setattr(apsw,"Connection",lambda *a,**kw:pytest.fail("native parser opened"))
    for payload,fmt in ((data+b"x","sqlite"),(data,"sqlite-journal"),(b"x"*16777217,"sqlite"),(bytearray(data),"sqlite")):
        with pytest.raises(SqliteTableError):reader.sqlite_table_preview(payload,fmt)


def test_internal_deadline_fails_closed(monkeypatch):
    original=reader._Guard.__init__
    def expired(self,apsw):original(self,apsw);self.deadline=0
    monkeypatch.setattr(reader._Guard,"__init__",expired)
    with pytest.raises(SqliteTableError):reader.sqlite_table_preview(sqlite_bytes(),"sqlite")


def test_hard_heap_limit_is_real_native_limit():
    import apsw
    reader.sqlite_table_preview(sqlite_bytes(),"sqlite")
    assert 0<apsw.hard_heap_limit(-1)<=33554432


def test_schema_default_and_index_expression_are_not_executed_or_returned():
    # DATE is deliberately outside the function authorizer; default expressions
    # and expression indexes are parsed as schema, never evaluated for preview.
    script="CREATE TABLE measurements(a DEFAULT (date('now')),b);INSERT INTO measurements(a,b) VALUES('kept',2);CREATE INDEX e ON measurements(abs(b));"
    value=reader.sqlite_table_preview(sqlite_bytes(script),"sqlite","table",sqlite_options(0,1,[0,1]))
    assert value["table"]["rows"][0][0]["value"]=="kept" and "date('now')" not in json.dumps(value)


def test_all_native_read_errors_have_fixed_diagnostics(monkeypatch):
    import apsw
    data=sqlite_bytes()
    def fail(*args,**kwargs):raise apsw.CorruptError("private secret /tmp/native")
    monkeypatch.setattr(reader,"_catalog",fail)
    with pytest.raises(SqliteTableError) as raised:reader.sqlite_table_preview(data,"sqlite")
    assert "private" not in str(raised.value) and "native" not in str(raised.value)


def test_shared_pure_contract_is_byte_identical():
    root=Path(__file__).resolve().parents[2]
    assert (root/"sandbox/app/services/sqlite_table_payload.py").read_bytes()==(root/"backend/app/application/services/sqlite_table_visualization.py").read_bytes()
