"""Pure contract tests do not require a database, sandbox or API server."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ModuleType("_records_contract_test")
PACKAGE.__path__ = [str(ROOT / "backend/app/application/services")]
sys.modules[PACKAGE.__name__] = PACKAGE
SPEC = importlib.util.spec_from_file_location(PACKAGE.__name__ + ".payload", ROOT / "backend/app/application/services/database_records_visualization.py")
contract = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(contract)
FIXTURES = json.loads((ROOT / "frontend/tests/browser/database-records-data.json").read_text())


def test_pure_contract_copies_identical():
    assert (ROOT / "sandbox/app/services/database_records_payload.py").read_bytes() == (ROOT / "backend/app/application/services/database_records_visualization.py").read_bytes()


@pytest.mark.parametrize("reader", ["bson", "redis-rdb"])
@pytest.mark.parametrize("variant", ["tree", "first", "second", "empty", "emptyPage"])
def test_fixture_roundtrip(reader, variant):
    value = copy.deepcopy(FIXTURES[reader][variant])
    assert contract.validate_database_records_payload(value, reader=reader, kind=value["kind"], options=value["selected"], fmt=value["metadata"]["format"], size=value["metadata"]["source_bytes"]) == value


def mutate(path, changed):
    def action(value):
        target = value
        for key in path[:-1]: target = target[key]
        target[path[-1]] = changed
    return action


@pytest.mark.parametrize("action", [
    mutate(["contract_version"], 1), mutate(["contract_version"], True), mutate(["type"], "redis-rdb"),
    mutate(["reader"], "sqlite-table"), mutate(["media_type"], "text/html"), mutate(["warnings"], []),
    mutate(["sampled"], False), mutate(["metadata", "source_bytes"], True), mutate(["metadata", "source_bytes"], 16777217),
    mutate(["metadata", "format_version"], 11), mutate(["metadata", "format"], "wt"),
    mutate(["metadata", "checksum"], "verified"), mutate(["metadata", "nodes_returned"], 4097),
    mutate(["metadata", "limits", "max_depth"], 100), mutate(["metadata", "total_records"], 100001),
    mutate(["choices", "groups", 0, "label"], "/Users/private"), mutate(["choices", "groups", 0, "id"], "g-" + "a"*24),
    mutate(["choices", "groups", 0, "record_count"], 1), mutate(["choices", "groups", 0, "counts"], {"javascript": 3}),
    mutate(["selected", "offset"], True), mutate(["selected", "limit"], 51), mutate(["table", "offset"], 1),
    mutate(["table", "has_more"], False), mutate(["table", "records", 0, "key"], {"type": "text", "value": "key"}),
    mutate(["table", "records", 0, "expires_at_ms"], "1"), mutate(["table", "records", 0, "index"], "1"),
    mutate(["table", "records", 0, "truncated"], False),
    mutate(["table", "records", 0, "nodes", 0, "parent"], 0), mutate(["table", "records", 0, "nodes", 0, "cell", "count"], 0),
    mutate(["table", "records", 0, "nodes", 1, "parent"], 1), mutate(["table", "records", 0, "nodes", 1, "id"], 2),
    mutate(["table", "records", 0, "nodes", 1, "key"], "/Users/private"),
    mutate(["table", "records", 0, "nodes", 1, "key_omitted"], True),
    mutate(["table", "records", 0, "nodes", 2, "cell", "value"], 9223372036854775807),
    mutate(["table", "records", 0, "nodes", 2, "cell", "value"], "9223372036854775808"),
    mutate(["table", "records", 0, "nodes", 4, "cell", "bid"], "0"),
    mutate(["table", "records", 0, "nodes", 4, "cell", "value"], "eval(1)"),
    lambda value: value.update(extra="unexpected"),
])
def test_reject_malformed_or_unbound_payload(action):
    value = copy.deepcopy(FIXTURES["bson"]["first"]); action(value)
    with pytest.raises(contract.DatabaseRecordsError): contract.validate_database_records_payload(value)


@pytest.mark.parametrize("cell", [
    {"type": "decimal128", "value": "-0.0000", "bid": "000000000000000000000000000038b0"},
    {"type": "decimal128", "value": "1E-6176", "bid": "01000000000000000000000000000000"},
    {"type": "decimal128", "value": "Infinity", "bid": "00000000000000000000000000000078"},
    {"type": "date-ms", "value": "-9223372036854775808"},
    {"type": "timestamp", "seconds": "4294967295", "increment": "4294967295"},
    {"type": "binary", "bytes": 0, "subtype": "ff"},
    {"type": "text", "value": "<script>alert(1)</script>"},
    {"type": "omitted", "bytes": 513, "reason": "text-budget"},
])
def test_precise_inert_values(cell): contract.validate_cell(cell)


@pytest.mark.parametrize("cell", [
    {"type": "decimal128", "value": "1e+4", "bid": "0"*32},
    {"type": "decimal128", "value": "1.2301", "bid": "0c300000000000000000000000003830"},
    {"type": "decimal128", "value": "0.0000", "bid": "000000000000000000000000000038b0"},
    {"type": "decimal128", "value": "1.23", "bid": "0c300000000000000000000000003830"},
    {"type": "date-ms", "value": "1.5"}, {"type": "date-ms", "value": "-0"},
    {"type": "integer", "value": "01"}, {"type": "integer", "value": "+1"},
    {"type": "timestamp", "seconds": "4294967296", "increment": "0"},
    {"type": "binary", "bytes": True, "subtype": None},
    {"type": "binary", "bytes": 1, "subtype": "zzz"},
    {"type": "text", "value": "/private/file"}, {"type": "text", "value": "x"*513},
    {"type": "text", "value": "\ud800"}, {"type": "real", "value": float("nan")},
    {"type": "real", "value": 9007199254740993}, {"type": "boolean", "value": 0},
    {"type": "omitted", "bytes": 512, "reason": "text-budget"},
    {"type": "unsupported", "name": "javascript", "code": "run()"},
    {"type": "document", "count": 128}, {"type": "nonfinite", "value": None},
])
def test_bad_scalar_values(cell):
    with pytest.raises((contract.DatabaseRecordsError, UnicodeError)): contract.validate_cell(cell)


@pytest.mark.parametrize("reader,kwargs", [("bson", {"reader": "redis-rdb"}), ("bson", {"kind": "tree"}),
    ("bson", {"fmt": "rdb"}), ("bson", {"size": 100}), ("bson", {"limit": 10}),
    ("redis-rdb", {"options": {"group_id": "g-" + "0"*24, "offset": 0, "limit": 2}})])
def test_request_binding(reader, kwargs):
    with pytest.raises(contract.DatabaseRecordsError): contract.validate_database_records_payload(copy.deepcopy(FIXTURES[reader]["first"]), **kwargs)


def node(index, parent, key, cell, omitted=False):
    return {"id": index, "parent": parent, "key": key, "key_omitted": omitted, "cell": cell}


def shaped_record(reader, nodes, truncated=False):
    return {"index": "0", "key": None if reader == "bson" else {"type": "text", "value": "key"},
            "expires_at_ms": None, "nodes": nodes, "truncated": truncated}


@pytest.mark.parametrize("reader,nodes", [
    ("bson", [node(0, None, None, {"type": "document", "count": 2}), node(1, 0, "a", {"type": "document", "count": 1}), node(2, 0, "b", {"type": "null", "value": None}), node(3, 1, "late", {"type": "null", "value": None})]),
    ("bson", [node(0, None, None, {"type": "document", "count": 1}), node(1, 0, "bad", {"type": "hash", "count": 0})]),
    ("bson", [node(0, None, None, {"type": "document", "count": 2}), node(1, 0, "same", {"type": "null", "value": None}), node(2, 0, "same", {"type": "null", "value": None})]),
    ("bson", [node(0, None, None, {"type": "document", "count": 1}), node(1, 0, "list", {"type": "array", "count": 1}), node(2, 1, "01", {"type": "null", "value": None})]),
    ("bson", [node(0, None, None, {"type": "document", "count": 1}), node(1, 0, "binary", {"type": "binary", "bytes": 1, "subtype": None})]),
    ("redis-rdb", [node(0, None, None, {"type": "list", "count": 1}), node(1, 0, "0", {"type": "integer", "value": "1"})]),
    ("redis-rdb", [node(0, None, None, {"type": "hash", "count": 1}), node(1, 0, "nested", {"type": "array", "count": 0})]),
    ("redis-rdb", [node(0, None, None, {"type": "set", "count": 1}), node(1, 0, "1", {"type": "text", "value": "one"})]),
    ("redis-rdb", [node(0, None, None, {"type": "zset", "count": 1}), node(1, 0, "0", {"type": "entry", "count": 2}), node(2, 1, "score", {"type": "text", "value": "member"}), node(3, 1, "member", {"type": "real", "value": 1.25})]),
    ("redis-rdb", [node(0, None, None, {"type": "zset", "count": 1}), node(1, 0, "0", {"type": "entry", "count": 2}), node(2, 1, "member", {"type": "text", "value": "one"}), node(3, 1, "score", {"type": "nonfinite", "value": "NaN"})]),
    ("redis-rdb", [node(0, None, None, {"type": "binary", "bytes": 1, "subtype": "04"})]),
])
def test_preorder_and_engine_specific_shapes(reader, nodes):
    with pytest.raises(contract.DatabaseRecordsError): contract.validate_record(shaped_record(reader, nodes), reader)


@pytest.mark.parametrize("reader,root", [("bson", "document"), ("redis-rdb", "hash")])
def test_distinct_unsafe_fields_can_share_explicit_omitted_display_name(reader, root):
    record = shaped_record(reader, [node(0, None, None, {"type": root, "count": 2}),
        node(1, 0, "字段名已省略", {"type": "text", "value": "one"}, True),
        node(2, 0, "字段名已省略", {"type": "text", "value": "two"}, True)], truncated=True)
    assert contract.validate_record(record, reader) == (3, 2, 0)
