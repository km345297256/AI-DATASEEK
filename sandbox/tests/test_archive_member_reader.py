"""Archive-scoped member reads, integrity and no-filesystem regression tests."""
import builtins
import io
import json
import stat
import struct
import tarfile
import zipfile
import zlib

import pytest

from app.services import archive_member_reader as reader
from app.services.bounded_format_readers import BoundedFormatError
from test_bounded_format_readers import zip_bytes, tar_bytes


def directory(data, fmt="zip", offset=0):
    return reader.archive_member_preview(data, fmt, {"row_offset": offset})


def member(data, fmt="zip", index=0, offset=0):
    result = directory(data, fmt)
    token = result["table"]["rows"][index][4]
    return reader.archive_member_preview(data, fmt, {"member_id": token, "row_offset": offset})


@pytest.mark.parametrize("fmt,build", [("zip", zip_bytes), ("tar", tar_bytes)])
def test_directory_is_bounded_and_content_is_explicitly_selected(fmt, build):
    data = build([("numbers.csv", b"n,v\n9007199254740993,0.1234567890123456789\n"), ("nested.zip", b"not decoded")])
    result = directory(data, fmt)
    rows = result["table"]["rows"]
    assert rows[0][3] is True and rows[0][4].startswith("member-")
    assert rows[1][3:] == [False, None]
    assert result["metadata"]["contents_verified"] is False
    preview = member(data, fmt)
    assert preview["table"]["rows"] == [[1, "n,v"], [2, "9007199254740993,0.1234567890123456789"]]
    assert preview["metadata"]["checksum_verified"] == (fmt == "zip")
    assert preview["metadata"]["writes_source"] is False
    assert preview["metadata"]["recursive"] is False


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_supported_zip_methods_and_content_crc(method):
    data = zip_bytes([("test.txt", b"alpha\nbeta\n")], compression=method)
    result = member(data)
    assert result["table"]["rows"] == [[1, "alpha"], [2, "beta"]]
    assert result["metadata"]["checksum_verified"] is True


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_unimplemented_compression_is_directory_only(method):
    data = zip_bytes([("test.txt", b"alpha\nbeta\n")], compression=method)
    assert directory(data)["table"]["rows"][0][3:] == [False, None]


def test_readers_never_open_or_extract_files(monkeypatch):
    zipped, tarred = zip_bytes([("test.txt", b"hello")]), tar_bytes([("test.txt", b"hello")])
    originals = [bytes(zipped), bytes(tarred)]
    def fail(*_, **__):
        pytest.fail("Member preview must not open filesystem files or call extract APIs")
    monkeypatch.setattr(builtins, "open", fail)
    for cls in (zipfile.ZipFile, tarfile.TarFile):
        monkeypatch.setattr(cls, "extract", fail)
        monkeypatch.setattr(cls, "extractall", fail)
    monkeypatch.setattr(zipfile.ZipFile, "open", fail)
    monkeypatch.setattr(tarfile.TarFile, "extractfile", fail)
    assert member(zipped)["table"]["rows"][0][1] == "hello"
    assert member(tarred, "tar")["table"]["rows"][0][1] == "hello"
    assert originals == [zipped, tarred]


def test_member_reference_bound_to_entire_archive_content():
    first = zip_bytes([("same.txt", b"one"), ("other.txt", b"a")])
    changed = zip_bytes([("same.txt", b"one"), ("other.txt", b"b")])
    token = directory(first)["table"]["rows"][0][4]
    assert token != directory(changed)["table"]["rows"][0][4]
    with pytest.raises(BoundedFormatError, match="不属于"):
        reader.archive_member_preview(changed, "zip", {"member_id": token})


@pytest.mark.parametrize("fmt,build", [("zip", zip_bytes), ("tar", tar_bytes)])
def test_directory_and_text_pagination(fmt, build):
    data = build([(f"file-{i:03}.txt", b"line\n" * 405) for i in range(205)])
    second = directory(data, fmt, 200)
    assert len(second["table"]["rows"]) == 5 and second["table"]["total_rows"] == 205
    text = member(data, fmt, offset=200)
    assert text["table"]["row_offset"] == 200 and text["table"]["rows"][0] == [201, "line"]
    assert text["table"]["total_rows"] == 405 and text["sampled"] is True
    assert len(member(data, fmt, offset=400)["table"]["rows"]) == 5


def test_long_lines_and_tabs_are_explicitly_truncated():
    result = member(zip_bytes([("long.txt", ("\t" * 400 + "a" * 2000).encode())]))
    assert len(result["table"]["rows"][0][1]) == 1024
    assert result["sampled"] is True


def test_html_xml_scripts_are_inert_text_without_execution():
    text = "<script>alert(1)</script>\n<!DOCTYPE x [<!ENTITY e SYSTEM 'file:/secret'>]>\n<item>&e;</item>"
    result = member(zip_bytes([("content.xml", text.encode())]))
    assert result["table"]["rows"][0][1] == "<script>alert(1)</script>"
    assert result["table"]["rows"][1][1] == "[redacted]"
    assert result["table"]["rows"][2][1] == "<item>&e;</item>"
    assert result["metadata"]["member_text_only"] is True


@pytest.mark.parametrize("contents", [b"\0", b"\xff\xfe", b"\x1b[31m", "unsafe\u202e".encode(), b"abc\x7f"])
def test_binary_and_unsafe_control_characters_rejected(contents):
    with pytest.raises(BoundedFormatError):
        member(zip_bytes([("data.txt", contents)]))


@pytest.mark.parametrize("name", ["/absolute.txt", "../parent.txt", "foo/../bar.txt", "C:/drive.txt", "dir\\file.txt",
    "a//file.txt", "dir/./file.txt", "space /file.txt", "file\u202e.txt"])
def test_path_safety_validates_all_pages(name):
    data = zip_bytes([(f"{i}.txt", b"ok") for i in range(205)] + [(name, b"unsafe")])
    with pytest.raises(BoundedFormatError):
        directory(data)


@pytest.mark.parametrize("names", [["same.txt", "same.txt"], ["a.txt", "A.txt"], ["a", "a/file.txt"], ["é.txt", "e\u0301.txt"]])
def test_duplicate_and_conflicting_paths_rejected(names):
    with pytest.raises(BoundedFormatError):
        directory(zip_bytes([(name, b"x") for name in names]))


def test_zip_symlink_rejected():
    info = zipfile.ZipInfo("link.txt")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(BoundedFormatError):
        directory(zip_bytes([(info, b"target")]))


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.GNUTYPE_SPARSE])
def test_tar_links_devices_and_sparse_rejected(kind):
    info = tarfile.TarInfo("unsafe.txt")
    info.type, info.linkname = kind, "target"
    with pytest.raises(BoundedFormatError):
        directory(tar_bytes([(info, b"")]), "tar")


def test_tar_header_checksum_and_hidden_tail_rejected():
    data = bytearray(tar_bytes([("safe.txt", b"ok")]))
    data[0] ^= 1
    with pytest.raises(BoundedFormatError):
        directory(bytes(data), "tar")
    with pytest.raises(BoundedFormatError):
        directory(tar_bytes([("safe.txt", b"ok")]) + b"hidden", "tar")


def test_tar_pax_and_gnu_long_name_offset_matches_content():
    for fmt in (tarfile.PAX_FORMAT, tarfile.GNU_FORMAT):
        data = tar_bytes([("long/" + "x" * 120 + ".txt", b"payload")], format=fmt)
        assert member(data, "tar")["table"]["rows"] == [[1, "payload"]]


def test_zip_actual_crc_error_fails_selected_preview_not_directory():
    data = bytearray(zip_bytes([("safe.txt", b"hello")]))
    start = 30 + len("safe.txt")
    data[start] ^= 1
    assert directory(bytes(data))["metadata"]["contents_verified"] is False
    with pytest.raises(BoundedFormatError, match="CRC"):
        member(bytes(data))


def test_forged_declared_size_and_prefix_crc_cannot_hide_deflate_output():
    content, prefix = b"visible\nhidden bytes", b"visible"
    data = bytearray(zip_bytes([("safe.txt", content)], compression=zipfile.ZIP_DEFLATED))
    central = data.index(b"PK\x01\x02")
    for offset in (14, central + 16):
        struct.pack_into("<I", data, offset, zlib.crc32(prefix))
    for offset in (22, central + 24):
        struct.pack_into("<I", data, offset, len(prefix))
    # Python ZipExtFile returns just this forged prefix. Our parser must verify
    # the actual bounded deflate output instead of trusting that truncation.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.read("safe.txt") == prefix
    with pytest.raises(BoundedFormatError, match="实际大小"):
        member(bytes(data))


def test_zip_local_header_size_mismatch_rejected_even_in_directory():
    data = bytearray(zip_bytes([("safe.txt", b"hello")]))
    struct.pack_into("<I", data, 22, 4)
    with pytest.raises(BoundedFormatError):
        directory(bytes(data))


@pytest.mark.parametrize("trailer", [b"JUNK", None], ids=["trailing-compressed-data", "truncated-stream"])
def test_deflate_actual_stream_end_is_verified(trailer):
    data = bytearray(zip_bytes([("safe.txt", b"hello\nworld")], compression=zipfile.ZIP_DEFLATED))
    central = data.index(b"PK\x01\x02")
    delta = len(trailer) if trailer is not None else -1
    if trailer is None:
        del data[central - 1]
    else:
        data[central:central] = trailer
    new_central = central + delta
    compressed = struct.unpack_from("<I", data, 18)[0] + delta
    struct.pack_into("<I", data, 18, compressed)
    struct.pack_into("<I", data, new_central + 20, compressed)
    end = data.index(b"PK\x05\x06", new_central)
    struct.pack_into("<I", data, end + 16, new_central)
    with pytest.raises(BoundedFormatError, match="压缩流"):
        member(bytes(data))


def test_standard_zip_data_descriptor_is_supported_without_disk():
    class NonSeekable(io.BytesIO):
        def seek(self, *_):
            raise io.UnsupportedOperation()
    stream = NonSeekable()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("safe.txt", b"descriptor\n")
    data = stream.getvalue()
    assert struct.unpack_from("<H", data, 6)[0] & 8
    assert member(data)["table"]["rows"] == [[1, "descriptor"]]


def test_zip_ratio_budget_and_member_size_limits():
    with pytest.raises(BoundedFormatError):
        directory(zip_bytes([("bomb.txt", b"x" * 1048576)], compression=zipfile.ZIP_DEFLATED))
    result = directory(zip_bytes([("big.txt", b"x" * (262144 + 1))]))
    assert result["table"]["rows"][0][3:] == [False, None]


@pytest.mark.parametrize("options", [[], {"path": "safe.txt"}, {"member_id": "safe.txt"}, {"member_id": None},
    {"member_id": "member-" + "0" * 64}, {"row_offset": True}, {"row_offset": -1}, {"row_offset": 262145},
    {"extract": True}, {"format": "zip"}, {"row_offset": 1}])
def test_invalid_member_options(options):
    with pytest.raises(BoundedFormatError):
        reader.archive_member_preview(zip_bytes([("safe.txt", b"hello")]), "zip", options)


def test_empty_text_and_utf8_bom():
    assert member(zip_bytes([("empty.txt", b"")]))["table"]["rows"] == [[1, ""]]
    assert member(zip_bytes([("bom.txt", b"\xef\xbb\xbfhello")]))["table"]["rows"] == [[1, "hello"]]


def test_empty_archives_are_valid_directories():
    for fmt, data in (("zip", zip_bytes([])), ("tar", tar_bytes([]))):
        result = directory(data, fmt)
        assert result["table"]["total_rows"] == 0 and result["table"]["rows"] == []
        assert result["sampled"] is False
