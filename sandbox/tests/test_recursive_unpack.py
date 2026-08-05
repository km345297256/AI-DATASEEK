import base64
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import zipfile

import pytest

from scripts.recursive_unpack import Limits, UnpackError, unpack_recursive


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
