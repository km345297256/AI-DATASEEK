import hashlib
import json
import os
import threading
import time
import zipfile

import pytest
from PIL import Image
from openpyxl import Workbook

from app.services import artifact_validation as validation


def _receipt(path, kind, root, **kwargs):
    return validation.validate_artifacts([{"path": str(path), "kind": kind}], root=root, **kwargs)["files"][0]


def test_valid_image_is_decoded_and_receipt_matches_bytes(tmp_path):
    path = tmp_path / "chart.png"
    Image.new("RGB", (16, 8), "red").save(path)
    receipt = _receipt(path, "image", tmp_path)
    assert receipt["valid"] is True
    assert receipt["metadata"] == {"format": "PNG", "width": 16, "height": 8, "frames": 1}
    assert receipt["size"] == path.stat().st_size
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("case", ["fake", "truncated", "format_mismatch", "too_many_pixels"])
def test_image_rejects_invalid_bytes_not_just_file_existence(tmp_path, monkeypatch, case):
    path = tmp_path / "chart.png"
    Image.new("RGB", (16, 8), "red").save(path)
    if case == "fake":
        path.write_text("a script was prepared, no chart was rendered")
    elif case == "truncated":
        path.write_bytes(path.read_bytes()[:-20])
    elif case == "format_mismatch":
        Image.new("RGB", (16, 8)).save(path, format="JPEG")
    else:
        monkeypatch.setattr(validation, "MAX_IMAGE_PIXELS", 100)
    assert _receipt(path, "image", tmp_path)["valid"] is False


def test_all_image_frames_are_bounded(tmp_path, monkeypatch):
    path = tmp_path / "animated.gif"
    Image.new("RGB", (10, 10), "red").save(
        path, save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")], duration=100,
    )
    monkeypatch.setattr(validation, "MAX_IMAGE_FRAMES", 1)
    assert _receipt(path, "image", tmp_path)["reason"] == "image_size_limit"


@pytest.mark.parametrize("suffix,content,columns", [
    (".csv", "name,value\nsecret,42\n", 2),
    (".tsv", "name\tvalue\nsecret\t42\n", 2),
    (".csv", "single\nsecret\n", 1),
])
def test_tables_report_structure_without_values(tmp_path, suffix, content, columns):
    path = tmp_path / ("table" + suffix)
    path.write_text(content)
    receipt = _receipt(path, "table", tmp_path)
    assert receipt["valid"] is True
    assert receipt["metadata"]["column_count"] == columns
    assert receipt["metadata"]["row_count"] == 2
    assert "secret" not in json.dumps(receipt)


@pytest.mark.parametrize("content", ["", "   \n", "a,b\n1,2,3\n", 'a,b\n"unclosed,2\n'])
def test_empty_or_malformed_csv_is_not_deliverable(tmp_path, content):
    path = tmp_path / "table.csv"
    path.write_text(content)
    assert _receipt(path, "table", tmp_path)["valid"] is False


@pytest.mark.parametrize("suffix,content", [
    (".csv", "private_header,other\nprivate_value,42,extra\n"),
    (".tsv", "private_header\tother\nprivate_value\t42\textra\n"),
])
def test_ragged_table_retains_safe_structural_diagnostics_without_relaxing_validation(tmp_path, suffix, content):
    path = tmp_path / ("table" + suffix)
    path.write_text(content)
    receipt = _receipt(path, "table", tmp_path)
    assert receipt["valid"] is False and receipt["reason"] == "inconsistent_table_width"
    assert receipt["diagnostics"] == {"actual_columns": 3, "expected_columns": 2, "row_number": 2}
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "private_header" not in json.dumps(receipt) and "private_value" not in json.dumps(receipt)


def test_csv_quote_syntax_failure_has_position_not_the_parser_excerpt(tmp_path):
    path = tmp_path / "table.csv"
    path.write_text('first,second\n"private_unclosed_value,2\n')
    receipt = _receipt(path, "table", tmp_path)
    assert not receipt["valid"] and receipt["reason"] == "invalid_csv_syntax"
    assert receipt["diagnostics"] == {"line_number": 2}
    assert "private_unclosed_value" not in json.dumps(receipt)


def test_valid_quoted_csv_preserves_normal_csv_semantics(tmp_path):
    path = tmp_path / "quoted.csv"
    path.write_text('label,value\n"comma, in quoted label",2\n"multiline\nlabel",3\n')
    receipt = _receipt(path, "table", tmp_path)
    assert receipt["valid"] and receipt["metadata"]["row_count"] == 3
    assert receipt["metadata"]["column_count"] == 2 and receipt["diagnostics"] == {}


@pytest.mark.parametrize("suffix,content,reason", [
    (".json", b'{"private_key": }', "invalid_json_syntax"),
    (".py", b'def private_function(:\n    pass', "invalid_code_syntax"),
    (".md", b'private_text\xff', "invalid_text_encoding"),
])
def test_parser_failures_preserve_only_fixed_codes_and_numeric_positions(tmp_path, suffix, content, reason):
    path = tmp_path / ("output" + suffix)
    path.write_bytes(content)
    receipt = _receipt(path, "code" if suffix == ".py" else "report", tmp_path)
    assert not receipt["valid"] and receipt["reason"] == reason
    assert receipt["diagnostics"] and all(type(value) is int and value >= 0 for value in receipt["diagnostics"].values())
    assert "private_" not in json.dumps(receipt)


def test_diagnostic_sanitizer_rejects_content_unknown_fields_bool_and_unbounded_numbers():
    issue = validation._InvalidArtifact("invalid_content", {"row_number": 3, "actual_columns": 5,
        "expected_columns": True, "path": "/Users/private", "field": "secret", "size_bytes": 2**64})
    assert issue.diagnostics == {"actual_columns": 5, "row_number": 3}


def test_csv_scan_does_not_accept_unvalidated_tail(tmp_path, monkeypatch):
    path = tmp_path / "large.csv"
    path.write_text("a,b\n1,2\n3,4\n")
    monkeypatch.setattr(validation, "MAX_TABLE_ROWS", 2)
    receipt = _receipt(path, "table", tmp_path)
    assert receipt["valid"] is False and receipt["reason"] == "table_size_limit"


def test_xlsx_checks_structure_without_evaluating_formulas_or_exposing_cells(tmp_path):
    path = tmp_path / "table.xlsx"
    workbook = Workbook()
    workbook.active.append(["private_column", "value"])
    workbook.active.append(["private_value", "=1+1"])
    workbook.save(path)
    receipt = _receipt(path, "table", tmp_path)
    assert receipt["valid"] is True
    assert receipt["metadata"]["tables"] == [{"row_count": 2, "column_count": 2}]
    assert "private_" not in json.dumps(receipt)


def test_empty_xlsx_is_not_a_table(tmp_path):
    path = tmp_path / "table.xlsx"
    Workbook().save(path)
    assert _receipt(path, "table", tmp_path)["reason"] == "empty_table"


def test_xlsx_zip_bomb_budget_applies_before_parsing(tmp_path, monkeypatch):
    path = tmp_path / "table.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sheet.xml", "x" * 4096)
    monkeypatch.setattr(validation, "MAX_ARCHIVE_BYTES", 1024)
    assert _receipt(path, "table", tmp_path)["reason"] == "archive_size_limit"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_workbook_dtd_is_not_parsed(tmp_path, encoding):
    path = tmp_path / "table.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sheet.xml", '<!DOCTYPE x [<!ENTITY secret "private_value">]><x/>'.encode(encoding))
    receipt = _receipt(path, "table", tmp_path)
    assert receipt["reason"] == "unsafe_workbook_xml"
    assert "private_value" not in json.dumps(receipt)


@pytest.mark.parametrize("content,kind,valid", [
    ('{"summary":"secret"}', "report", True),
    ('[{"a":1},{"a":2}]', "table", True),
    ('[[1,2],[3,4]]', "table", True),
    ('[{"a":1},{"b":2}]', "table", False),
    ('{"summary":"secret"}', "table", False),
    ('{"a":', "report", False),
    ('{"a":NaN}', "report", False),
    ('{"a":1,"a":2}', "report", False),
])
def test_json_requires_complete_parse_and_table_shape(tmp_path, content, kind, valid):
    path = tmp_path / "result.json"
    path.write_text(content)
    receipt = _receipt(path, kind, tmp_path)
    assert receipt["valid"] is valid
    assert "secret" not in json.dumps(receipt)


def test_code_never_satisfies_image_or_table_and_is_never_executed(tmp_path):
    marker = tmp_path / "must-not-exist"
    path = tmp_path / "script.py"
    path.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    for kind in ("image", "table", "report"):
        receipt = _receipt(path, kind, tmp_path)
        assert receipt["valid"] is False and receipt["kind"] == "code"
        assert receipt["reason"] == "kind_mismatch"
    assert _receipt(path, "code", tmp_path)["valid"] is True
    assert not marker.exists()


def test_text_report_must_be_nonempty_and_unimplemented_format_is_explicit(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("# Actual report\nA bounded finding.")
    assert _receipt(path, "report", tmp_path)["valid"] is True
    path.write_bytes(b"\x00binary")
    assert _receipt(path, "report", tmp_path)["valid"] is False
    assert _receipt(tmp_path / "report.pdf", "report", tmp_path)["reason"] == "unsupported_format"


@pytest.mark.parametrize("suffix", [".txt", ".md", ".markdown", ".html", ".htm"])
def test_supported_report_formats_are_validated_as_real_nonempty_text(tmp_path, suffix):
    path = tmp_path / ("report" + suffix)
    path.write_text("A real explanatory result.")
    receipt = _receipt(path, "report", tmp_path)
    assert receipt["valid"] and receipt["metadata"]["validation_level"] == "nonempty_text"


def test_batch_diagnostics_are_per_file_and_do_not_poison_a_valid_image(tmp_path):
    image = tmp_path / "figure.png"
    table = tmp_path / "extra.csv"
    Image.new("RGB", (8, 8), "red").save(image)
    table.write_text("one,two\n1,2,3\n")
    files = validation.validate_artifacts([{"path": str(image), "kind": "image"},
        {"path": str(table), "kind": "table"}], root=tmp_path)["files"]
    assert files[0]["valid"] and files[0]["reason"] == "validated"
    assert not files[1]["valid"] and files[1]["reason"] == "inconsistent_table_width"
    assert files[1]["diagnostics"]["expected_columns"] == 2


def test_no_dataset_private_symlink_fifo_or_directory_is_read(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    private = tmp_path / "private.md"
    private.write_text("must never appear in receipts")
    (output / "link.md").symlink_to(private)
    (output / "parent").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(output / "pipe.md")
    (output / "dir.md").mkdir()
    for path in (private, output / "../private.md", output / "link.md", output / "parent/private.md",
                 output / "pipe.md", output / "dir.md", output / "missing.md"):
        receipt = _receipt(path, "report", output)
        assert receipt["valid"] is False
        assert receipt["sha256"] is None and receipt["size"] is None
        assert "must never appear" not in json.dumps(receipt)


def test_file_and_batch_size_limits_are_fail_closed(tmp_path, monkeypatch):
    paths = [tmp_path / "one.md", tmp_path / "two.md"]
    for path in paths:
        path.write_text("123456")
    monkeypatch.setattr(validation, "MAX_FILE_BYTES", 5)
    assert _receipt(paths[0], "report", tmp_path)["reason"] == "file_size_limit"
    monkeypatch.setattr(validation, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(validation, "MAX_BATCH_BYTES", 10)
    files = validation.validate_artifacts([
        {"path": str(path), "kind": "report"} for path in paths
    ], root=tmp_path)["files"]
    assert files[0]["valid"] is True
    assert files[1]["reason"] == "batch_size_limit"


@pytest.mark.parametrize("condition", ["cancelled", "deadline"])
def test_cancelled_or_expired_batch_never_opens_files(tmp_path, monkeypatch, condition):
    def unexpected_open(*args):
        raise AssertionError("cancelled validation must not open output")
    monkeypatch.setattr(validation, "_open_output", unexpected_open)
    cancelled = threading.Event()
    if condition == "cancelled":
        cancelled.set()
    receipt = _receipt(tmp_path / "report.md", "report", tmp_path, cancelled=cancelled,
                       deadline=time.monotonic() - 1 if condition == "deadline" else time.monotonic() + 10)
    assert receipt["reason"] == "validation_deadline"


def test_changed_file_receives_no_trusted_receipt(tmp_path, monkeypatch):
    path = tmp_path / "report.md"
    path.write_text("initial")
    initial_time = path.stat().st_mtime_ns - 1_000_000_000
    os.utime(path, ns=(initial_time, initial_time))
    original_fstat = os.fstat
    calls = 0
    def racing_stat(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("changed")
        return original_fstat(descriptor)
    monkeypatch.setattr(validation.os, "fstat", racing_stat)
    receipt = _receipt(path, "report", tmp_path)
    assert receipt["reason"] == "changed_during_read"
    assert receipt["sha256"] is None


def test_batch_api_validates_shape_and_never_returns_content(client, tmp_path, monkeypatch):
    from conftest import BASE_URL
    from app.api.v1 import file as file_api
    path = tmp_path / "report.md"
    path.write_text("private report body")
    monkeypatch.setattr(file_api, "validate_artifacts", lambda items, **kwargs: validation.validate_artifacts(
        items, root=tmp_path, **kwargs,
    ))
    response = client.post(f"{BASE_URL}/api/v1/file/validate-artifacts", json={
        "items": [{"path": str(path), "kind": "report"}],
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True and payload["data"]["files"][0]["valid"] is True
    assert "private report body" not in response.text
    for items in ([], [{"path": str(path), "kind": "unknown"}],
                  [{"path": str(path), "kind": "report"}] * 33):
        assert client.post(f"{BASE_URL}/api/v1/file/validate-artifacts", json={"items": items}).status_code == 422
