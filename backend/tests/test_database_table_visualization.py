"""Validate native-produced previews without loading a native DB in FastAPI."""
import copy
import json
from pathlib import Path

import pytest
from app.application.services.database_table_visualization import (
    DatabaseTableError, validate_database_table_options, validate_database_table_payload,
)

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/browser/database-native-data.json"


@pytest.fixture
def data(): return json.loads(FIXTURE.read_text())


def test_native_catalog_pages_column_subset_and_empty_are_accepted(data):
    for value in data.values():
        assert validate_database_table_payload(value, kind=value["kind"], options=value["selected"], fmt="duckdb", size=value["metadata"]["source_bytes"]) is value
    assert data["first"]["table"]["rows"][0][0]["value"] == "170141183460469231731687303715884105727"
    assert data["first"]["table"]["rows"][1][1]["value"] == "-99999999999999999999.123456789012345678"


@pytest.mark.parametrize("path,value", [
    (("contract_version",),1), (("contract_version",),True), (("reader",),"sqlite-table"), (("kind",),"text"),
    (("metadata","engine"),"sqlite"), (("metadata","format"),"dbf"), (("metadata","container"),"SQLite 3"),
    (("metadata","ordering"),"rowid-ascending"), (("metadata","source_bytes"),True), (("metadata","total_rows_known"),True),
    (("metadata","rows_returned"),99), (("metadata","blob_values"),99), (("metadata","limits","max_rows"),201),
    (("choices","tables",1,"id"),"measurements"), (("choices","tables",1,"label"),"/Users/private.db"),
    (("choices","tables",1,"columns",0,"nullable"),1), (("choices","tables",1,"columns",0,"primary_key"),True),
    (("choices","tables",1,"columns",0,"previewable"),False), (("choices","tables",1,"columns",0,"data_type"),"sql-injection"),
    (("table","row_ids"),["1","0"]), (("table","row_ids"),[0,1]), (("table","row_ids"),["0","0"]),
    (("table","rows",0,0,"value"),9007199254740993), (("table","rows",0,0,"value"),"1e3"),
    (("table","rows",0,0,"type"),"decimal"),
    (("table","rows",0,1,"value"),1.23), (("table","rows",0,1,"value"),"1e38"),
    (("table","rows",0,2,"value"),float("nan")), (("table","rows",0,3,"value"),"/private/secret"),
    (("table","rows",0,5,"bytes"),True), (("table","column_ids"),[0]), (("table","has_more"),1),
])
def test_untrusted_worker_payload_strictly_rejected(data, path, value):
    target = data["first"]
    for key in path[:-1]: target = target[key]
    target[path[-1]] = value
    with pytest.raises(DatabaseTableError): validate_database_table_payload(data["first"])


def test_payload_cannot_add_private_path_sql_or_relax_size(data):
    for target in (data["tree"], data["first"]):
        for where in ((), ("metadata",), ("choices",)):
            changed = copy.deepcopy(target); nested = changed
            for key in where: nested = nested[key]
            nested["path"] = "/tmp/secret"
            with pytest.raises(DatabaseTableError): validate_database_table_payload(changed)
        with pytest.raises(DatabaseTableError): validate_database_table_payload(target, size=target["metadata"]["source_bytes"]+1)
        with pytest.raises(DatabaseTableError): validate_database_table_payload(target, limit=16)


@pytest.mark.parametrize("change", [{"table":"Measurements"},{"columns":[True]},{"columns":[0,0]},{"columns":[]},{"row_limit":201},{"row_offset":100001},{"sql":"SELECT 1"},{"filename":"/tmp/a"}])
def test_options_explicit_selection_only(data, change):
    with pytest.raises(DatabaseTableError): validate_database_table_options("table", {**data["first"]["selected"], **change})


def test_temporal_null_boolean_and_exact_limits(data):
    from app.application.services.database_table_visualization import validate_cell
    for cell in [
        {"type":"boolean","value":False},{"type":"null","value":None}, {"type":"nonfinite","value":None},
        {"type":"date","value":"2026-09-11"},{"type":"time","value":"12:00:00.000000001"},
        {"type":"integer","value":str(2**128-1)}, {"type":"decimal","value":"0.00000000000000000000000000000000000001"},
    ]: validate_cell(cell)
    for cell in [{"type":"integer","value":str(2**128)},{"type":"boolean","value":0},{"type":"blob","bytes":16777217},
                 {"type":"text-omitted","bytes":513,"reason":"unsafe-text"},{"type":"text","value":"file:/private/a"}]:
        with pytest.raises(DatabaseTableError): validate_cell(cell)


def test_backend_contract_matches_sandbox_byte_for_byte():
    root = Path(__file__).resolve().parents[2]
    assert (root/"sandbox/app/services/database_table_payload.py").read_bytes() == (root/"backend/app/application/services/database_table_visualization.py").read_bytes()
