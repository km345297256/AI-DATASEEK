import base64
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import zipfile

import pytest

from scripts.recursive_unpack import (
    Limits,
    UnpackError,
    ZIP_DIRECTORY_MAX_BYTES,
    ZIP_METADATA_MAX_BYTES,
    archive_kind,
    unpack_recursive,
    zip_container_kind,
)


# ISC-licensed fixture from markokr/rarfile test/files/rar3-subdirs.rar.
# sha256: d3d4bd8c90b22fa01c11f460589499de06f9d28e076ec3762eea738c0903e874
RAR3_SUBDIRS = base64.b64decode(
    "UmFyIRoHAM+QcwAADQAAAAAAAADiw3QgkDcABgAAAAYAAAADx6QEyTao9FAdMBIApIEAAHN1"
    "YlxkaXIyXGZpbGUyLnR4dACwfLUwZmlsZTIK+lF0IJA/AAgAAAAIAAAAA30kt3FIqPRQHTAa"
    "AKSBAABzdWJcd2l0aCBzcGFjZVxsb25nIGZuLnR4dADwCEdMbG9uZyBmbgojwXQgklcABQAA"
    "AAUAAAADwYnsL+Co9FAdMDIApIEAAHN1YlzDvMi1xKnDtuG4i8OoXGZpbGUudHh0AALGAvw1"
    "KQEg9gse6FwAZmlsZQAudHh0ALByoRVmaWxlChRcdCCQNwAGAAAABgAAAAME9yniMKj0UB0w"
    "EgCkgQAAc3ViXGRpcjFcZmlsZTEudHh0APAChIVmaWxlMQrRdXTgkC0AAAAAAAAAAAADAAAA"
    "ADao9FAUMAgA7UEAAHN1YlxkaXIyALB/JjP75XTgkDMAAAAAAAAAAAADAAAAAEio9FAUMA4A7"
    "UEAAHN1Ylx3aXRoIHNwYWNlAPDLG06903TgkC4AAAAAAAAAAAADAAAAACSo9FAUMAkA7UEAAH"
    "N1YlxlbXB0eQDwcNkb8Ed04JJDAAAAAAAAAAAAAwAAAADgqPRQFDAeAO1BAABzdWJcw7zItcSp"
    "w7bhuIvDqAACxgL8NSkBIPYLHugAsDR2F89rdOCQLQAAAAAAAAAAAAMAAAAAMKj0UBQwCADt"
    "QQAAc3ViXGRpcjEA8MVYh6xSdOCQKAAAAAAAAAAAAAMAAAAA1aj0UBQwAwDtQQAAc3ViALAO"
    "1STEPXsAQAcA"
)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _office_bytes(part: str, mime: str) -> bytes:
    return _zip_bytes({
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/{part}" ContentType="{mime}"/>'
            '</Types>'
        ).encode(),
        "_rels/.rels": b"<Relationships/>",
        part: b"<document/>",
    })


@pytest.mark.parametrize("filename,part,mime,kind", [
    ("renamed.zip", "xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", "excel"),
    ("report.bin", "xl/workbook.xml", "application/vnd.ms-excel.sheet.macroEnabled.main+xml", "excel"),
    ("notes.xlsx", "word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml", "word"),
    ("slides.pptx", "ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml", "presentation"),
])
def test_office_containers_are_identified_by_package_type_not_filename(tmp_path, filename, part, mime, kind):
    source = tmp_path / filename
    source.write_bytes(_office_bytes(part, mime))
    assert zip_container_kind(source) == kind
    assert archive_kind(source) is None
    with pytest.raises(UnpackError, match="not a supported"):
        unpack_recursive(source, tmp_path / "unpacked", Limits())
    assert not (tmp_path / "unpacked").exists()


@pytest.mark.parametrize("members,kind", [
    ({"mimetype": b"application/vnd.oasis.opendocument.spreadsheet", "content.xml": b"<document/>", "META-INF/manifest.xml": b"<manifest/>"}, "opendocument"),
    ({"mimetype": b"application/epub+zip", "META-INF/container.xml": b"<container/>"}, "epub"),
    ({"one.npy": b"\x93NUMPY\x01\x00", "two.npy": b"\x93NUMPY\x01\x00"}, "numpy"),
    ({"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n", "Main.class": b"\xca\xfe\xba\xbe"}, "java"),
])
def test_other_zip_based_formats_are_not_recursively_unpacked(tmp_path, members, kind):
    source = tmp_path / "container.zip"
    source.write_bytes(_zip_bytes(members))
    assert zip_container_kind(source) == kind
    assert archive_kind(source) is None


def test_ordinary_zip_with_workbook_extension_still_recurses(tmp_path):
    nested = _zip_bytes({"values.csv": b"x,y\n1,2\n"})
    source = tmp_path / "misnamed.xlsx"
    source.write_bytes(_zip_bytes({"also-misnamed.xlsx": nested}))
    assert zip_container_kind(source) is None
    assert archive_kind(source) == "zip"
    manifest = unpack_recursive(source, tmp_path / "unpacked", Limits())
    assert manifest["summary"]["archive_count"] == 2
    assert manifest["files"][0]["path"].endswith("/values.csv")


def test_nested_office_files_remain_intact_and_count_against_existing_limits(tmp_path):
    workbook = _office_bytes("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    nested = _zip_bytes({"table.xlsx": workbook})
    source = tmp_path / "dataset.zip"
    source.write_bytes(_zip_bytes({"nested.zip": nested, "renamed.zip": workbook}))
    output = tmp_path / "unpacked"
    manifest = unpack_recursive(source, output, Limits())
    assert manifest["summary"]["archive_count"] == 2
    assert {item["path"] for item in manifest["files"]} == {"nested_contents/table.xlsx", "renamed.zip"}
    assert (output / "nested_contents/table.xlsx").read_bytes() == workbook
    assert (output / "renamed.zip").read_bytes() == workbook
    with pytest.raises(UnpackError, match="single file exceeds"):
        unpack_recursive(source, tmp_path / "limited", Limits(max_single_file_bytes=len(workbook) - 1))
    assert not (tmp_path / "limited").exists()


def test_zip_identification_bounds_directory_before_loading_it(tmp_path):
    source = tmp_path / "oversized.zip"
    data = bytearray(_zip_bytes({"values.csv": b"x\n1\n"}))
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<L", data, end + 12, ZIP_DIRECTORY_MAX_BYTES + 1)
    source.write_bytes(data)
    with pytest.raises(UnpackError, match="identification metadata exceeds"):
        archive_kind(source)


@pytest.mark.parametrize("oversized", [False, True])
def test_zip64_directory_budget_is_checked_even_without_legacy_sentinel(tmp_path, oversized):
    source = tmp_path / "zip64.zip"
    data = _zip_bytes({"values.csv": b"x\n1\n"})
    end = data.rfind(b"PK\x05\x06")
    record = struct.unpack_from("<4s4H2LH", data, end)
    directory_bytes = ZIP_DIRECTORY_MAX_BYTES + 1 if oversized else record[5]
    zip64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
                        record[4], record[4], directory_bytes, record[6])
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
    source.write_bytes(data[:end] + zip64 + locator + data[end:])
    if oversized:
        with pytest.raises(UnpackError, match="identification metadata exceeds"):
            archive_kind(source)
    else:
        assert archive_kind(source) == "zip"
        manifest = unpack_recursive(source, tmp_path / "unpacked", Limits())
        assert manifest["summary"]["file_count"] == 1


def test_zip_identification_bounds_decompressed_format_metadata(tmp_path):
    source = tmp_path / "metadata-bomb.xlsx"
    source.write_bytes(_zip_bytes({
        "[Content_Types].xml": b"x" * (ZIP_METADATA_MAX_BYTES + 1),
        "_rels/.rels": b"<Relationships/>",
        "xl/workbook.xml": b"<workbook/>",
    }))
    with pytest.raises(UnpackError, match="format metadata exceeds"):
        archive_kind(source)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_container_metadata_rejects_entity_declarations_before_xml_parsing(tmp_path, encoding):
    source = tmp_path / "entities.xlsx"
    source.write_bytes(_zip_bytes({
        "[Content_Types].xml": '<!DOCTYPE Types [<!ENTITY item "unsafe">]><Types>&item;</Types>'.encode(encoding),
        "_rels/.rels": b"<Relationships/>",
        "xl/workbook.xml": b"<workbook/>",
    }))
    with pytest.raises(UnpackError, match="entity declarations"):
        archive_kind(source)


def test_office_recognition_does_not_bypass_unsafe_member_checks(tmp_path):
    source = tmp_path / "unsafe.xlsx"
    data = _office_bytes("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    source.write_bytes(data)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("../escape.txt", b"unsafe")
    with pytest.raises(UnpackError, match="unsafe member path"):
        archive_kind(source)


def test_recursively_extracts_nested_zip_and_writes_final_manifest(tmp_path):
    nested = _zip_bytes({"nested/数据.csv": "年份,值\n2020,1\n".encode("utf-8")})
    source = tmp_path / "dataset.zip"
    source.write_bytes(_zip_bytes({"README.txt": b"dataset", "inside.zip": nested}))
    output = tmp_path / "unpacked"

    manifest = unpack_recursive(source, output, Limits())

    assert (output / "README.txt").read_text() == "dataset"
    assert (output / "inside_contents/nested/数据.csv").read_text() == "年份,值\n2020,1\n"
    assert manifest["summary"]["archive_count"] == 2
    assert [entry["path"] for entry in manifest["files"]] == [
        "README.txt",
        "inside_contents/nested/数据.csv",
    ]
    persisted = json.loads((output / "unpack_manifest.json").read_text())
    assert persisted == manifest


@pytest.mark.skipif(shutil.which("unrar") is None, reason="unrar is verified in the image build")
def test_recursively_extracts_rar_inside_zip_with_unrar(tmp_path):
    source = tmp_path / "dataset.zip"
    source.write_bytes(_zip_bytes({"inside.rar": RAR3_SUBDIRS}))
    output = tmp_path / "unpacked"

    manifest = unpack_recursive(source, output, Limits())

    assert manifest["summary"]["archive_count"] == 2
    assert manifest["summary"]["file_count"] == 4
    assert all(not entry["path"].endswith(".rar") for entry in manifest["files"])
    assert (output / "inside_contents/sub/dir1/file1.txt").read_bytes() == b"file1\n"
    assert (output / "inside_contents/sub/dir2/file2.txt").read_bytes() == b"file2\n"


@pytest.mark.parametrize(
    "unsafe_name",
    ["../escape.txt", "/absolute.txt", "C:\\outside.txt", "safe/../../escape.txt"],
)
def test_rejects_zip_path_traversal_transactionally(tmp_path, unsafe_name):
    source = tmp_path / "unsafe.zip"
    source.write_bytes(_zip_bytes({unsafe_name: b"unsafe"}))
    output = tmp_path / "unpacked"

    with pytest.raises(UnpackError):
        unpack_recursive(source, output, Limits())

    assert not output.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_rejects_zip_symbolic_links(tmp_path):
    source = tmp_path / "links.zip"
    with zipfile.ZipFile(source, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../../outside")

    with pytest.raises(UnpackError, match="Symbolic|symbolic"):
        unpack_recursive(source, tmp_path / "unpacked", Limits())


def test_source_root_rejects_archive_symlink_escape(tmp_path):
    allowed_root = tmp_path / "mounted-dataset"
    allowed_root.mkdir()
    outside = tmp_path / "outside.zip"
    outside.write_bytes(_zip_bytes({"data.csv": b"x\n1\n"}))
    linked_archive = allowed_root / "dataset.zip"
    linked_archive.symlink_to(outside)

    with pytest.raises(UnpackError, match="outside the allowed dataset root"):
        unpack_recursive(
            linked_archive,
            tmp_path / "unpacked",
            Limits(),
            allowed_source_root=allowed_root,
        )


def test_rejects_declared_expansion_over_limit_without_partial_output(tmp_path):
    source = tmp_path / "large.zip"
    source.write_bytes(_zip_bytes({"one.bin": b"123456", "two.bin": b"abcdef"}))
    output = tmp_path / "unpacked"

    with pytest.raises(UnpackError, match="total limit"):
        unpack_recursive(
            source,
            output,
            Limits(max_total_bytes=10, max_single_file_bytes=10),
        )

    assert not output.exists()


@pytest.mark.skipif(shutil.which("7z") is None, reason="7z is verified in the image build")
def test_recursively_extracts_nested_7z_inside_zip(tmp_path):
    sevenzip_source = tmp_path / "sevenzip-input"
    sevenzip_source.mkdir()
    (sevenzip_source / "values.csv").write_text("x,y\n1,2\n")
    nested_archive = tmp_path / "inside.7z"
    subprocess.run(
        ["7z", "a", "-bd", "-bb0", str(nested_archive), "values.csv"],
        cwd=sevenzip_source,
        check=True,
        capture_output=True,
        text=True,
    )
    source = tmp_path / "dataset.zip"
    source.write_bytes(_zip_bytes({"inside.7z": nested_archive.read_bytes()}))

    manifest = unpack_recursive(source, tmp_path / "unpacked", Limits())

    assert (tmp_path / "unpacked/inside_contents/values.csv").read_text() == "x,y\n1,2\n"
    assert manifest["summary"]["archive_count"] == 2
    assert [entry["path"] for entry in manifest["files"]] == [
        "inside_contents/values.csv"
    ]


def test_command_prints_final_file_list_as_json(tmp_path):
    source = tmp_path / "dataset.zip"
    source.write_bytes(_zip_bytes({"table.csv": b"a,b\n1,2\n"}))
    output = tmp_path / "output"
    command = Path(__file__).parents[1] / "scripts/recursive_unpack.py"

    result = subprocess.run(
        [sys.executable, str(command), str(source), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["files"] == [{"path": "table.csv", "size": 8}]
