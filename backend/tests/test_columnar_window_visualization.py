import pytest

from app.application.services.columnar_window_visualization import (
    ColumnarWindowError, LIMITS, SEMANTICS, WARNING,
    validate_columnar_window_options, validate_columnar_window_payload,
)


def fixture(kind="table"):
    columns = [{"id": i, "label": name, "type": typ, "nullable": True, "precision": 20 if i == 1 else None, "scale": 4 if i == 1 else None}
               for i, (name, typ) in enumerate([("identity", "int64"), ("decimal", "decimal128"), ("value", "float64"), ("text", "string"), ("ok", "bool")])]
    result = {"contract_version": 2, "type": "columnar-window", "reader": "columnar-window", "kind": kind, "media_type": "application/json",
        "choices": {"columns": columns}, "selected": {} if kind == "tree" else {"columns": [0, 1, 2, 3, 4], "row_offset": 0, "row_limit": 200},
        "warnings": [WARNING], "sampled": False,
        "metadata": {"format": "parquet", "container": "Parquet", "input_mode": "window", "value_semantics": SEMANTICS,
            "source_bytes": 1024**3, "read_bytes": 4096, "read_requests": 4, "total_rows": 2, "total_columns": 5, "total_groups": 1,
            "metadata_bytes": 1024, "groups_read": 0 if kind == "tree" else 1, "scan_rows": 0 if kind == "tree" else 2,
            "decoded_bytes": 0 if kind == "tree" else 128, "blocks_checked": 0 if kind == "tree" else 5, "nonfinite_values": 0 if kind == "tree" else 1, "limits": dict(LIMITS)}}
    if kind == "tree": result["tree"] = [{"path": "/column-" + str(c["id"]), "node_type": "column", "attributes": {"label": c["label"], "type": c["type"]}} for c in columns]
    else: result["table"] = {"columns": [c["label"] for c in columns], "rows": [[str(2**63-1), "1234567890123456.1200", 1.0000000000000002, "<text>", True], [str(-2**63), "-0.0100", None, "", False]], "row_offset": 0, "total_rows": 2, "total_columns": 5}
    return result


@pytest.mark.parametrize("kind", ["tree", "table"])
def test_valid_bound_response(kind):
    result = fixture(kind)
    assert validate_columnar_window_payload(result, kind=kind, options=result["selected"], fmt="parquet", source_bytes=1024**3, read_bytes=4096, read_requests=4) is result


@pytest.mark.parametrize("binding", [{"kind": "tree"}, {"fmt": "arrow"}, {"source_bytes": 100}, {"read_bytes": 32}, {"read_requests": 3}, {"options": {"columns": [1, 0], "row_offset": 0, "row_limit": 200}}])
def test_response_must_match_host_and_request(binding):
    with pytest.raises(ColumnarWindowError): validate_columnar_window_payload(fixture(), **binding)


@pytest.mark.parametrize("path,value", [
    (("kind",), []), (("contract_version",), True), (("reader",), "array-window"), (("sampled",), True), (("warnings",), ["unsafe"]),
    (("metadata", "format"), []), (("metadata", "read_bytes"), 8388609), (("metadata", "read_requests"), 129), (("metadata", "source_bytes"), True),
    (("metadata", "decoded_bytes"), 16777217), (("metadata", "scan_rows"), 262145), (("metadata", "total_rows"), 2**53), (("metadata", "total_groups"), 1025),
    (("metadata", "metadata_bytes"), 1048577), (("metadata", "groups_read"), 0), (("metadata", "nonfinite_values"), 2), (("metadata", "limits", "max_rows"), 201),
    (("choices", "columns", 0, "label"), "/Users/private"), (("choices", "columns", 0, "type"), "binary"), (("choices", "columns", 0, "id"), True),
    (("choices", "columns", 1, "precision"), 19), (("choices", "columns", 1, "scale"), 5), (("choices", "columns", 0, "nullable"), 1),
    (("table", "rows", 0, 0), 9223372036854775807), (("table", "rows", 0, 0), "9223372036854775808"), (("table", "rows", 0, 0), "01"),
    (("table", "rows", 0, 1), "1.2"), (("table", "rows", 0, 1), "1e4"), (("table", "rows", 0, 2), True), (("table", "rows", 0, 2), 10**999),
    (("table", "rows", 0, 2), float("inf")), (("table", "rows", 0, 3), "x"*2049), (("table", "rows", 0, 3), "\ud800"), (("table", "rows", 0, 4), 1),
    (("table", "row_offset"), True), (("table", "columns"), ["other"]), (("selected", "columns"), [0, 0]), (("selected", "row_limit"), 201),
])
def test_strict_payload_types_and_budgets(path, value):
    result = fixture(); target = result
    for key in path[:-1]: target = target[key]
    target[path[-1]] = value
    with pytest.raises(ColumnarWindowError): validate_columnar_window_payload(result)


@pytest.mark.parametrize("kind,options", [(None, {}), ([], {}), ("tree", None), ("tree", {"sql": "SELECT 1"}), ("table", {}),
    ("table", {"columns": [True], "row_offset": 0, "row_limit": 1}), ("table", {"columns": list(range(33)), "row_offset": 0, "row_limit": 1}),
    ("table", {"columns": [128], "row_offset": 0, "row_limit": 1}), ("table", {"columns": [0], "row_offset": -1, "row_limit": 1}),
    ("table", {"columns": [0], "row_offset": 0, "row_limit": 1, "path": "other"})])
def test_invalid_options(kind, options):
    with pytest.raises(ColumnarWindowError): validate_columnar_window_options(kind, options)


@pytest.mark.parametrize("location", ["root", "metadata", "choices", "column", "tree"])
def test_no_unknown_keys_or_source_paths(location):
    result = fixture("tree")
    target = {"root": result, "metadata": result["metadata"], "choices": result["choices"], "column": result["choices"]["columns"][0], "tree": result["tree"][0]}[location]
    target["source_path"] = "/private/secret"
    with pytest.raises(ColumnarWindowError): validate_columnar_window_payload(result)
