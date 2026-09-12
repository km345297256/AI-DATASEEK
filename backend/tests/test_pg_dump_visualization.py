"""No native database dependency is imported into the backend validator."""
import copy
from pathlib import Path

import pytest

from app.application.services.pg_dump_visualization import (
    LIMITS, PGDumpError, WARNING, validate_pg_dump_options, validate_pg_dump_payload,
)


def payload():
    return {
        "type": "pg-dump", "reader": "pg-dump", "kind": "tree",
        "contract_version": 2, "media_type": "application/json",
        "tree": [{"path": "/o-" + "ab" * 12, "node_type": "group", "attributes": {
            "object_type": "TABLE", "schema": "science lab", "name": "测量 data"}}],
        "metadata": {
            "engine": "pg-dump", "format": "dump", "container": "PostgreSQL custom archive",
            "archive_version": "1.16.0", "source_bytes": 1024, "input_mode": "whole",
            "objects_returned": 1, "objects_total": 1, "data_verified": False, "limits": dict(LIMITS),
        }, "warnings": [WARNING], "sampled": False, "choices": {}, "selected": {},
    }


def test_exact_contract_is_accepted_and_backend_copy_matches_sandbox():
    value = payload()
    assert validate_pg_dump_payload(value, fmt="dump", kind="tree", options={}, size=1024) is value
    root = Path(__file__).resolve().parents[2]
    assert (root / "backend/app/application/services/pg_dump_visualization.py").read_bytes() == (root / "sandbox/app/services/pg_dump_payload.py").read_bytes()


@pytest.mark.parametrize("path,value", [
    (("type",), "database-table"), (("reader",), "sql-dump"), (("kind",), "table"),
    (("contract_version",), 1), (("contract_version",), True), (("media_type",), "text/html"),
    (("sampled",), True), (("sampled",), 0), (("choices",), []), (("selected",), {"sql": "SELECT 1"}),
    (("warnings",), []), (("metadata", "engine"), "postgres-server"), (("metadata", "format"), "sql"),
    (("metadata", "container"), "postgres"), (("metadata", "archive_version"), "1.17.0"),
    (("metadata", "source_bytes"), True), (("metadata", "source_bytes"), 16777217),
    (("metadata", "input_mode"), "stream"), (("metadata", "data_verified"), True),
    (("metadata", "objects_total"), 2), (("metadata", "objects_returned"), True),
    (("metadata", "limits", "max_toc_bytes"), 16777216),
    (("tree", 0, "node_type"), "table"), (("tree", 0, "path"), "/Users/private"),
    (("tree", 0, "attributes", "object_type"), "USER SQL"),
    (("tree", 0, "attributes", "schema"), "/home/private"),
    (("tree", 0, "attributes", "name"), "<img src=x onerror=alert(1)>"),
    (("tree", 0, "attributes", "name"), "a\u202eb"),
    (("tree", 0, "attributes", "name"), "x" * 129),
    (("tree", 0, "attributes", "name"), ""),
])
def test_forged_worker_payload_is_rejected(path, value):
    result = payload()
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(PGDumpError): validate_pg_dump_payload(result)


def test_private_metadata_unknown_fields_duplicate_nodes_and_budget_rejected():
    for path in [(), ("metadata",), ("tree", 0), ("tree", 0, "attributes")]:
        value = payload()
        target = value
        for key in path:
            target = target[key]
        target["owner"] = "private owner"
        with pytest.raises(PGDumpError): validate_pg_dump_payload(value)
    value = payload()
    value["tree"].append(copy.deepcopy(value["tree"][0]))
    value["metadata"].update(objects_total=2, objects_returned=2)
    with pytest.raises(PGDumpError): validate_pg_dump_payload(value)
    for kwargs in [{"fmt": "tar"}, {"size": 1025}, {"limit": 16}, {"limit": True}, {"kind": "table"}, {"options": {"file": "/tmp/x"}}]:
        with pytest.raises(PGDumpError): validate_pg_dump_payload(payload(), **kwargs)


@pytest.mark.parametrize("options", [{"table": "x"}, {"sql": "SELECT 1"}, {"filename": "/tmp/x"}, {"entry": "1"}, [], None])
def test_no_selection_or_command_api(options):
    with pytest.raises(PGDumpError): validate_pg_dump_options("tree", options)


@pytest.mark.parametrize("object_type", ["DATABASE", "DATABASE PROPERTIES", "COMMENT", "ACL", "USER MAPPING", "FOREIGN SERVER", "SUBSCRIPTION", "OTHER"])
def test_sensitive_object_tags_must_be_fixed_redacted_labels(object_type):
    value = payload()
    attrs = value["tree"][0]["attributes"]
    attrs["object_type"] = object_type
    with pytest.raises(PGDumpError): validate_pg_dump_payload(value)
    attrs.update(name="名称已隐藏", schema="无命名空间")
    assert validate_pg_dump_payload(value) is value
