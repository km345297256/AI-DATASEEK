"""Byte-level regressions inspired by table and notebook delivery failures.

No generated notebook or analysis code is executed. These checks establish
structure, never scientific values, author attribution or execution provenance.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from app.services import artifact_validation as validation


def receipt(tmp_path, text, *, suffix=".md", kind="report"):
    path = tmp_path / ("result" + suffix)
    path.write_text(text, encoding="utf-8")
    result = validation.validate_artifacts([{"path": str(path), "kind": kind}], root=tmp_path)["files"][0]
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def notebook(minor=5):
    return {"nbformat": 4, "nbformat_minor": minor, "metadata": {}, "cells": [
        {"cell_type": "markdown", "id": "description", "metadata": {}, "source": "# Proposed workflow"},
        {"cell_type": "code", "id": "calculation", "metadata": {}, "source": ["raise RuntimeError('do not execute')\n"],
         "execution_count": None, "outputs": []},
    ]}


def test_escaped_footer_delimiter_preserves_actual_three_column_table(tmp_path):
    text = "# Source evidence\n\n| Field | Evidence | Page |\n| :--- | ---: | --- |\n| Issue | Volume 9 \\| Issue 2 | 1–4 |\n"
    result = receipt(tmp_path, text)
    assert result["valid"] and result["kind"] == "report"
    assert result["metadata"]["table_validation"] == "rectangular"
    assert result["metadata"]["tables"] == [{"row_count": 2, "column_count": 3}]
    assert "Volume 9" not in json.dumps(result)
    as_table = receipt(tmp_path, text, kind="table")
    assert as_table["valid"] and as_table["kind"] == "table"


@pytest.mark.parametrize("bad", [
    "| Field | Evidence | Page |\n| --- | --- | --- |\n| Issue | Volume 9 | Issue 2 | 1–4 |\n",
    "| Topic | Source | Advice |\n| --- | --- | --- |\n| Topic | Page 3 |\n",
    "| A | B |\n| --- | --- | --- |\n| 1 | 2 |\n",
])
def test_ragged_markdown_is_readable_report_but_not_a_validated_table(tmp_path, bad):
    report = receipt(tmp_path, bad)
    assert report["valid"] and report["metadata"]["table_validation"] == "inconsistent"
    table = receipt(tmp_path, bad, kind="table")
    assert not table["valid"] and table["reason"] == "inconsistent_table_width"
    assert table["diagnostics"]["expected_columns"] != table["diagnostics"]["actual_columns"]
    assert "Volume" not in json.dumps(table)


@pytest.mark.parametrize("text", [
    "Here is the requested evidence table.\nNo table was actually supplied.",
    "| A | B |\n| --- | --- |\n",  # Header alone is not populated content.
    "```markdown\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```\n",
    "~~~\n| A | B |\n| --- | --- |\n| 1 | 2 |\n~~~\n",
    "    | A | B |\n    | --- | --- |\n    | 1 | 2 |\n",
])
def test_table_claims_and_code_examples_do_not_supply_a_table(tmp_path, text):
    result = receipt(tmp_path, text, kind="table")
    assert not result["valid"] and result["reason"] == "empty_table"


def test_a_later_ragged_table_cannot_hide_behind_an_earlier_valid_one(tmp_path):
    text = "A|B\n--|--\n1|2\n\nC|D\n--|--\n3|4|5\n"
    result = receipt(tmp_path, text)
    assert result["valid"] and result["metadata"]["table_count"] == 2
    assert result["metadata"]["table_validation"] == "inconsistent"


@pytest.mark.parametrize("text,columns", [
    ("A|B\n--|--\n1|2\n", 2),
    ("| A |\n| :---: |\n| 1 |\n", 1),
    ("| A | B |\n| -- | -- |\n| `a\\|b` | c |\n", 2),
])
def test_supported_pipe_table_variants(tmp_path, text, columns):
    result = receipt(tmp_path, text, kind="table")
    assert result["valid"] and result["metadata"]["tables"][0]["column_count"] == columns


def test_markdown_table_scan_enforces_resource_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "MAX_TABLE_CELLS", 3)
    result = receipt(tmp_path, "A|B\n--|--\n1|2\n", kind="table")
    assert not result["valid"] and result["reason"] == "table_size_limit"


def test_modern_notebook_template_is_preserved_without_executing_or_fabricating_output(tmp_path):
    value = notebook()
    original = json.dumps(value)
    result = receipt(tmp_path, original, suffix=".ipynb", kind="code")
    assert result["valid"] and result["metadata"]["validation_level"] == "notebook_structure"
    assert result["metadata"]["code_cell_count"] == 1
    assert result["metadata"]["executed_cell_count"] == result["metadata"]["output_count"] == 0
    assert (tmp_path / "result.ipynb").read_text() == original
    assert "do not execute" not in json.dumps(result)


def test_legacy_44_cells_do_not_require_ids(tmp_path):
    value = notebook(minor=4)
    for cell in value["cells"]:
        cell.pop("id")
    assert receipt(tmp_path, json.dumps(value), suffix=".ipynb", kind="code")["valid"]


@pytest.mark.parametrize("mutation", [
    lambda v: v.pop("nbformat"),
    lambda v: v.update(nbformat=True),
    lambda v: v.update(nbformat=3),
    lambda v: v.update(nbformat_minor=True),
    lambda v: v.update(metadata=[]),
    lambda v: v["cells"][1].pop("id"),
    lambda v: v["cells"][1].update(id="description"),
    lambda v: v["cells"][1].update(id="invalid id"),
    lambda v: v["cells"][1].update(source=[1]),
    lambda v: v["cells"][1].update(cell_type=[]),
    lambda v: v["cells"][1].update(cell_type={}),
    lambda v: v["cells"][1].pop("execution_count"),
    lambda v: v["cells"][1].update(execution_count=True),
    lambda v: v["cells"][1].update(outputs={}),
    lambda v: v["cells"][0].update(outputs=[]),
    lambda v: v["cells"][1].update(outputs=[{"output_type": "stream", "text": "missing stream name"}]),
    lambda v: v["cells"][1].update(outputs=[{"output_type": "stream", "name": [], "text": "x"}]),
    lambda v: v["cells"][1].update(outputs=[{"output_type": "stream", "name": {}, "text": "x"}]),
    lambda v: v["cells"][1].update(outputs=[{"output_type": [], "text": "x"}]),
    lambda v: v["cells"][1].update(outputs=[{"output_type": {}, "text": "x"}]),
    lambda v: v["cells"][1].update(outputs=[{"output_type": "error", "ename": "Error", "evalue": "x", "traceback": "not a list"}]),
    lambda v: v["cells"][1].update(outputs=[{"output_type": "execute_result", "data": {}, "metadata": {}}]),
])
def test_structurally_invalid_notebooks_do_not_receive_valid_receipts(tmp_path, mutation):
    value = deepcopy(notebook())
    mutation(value)
    result = receipt(tmp_path, json.dumps(value), suffix=".ipynb", kind="code")
    assert not result["valid"] and result["reason"] == "invalid_notebook"


@pytest.mark.parametrize("text,reason", [
    ('{"cells":[],"cells":[],"nbformat":4,"nbformat_minor":5,"metadata":{}}', "duplicate_json_key"),
    ('{"cells":[],"nbformat":4,"nbformat_minor":5,"metadata":{"v":NaN}}', "invalid_json_constant"),
])
def test_notebook_uses_strict_json_without_silent_repairs(tmp_path, text, reason):
    result = receipt(tmp_path, text, suffix=".ipynb", kind="code")
    assert not result["valid"] and result["reason"] == reason


def test_existing_notebook_outputs_are_structure_not_execution_evidence(tmp_path):
    value = notebook()
    value["cells"][1].update(execution_count=7, outputs=[
        {"output_type": "stream", "name": "stdout", "text": ["unverified output\n"]},
        {"output_type": "display_data", "data": {"application/json": {"a": 1}}, "metadata": {}},
        {"output_type": "execute_result", "execution_count": 7, "data": {"text/plain": "1"}, "metadata": {}},
    ])
    result = receipt(tmp_path, json.dumps(value), suffix=".ipynb", kind="code")
    assert result["valid"] and result["metadata"]["executed_cell_count"] == 1
    assert result["metadata"]["output_count"] == 3
    assert "execution_confirmed" not in result["metadata"]
    assert "unverified output" not in json.dumps(result)


@pytest.mark.parametrize("field", ["cell_type", "output_type", "name"])
def test_malformed_notebook_type_receives_a_safe_api_receipt_not_500(client, tmp_path, monkeypatch, field):
    from conftest import BASE_URL
    from app.api.v1 import file as file_api

    value = notebook()
    if field == "cell_type":
        value["cells"][1][field] = {"private_bad_type": True}
    else:
        output = {"output_type": "stream", "name": "stdout", "text": "private_value"}
        output[field] = {"private_bad_type": True}
        value["cells"][1]["outputs"] = [output]
    path = tmp_path / "notebook.ipynb"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(file_api, "validate_artifacts", lambda items, **kwargs:
        validation.validate_artifacts(items, root=tmp_path, **kwargs))
    response = client.post(f"{BASE_URL}/api/v1/file/validate-artifacts",
                          json={"items": [{"path": str(path), "kind": "code"}]})
    assert response.status_code == 200
    result = response.json()["data"]["files"][0]
    assert result["valid"] is False and result["reason"] == "invalid_notebook"
    assert "private_bad_type" not in response.text and "private_value" not in response.text
