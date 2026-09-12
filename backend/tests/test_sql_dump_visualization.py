"""Validate SQL literals without SQLGlot, SQL execution, or DB connections."""
import copy
import json
from pathlib import Path

import pytest
from app.application.services.sql_dump_visualization import (
    SqlDumpError, validate_cell, validate_sql_dump_options, validate_sql_dump_payload,
)

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/browser/sql-dump-data.json"


@pytest.fixture
def data(): return json.loads(FIXTURE.read_text())


def test_native_sqlite_dump_catalog_pages_subsets_empty(data):
    for payload in data.values():
        assert validate_sql_dump_payload(payload, kind=payload["kind"], options=payload["selected"], fmt="sql", size=payload["metadata"]["source_bytes"]) is payload
    assert data["first"]["table"]["rows"][0][0] == {"type": "number-literal", "value": "9223372036854775807"}
    assert data["first"]["table"]["rows"][1][4] == {"type": "text", "value": ""}


@pytest.mark.parametrize("path,value", [
    (("contract_version",), 1), (("contract_version",), True), (("reader",), "database-table"), (("kind",), "text"),
    (("metadata", "dialect"), "auto"), (("metadata", "format"), "dump"), (("metadata", "source_bytes"), True),
    (("metadata", "ordering"), "database-final-state"), (("metadata", "source_rows"), -1), (("metadata", "source_rows"), 1),
    (("metadata", "ignored_statement_count"), 4097), (("metadata", "limits", "max_rows"), 201),
    (("choices", "tables", 1, "id"), "measurements"), (("choices", "tables", 1, "label"), "/private/secret"),
    (("choices", "tables", 1, "columns", 0, "id"), True), (("choices", "tables", 1, "columns", 0, "declared_type"), "load_extension('x')"),
    (("table", "rows", 0, 0, "type"), "integer"), (("table", "rows", 0, 0, "value"), 9007199254740993),
    (("table", "rows", 0, 0, "value"), "NaN"), (("table", "rows", 0, 0, "value"), "1+2"),
    (("table", "rows", 0, 0, "value"), "1" * 129), (("table", "rows", 0, 3, "value"), "/Users/private"),
    (("table", "rows", 0, 4, "value"), 0), (("table", "rows", 0, 5, "bytes"), True),
    (("table", "row_ids"), ["0", "0"]), (("table", "row_ids"), [0, 1]), (("table", "row_ids"), ["1", "2"]),
    (("table", "column_ids"), [0]), (("table", "has_more"), 1), (("selected", "dialect"), "postgres"),
])
def test_worker_mutations_rejected(data, path, value):
    target = data["first"]
    for key in path[:-1]: target = target[key]
    target[path[-1]] = value
    with pytest.raises(SqlDumpError): validate_sql_dump_payload(data["first"])


def test_private_fields_binding_and_output_budgets(data):
    for original in [data["tree"], data["first"]]:
        for path in [(), ("choices",), ("metadata",)]:
            changed = copy.deepcopy(original); target = changed
            for key in path: target = target[key]
            target["sql"] = "COPY t FROM PROGRAM '/private/missing'"
            with pytest.raises(SqlDumpError): validate_sql_dump_payload(changed)
        with pytest.raises(SqlDumpError): validate_sql_dump_payload(original, size=original["metadata"]["source_bytes"] + 1)
        with pytest.raises(SqlDumpError): validate_sql_dump_payload(original, fmt="sqlite")
        with pytest.raises(SqlDumpError): validate_sql_dump_payload(original, limit=10)


@pytest.mark.parametrize("options", [{}, {"dialect": "auto"}, {"dialect": "sqlite", "sql": "SELECT 1"}, {"dialect": True}, None])
def test_tree_requires_explicit_known_dialect(options):
    with pytest.raises(SqlDumpError): validate_sql_dump_options("tree", options)


@pytest.mark.parametrize("change", [{"columns": []}, {"columns": [True]}, {"columns": [0, 0]}, {"columns": list(range(17))},
    {"row_limit": 201}, {"row_offset": 100001}, {"table": "name"}, {"dialect": "AUTO"}, {"sql": "SELECT 1"}])
def test_page_selection_is_explicit_and_bounded(data, change):
    with pytest.raises(SqlDumpError): validate_sql_dump_options("table", {**data["first"]["selected"], **change})


def test_literal_cells_have_no_numeric_coercion_and_null_is_distinct():
    for value in ["+000.0012300", "-1e+400", ".001", "1.", "9" * 128]: validate_cell({"type": "number-literal", "value": value})
    for cell in [{"type": "text", "value": ""}, {"type": "boolean", "value": False}, {"type": "null", "value": None}, {"type": "blob", "bytes": 0}]: validate_cell(cell)
    for cell in [{"type": "number-literal", "value": 0}, {"type": "boolean", "value": 0}, {"type": "text", "value": "\ud800"},
        {"type": "blob", "bytes": 16777217}, {"type": "text-omitted", "bytes": 1, "reason": "cell-budget"}]:
        with pytest.raises((SqlDumpError, UnicodeError)): validate_cell(cell)


def test_backend_and_sandbox_contract_copies_are_byte_identical():
    root = Path(__file__).resolve().parents[2]
    assert (root / "sandbox/app/services/sql_dump_payload.py").read_bytes() == (root / "backend/app/application/services/sql_dump_visualization.py").read_bytes()
