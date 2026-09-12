"""Strict public data shape checked against real independent sandbox payloads."""
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import types
import zipfile

import pytest

from app.application.services.archive_member_visualization import (
    ScientificPreviewRejected, validate_archive_member_options, validate_archive_member_payload,
)


@pytest.fixture
def archive_reader(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "sandbox/app/services"
    package = types.ModuleType("app.services")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "app.services", package)
    for module_name, filename in (("app.services.bounded_format_readers", "bounded_format_readers.py"),
                                  ("independent_archive_member_fixture", "archive_member_reader.py")):
        spec = importlib.util.spec_from_file_location(module_name, root / filename)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, module_name, module)
        spec.loader.exec_module(module)
    return module


def archive(fmt="zip", text=b"alpha\n9007199254740993\n<script>alert(1)</script>"):
    output = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as source:
            source.writestr("sample.txt", text)
            source.writestr("binary.bin", b"\0")
    else:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as source:
            info = tarfile.TarInfo("sample.txt")
            info.size = len(text)
            source.addfile(info, io.BytesIO(text))
    return output.getvalue()


def payload(reader, mode="directory", fmt="zip", *, text=None, offset=0):
    content = archive(fmt) if text is None else archive(fmt, text)
    directory = reader.archive_member_preview(content, fmt)
    if mode == "directory":
        return directory
    return reader.archive_member_preview(content, fmt, {"member_id": directory["table"]["rows"][0][4], "row_offset": offset})


@pytest.mark.parametrize("fmt", ["zip", "tar"])
@pytest.mark.parametrize("mode", ["directory", "text"])
def test_real_sandbox_payload_is_strictly_accepted(archive_reader, fmt, mode):
    result = payload(archive_reader, mode, fmt)
    assert validate_archive_member_payload(result) is result


def test_real_long_unicode_paged_text_and_empty_member(archive_reader):
    for text, offset in (("数".encode() * 2000, 0), (b"line\n" * 405, 200), (b"", 0)):
        result = payload(archive_reader, "text", text=text, offset=offset)
        validate_archive_member_payload(result)


@pytest.mark.parametrize("options", [[], None, False, {"member_id": None}, {"member_id": "../private"},
    {"member_id": "member-" + "A" * 64}, {"row_offset": -1}, {"row_offset": True}, {"row_offset": 262145},
    {"extract": True}, {"path": "a.txt"}, {"url": "https://invalid.example"}])
def test_options_are_only_opaque_member_and_bounded_page(options):
    with pytest.raises(ScientificPreviewRejected):
        validate_archive_member_options(options)


def test_valid_options():
    validate_archive_member_options({})
    validate_archive_member_options({"member_id": "member-" + "a" * 64, "row_offset": 200})


@pytest.mark.parametrize("key,value", [("contract_version", True), ("reader", "archive"), ("type", "archive"),
    ("kind", "image"), ("media_type", "text/html"), ("sampled", "false"), ("warnings", []),
    ("data_base64", "AA=="), ("path", "/private"), ("table", None), ("metadata", [])])
def test_envelope_and_unknown_fields_rejected(archive_reader, key, value):
    result = payload(archive_reader)
    result[key] = value
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)


@pytest.mark.parametrize("key,value", [("writes_source", True), ("recursive", True), ("format", "gz"),
    ("format", []), ("source_bytes", 0), ("source_bytes", True), ("source_bytes", 67108865),
    ("member_limit_bytes", 999999), ("contents_verified", True), ("mode", "html"), ("unknown", "x")])
def test_scope_and_metadata_cannot_escalate(archive_reader, key, value):
    result = payload(archive_reader)
    result["metadata"][key] = value
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)


@pytest.mark.parametrize("mutation", [lambda d: d["table"].update(rows=[]),
    lambda d: d["table"].update(row_offset=True), lambda d: d["table"].update(row_offset=4096),
    lambda d: d["table"].update(column_offset=1), lambda d: d["table"].update(total_rows=4097),
    lambda d: d["table"].update(total_columns=True), lambda d: d["table"].update(unknown="x"),
    lambda d: d["table"]["rows"][0].__setitem__(0, "../private"),
    lambda d: d["table"]["rows"][0].__setitem__(0, "https://invalid.example"),
    lambda d: d["table"]["rows"][0].__setitem__(0, "unsafe\u202e.txt"),
    lambda d: d["table"]["rows"][0].__setitem__(1, []),
    lambda d: d["table"]["rows"][0].__setitem__(2, 2**53),
    lambda d: d["table"]["rows"][0].__setitem__(3, 1),
    lambda d: d["table"]["rows"][0].__setitem__(4, "a.txt"),
    lambda d: d["table"]["rows"][1].__setitem__(4, "member-" + "0" * 64),
    lambda d: d.update(sampled=True)])
def test_malformed_or_forged_directory_payload_rejected(archive_reader, mutation):
    result = payload(archive_reader)
    mutation(result)
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)


@pytest.mark.parametrize("key,value", [("member_id", "sample.txt"), ("member_name", "/private/x"),
    ("member_name", "relative/../x"), ("member_bytes", 262145), ("member_bytes", True),
    ("checksum_verified", False), ("encoding", "html"), ("line_char_limit", 2048),
    ("member_text_only", False), ("url", "file:/private")])
def test_malformed_text_metadata_rejected(archive_reader, key, value):
    result = payload(archive_reader, "text")
    result["metadata"][key] = value
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)


@pytest.mark.parametrize("value", [None, True, 2**53, "\x00", "unsafe\u202e", "\ud800", "x" * 1025,
                                   "/Users/private/file", "https://invalid.example"])
def test_text_values_are_bounded_inert_strings(archive_reader, value):
    result = payload(archive_reader, "text")
    result["table"]["rows"][0][1] = value
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)


def test_text_line_indices_and_sampled_pagination_are_strict(archive_reader):
    result = payload(archive_reader, "text", text=b"row\n" * 405, offset=200)
    result["sampled"] = False
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)
    result["sampled"] = True
    result["table"]["rows"][0][0] = 0
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)


def test_duplicate_visible_member_ids_are_rejected(archive_reader):
    result = payload(archive_reader)
    result["table"]["rows"][1] = list(result["table"]["rows"][0])
    with pytest.raises(ValueError):
        validate_archive_member_payload(result)
