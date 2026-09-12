"""Host-side SQLite contract requires no SQLite/ASPW import or filesystem I/O."""
import copy
import json
from pathlib import Path
import pytest
from app.application.services.sqlite_table_visualization import (
    SqliteTableError, validate_sqlite_table_payload, validate_sqlite_table_options,
)
from app.application.services.extended_visualization import validate_payload

FIXTURES=json.loads(Path(__file__).with_name("sqlite_table_contract_fixtures.json").read_text())


@pytest.mark.parametrize("key",list(FIXTURES))
def test_actual_sqlite_payload_and_existing_generic_budget(key):
    value=copy.deepcopy(FIXTURES[key]);kind=value["kind"]
    assert validate_sqlite_table_payload(value,kind=kind,options=value["selected"],fmt="sqlite",size=value["metadata"]["source_bytes"]) is value
    assert validate_payload(value,"sqlite-table",kind,2097152) is value


MUTATIONS=[
 ("version bool",lambda v:v.update(contract_version=True)),
 ("wrong reader",lambda v:v.update(reader="columnar-window")),
 ("extra SQL",lambda v:v.update(sql="SELECT 1")),
 ("extra metadata path",lambda v:v["metadata"].update(path="private")),
 ("source bool",lambda v:v["metadata"].update(source_bytes=True)),
 ("input budget",lambda v:v["metadata"].update(source_bytes=16777217)),
 ("format",lambda v:v["metadata"].update(format="parquet")),
 ("mode",lambda v:v["metadata"].update(input_mode="window")),
 ("container",lambda v:v["metadata"].update(container="SQLCipher")),
 ("scope",lambda v:v["metadata"].update(value_semantics="cast numbers")),
 ("total known",lambda v:v["metadata"].update(total_rows_known=True)),
 ("progress",lambda v:v["metadata"].update(progress_callbacks=2001)),
 ("schema budget",lambda v:v["metadata"].update(schema_bytes=65537)),
 ("schema source",lambda v:v["metadata"].update(schema_bytes=v["metadata"]["source_bytes"]+1)),
 ("native limit",lambda v:v["metadata"]["limits"].update(max_record_bytes=65537)),
 ("bool limit",lambda v:v["metadata"]["limits"].update(max_rows=True)),
 ("missing warning",lambda v:v.update(warnings=[])),
 ("sampled bool",lambda v:v.update(sampled=1)),
 ("table id",lambda v:v["choices"]["tables"][0].update(id="t-"+"0"*24)),
 ("unsafe table",lambda v:v["choices"]["tables"][0].update(label="table name")),
 ("table order",lambda v:v["choices"]["tables"].reverse()),
 ("column id bool",lambda v:v["choices"]["tables"][1]["columns"][0].update(id=True)),
 ("column name",lambda v:v["choices"]["tables"][1]["columns"][0].update(label="/home/leak")),
 ("column affinity",lambda v:v["choices"]["tables"][1]["columns"][0].update(affinity="NATIVE")),
 ("pk",lambda v:v["choices"]["tables"][1]["columns"][0].update(primary_key=2)),
 ("null flag",lambda v:v["choices"]["tables"][1]["columns"][0].update(nullable=1)),
 ("rows mismatch",lambda v:v["metadata"].update(rows_returned=1)),
 ("selected columns",lambda v:v["selected"].update(columns=[0])),
 ("selected offset",lambda v:v["selected"].update(row_offset=1)),
 ("column_ids bool",lambda v:v["table"].update(column_ids=[False,1,2,3,4,5])),
 ("rowid numeric",lambda v:v["table"]["row_ids"].__setitem__(0,-5)),
 ("rowid overflow",lambda v:v["table"]["row_ids"].__setitem__(0,"-9223372036854775809")),
 ("rowid order",lambda v:v["table"]["row_ids"].reverse()),
 ("integer numeric",lambda v:v["table"]["rows"][0][1].update(value=1)),
 ("integer overflow",lambda v:v["table"]["rows"][0][1].update(value="9223372036854775808")),
 ("integer negative zero",lambda v:v["table"]["rows"][0][1].update(value="-0")),
 ("integer plus",lambda v:v["table"]["rows"][0][1].update(value="+1")),
 ("real nonfinite",lambda v:v["table"]["rows"][0][2].update(value=float("inf"))),
 ("real bool",lambda v:v["table"]["rows"][0][2].update(value=True)),
 ("text budget",lambda v:v["table"]["rows"][0][3].update(value="a"*513)),
 ("text invalid utf8",lambda v:v["table"]["rows"][0][3].update(value="\ud800")),
 ("text path",lambda v:v["table"]["rows"][0][3].update(value="/Users/leak")),
 ("blob bytes",lambda v:v["table"]["rows"][0][4].update(bytes=65537)),
 ("blob leaked data",lambda v:v["table"]["rows"][0][4].update(value="AA==")),
 ("null meaning",lambda v:v["table"]["rows"][0][5].update(value="NULL")),
 ("wrong blob statistics",lambda v:v["metadata"].update(blob_values=0)),
]
@pytest.mark.parametrize("label,mutate",MUTATIONS,ids=[v[0] for v in MUTATIONS])
def test_malformed_typed_payload_is_rejected(label,mutate):
    value=copy.deepcopy(FIXTURES["first"]);mutate(value)
    with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value)


@pytest.mark.parametrize("field",["table","columns","row_offset","row_limit"])
def test_request_selection_is_exactly_bound(field):
    value=copy.deepcopy(FIXTURES["first"]);options=copy.deepcopy(value["selected"])
    options[field]={"table":FIXTURES["empty"]["selected"]["table"],"columns":[1],"row_offset":2,"row_limit":1}[field]
    with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value,options=options)


@pytest.mark.parametrize("kind,options",[(None,{}),([],{}),(True,{}),("tree",{"sql":"SELECT 1"}),("table",{}),("table",None),("graph",{})])
def test_options_fail_closed(kind,options):
    with pytest.raises(SqliteTableError):validate_sqlite_table_options(kind,options)


@pytest.mark.parametrize("reason,size",[("unsafe-text",513),("cell-budget",512),("arbitrary",4)])
def test_omission_reason_must_match_byte_budget(reason,size):
    value=copy.deepcopy(FIXTURES["second"]);value["table"]["rows"][0][3].update(reason=reason,bytes=size)
    with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value)


def test_output_budget_and_source_kind_binding():
    value=copy.deepcopy(FIXTURES["first"])
    for kw in ({"limit":100},{"kind":"tree"},{"fmt":"db"},{"size":True},{"size":1}):
        with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value,**kw)


def test_catalog_never_claims_user_rows_loaded():
    value=copy.deepcopy(FIXTURES["tree"]);value["metadata"]["rows_returned"]=1
    with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value)


def test_tree_column_count_must_not_use_boolean_for_one():
    value=copy.deepcopy(FIXTURES["tree"]);value["tree"][0]["attributes"]["columns"]=True
    with pytest.raises(SqliteTableError):validate_sqlite_table_payload(value)


def test_pure_contract_parity_no_native_imports():
    root=Path(__file__).resolve().parents[2]
    host=(root/"backend/app/application/services/sqlite_table_visualization.py").read_bytes()
    assert host==(root/"sandbox/app/services/sqlite_table_payload.py").read_bytes()
    assert b"import sqlite3" not in host and b"import apsw" not in host
