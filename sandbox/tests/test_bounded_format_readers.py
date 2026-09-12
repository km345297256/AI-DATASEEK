"""Real inert fixtures and adversarial bounds for structure/archive adapters."""
from __future__ import annotations

import gzip
import io
import json
import stat
import struct
import tarfile
import zipfile
import zlib

import pytest

from app.services import bounded_format_readers as reader


def zip_bytes(entries, *, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return output.getvalue()


def tar_bytes(entries, *, format=tarfile.USTAR_FORMAT):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=format) as archive:
        for name, value in entries:
            info = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return output.getvalue()


def values(result):
    return [node["attributes"].get("value") for node in result["tree"]]


@pytest.mark.parametrize("fmt,data", [("json", b'{"signal":[1,2.5,true,null]}'),
                                      ("yaml", b"signal: [1, 2.5, true, null]\n"),
                                      ("yml", b"signal: [1, 2.5, true, null]\n")])
def test_real_json_yaml_structure_is_flat_stable_and_exact(fmt, data):
    result = reader.structure_preview(data, fmt)
    assert result["contract_version"] == 2 and result["reader"] == "structure"
    assert result["kind"] == "tree" and result["media_type"] == "application/json"
    assert result["tree"][0] == {"path": "/0", "node_type": "object", "attributes": {"label": "root", "children_count": 1}}
    assert result["tree"][1]["path"] == "/0/0"
    assert values(result)[2:] == ["1", "2.5", True, None]
    assert result["sampled"] is False


def test_json_precision_and_negative_zero_are_not_coerced():
    result = reader.structure_preview(b'[9007199254740993,0.12345678901234567890123456789,-0,1e9999]', "json")
    assert values(result)[1:] == ["9007199254740993", "0.12345678901234567890123456789", "-0", "1e9999"]
    assert all(node["attributes"]["numeric_representation"] == "source lexeme" for node in result["tree"][1:])
    json.dumps(result, allow_nan=False)


def test_labels_do_not_become_paths_and_sensitive_text_is_redacted():
    result = reader.structure_preview(json.dumps({"/Users/owner/file": "https://private.example/file", "plain": "/ho\nme/owner/file"}).encode(), "json")
    assert [node["path"] for node in result["tree"]] == ["/0", "/0/0", "/0/1"]
    assert result["tree"][1]["attributes"]["label"] == "[redacted]"
    assert values(result)[1:] == ["[redacted]", "[redacted]"]
    assert "/Users/" not in json.dumps(result)


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b"[NaN]", b"[Infinity]", b"[-Infinity]", b'{"a":}', b"[1] extra", b"\xff", b"\x00{}"])
def test_json_invalid_or_ambiguous_input_fails_closed(data):
    with pytest.raises(reader.BoundedFormatError):
        reader.structure_preview(data, "json")


@pytest.mark.parametrize("data", [
    ("[" * 33 + "0" + "]" * 33).encode(),
    ("[" + ",".join("0" for _ in range(4096)) + "]").encode(),
    json.dumps("x" * 65537).encode(),
    ("[" + "1" * 257 + "]").encode(),
])
def test_json_source_budgets_before_or_during_materialization(data):
    with pytest.raises(reader.BoundedFormatError):
        reader.structure_preview(data, "json")


def test_json_depth_preflight_runs_before_json_decoder(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("over-deep input must not reach json.loads")
    monkeypatch.setattr(reader.json, "loads", forbidden)
    with pytest.raises(reader.BoundedFormatError):
        reader.structure_preview(b"[" * 33 + b"0" + b"]" * 33, "json")


def test_structure_display_truncation_is_explicit():
    result = reader.structure_preview(json.dumps({"points": list(range(400)), "long": "x" * 1000}).encode(), "json")
    assert len(result["tree"]) == 256
    assert result["sampled"] is True
    assert result["metadata"]["source_nodes"] == 403
    assert "原文" in "".join(result["warnings"])
    deep = reader.structure_preview(b"[" * 16 + b"0" + b"]" * 16, "json")
    assert len(deep["tree"]) == 8 and deep["sampled"] is True


def test_long_scalar_display_is_not_claimed_complete():
    result = reader.structure_preview(json.dumps("x" * 700).encode(), "json")
    assert len(values(result)[0]) == 512 and result["sampled"] is True


@pytest.mark.parametrize("data", [
    b"a: &a [1, 2]\nb: *a", b"a: &a [*a]", b"a: *missing",
    b"!!python/object/apply:os.system ['touch forbidden']", b"a: !!binary eA==", b"a: !!set {x: null}",
    b"a: {<<: {b: 1}}", b"a: 1\na: 2", b"[1, 2]: value", b"1: value",
    b"---\na: 1\n---\nb: 2", b"a: .inf", b"a: -.Inf", b"a: .NaN", b"", b"# only comment",
    b"a: !!int not-a-number", b"a: !!float not-a-number", b"a: !!bool not-a-bool", b"a: !!null not-null",
])
def test_yaml_rejects_constructors_aliases_merge_duplicate_and_multidoc(data):
    with pytest.raises(reader.BoundedFormatError):
        reader.structure_preview(data, "yaml")


def test_yaml_never_invokes_safe_or_unsafe_constructors(monkeypatch):
    import yaml
    def forbidden(*args, **kwargs):
        pytest.fail("tree composition must not construct application objects")
    monkeypatch.setattr(yaml.SafeLoader, "construct_object", forbidden)
    result = reader.structure_preview(b"date: 2026-09-10\ncount: 0xFF\nname: demo", "yaml")
    assert values(result)[1:] == ["2026-09-10", "0xFF", "demo"]


@pytest.mark.parametrize("data", [("a: " + "x" * 65537).encode(),
                                  ("[" * 33 + "1" + "]" * 33).encode(),
                                  ("[" + ",".join("1" for _ in range(4096)) + "]").encode(),
                                  ("a: " + "1" * 257).encode()])
def test_yaml_source_budgets(data):
    with pytest.raises(reader.BoundedFormatError):
        reader.structure_preview(data, "yaml")


def test_xml_preserves_mixed_content_and_inert_attributes():
    result = reader.structure_preview(b'<?xml version="1.0" encoding="UTF-8"?><root unit="K">left<value>2.5</value>right</root>', "xml")
    assert result["tree"][0]["attributes"]["label"] == "root"
    assert [(node["node_type"], node["attributes"].get("value")) for node in result["tree"]] == [
        ("element", None), ("attribute", "K"), ("text", "left"), ("element", None), ("text", "2.5"), ("text", "right")]
    assert result["sampled"] is False


@pytest.mark.parametrize("data", [
    b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>',
    b'<!DOCTYPE foo SYSTEM "https://private.example/dtd"><foo/>',
    b'<!DOCTYPE foo [<!ENTITY a "x">]><foo>&a;</foo>',
    b'<!DOCTYPE foo><foo/>', b'<?xml-stylesheet href="https://private.example/style"?><foo/>',
    b'<foo>&undefined;</foo>', b'<foo><bar></foo>', b'<foo/><bar/>',
    b'<?xml version="1.0" encoding="ISO-8859-1"?><foo/>',
    '<foo/>'.encode("utf-16"), ("<x>" * 33 + "</x>" * 33).encode(),
    ("<x>" + "x" * 65537 + "</x>").encode(),
    ("<x>" + "<a/>" * 4096 + "</x>").encode(),
])
def test_xml_entities_external_resources_encodings_and_budgets_blocked(data):
    with pytest.raises(reader.BoundedFormatError) as error:
        reader.structure_preview(data, "xml")
    assert "/etc/" not in str(error.value) and "https://" not in str(error.value)


@pytest.mark.parametrize("fmt", ["json", "xml", "yaml"])
def test_structure_options_and_global_size_fail_closed(fmt):
    with pytest.raises(reader.BoundedFormatError):
        reader.structure_preview(b"x", fmt, {"path": "/etc/passwd"})
    with pytest.raises(reader.BoundedFormatError):
        reader.structure_preview(b"x" * (reader.STRUCTURE_INPUT_BYTES + 1), fmt)


def test_zip_real_directory_empty_archive_and_no_extraction(monkeypatch):
    data = zip_bytes([("folder/", b""), ("folder/a.csv", b"x,y\n1,2"), ("nested.zip", b"not inspected")])
    def forbidden(*args, **kwargs):
        pytest.fail("directory listing must not access member contents")
    for method in ("open", "read", "extract", "extractall", "testzip"):
        monkeypatch.setattr(zipfile.ZipFile, method, forbidden)
    result = reader.archive_preview(data, "zip")
    assert result["table"]["rows"] == [["folder", "directory", 0, 0, False],
        ["folder/a.csv", "file", 7, 7, False], ["nested.zip", "file", 13, 13, True]]
    assert result["metadata"]["contents_verified"] is False
    assert result["metadata"]["members_scanned"] == 3
    empty = reader.archive_preview(zip_bytes([]), "zip")
    assert empty["table"]["total_rows"] == 0 and empty["table"]["rows"] == []


@pytest.mark.parametrize("factory,fmt", [(zip_bytes, "zip"), (tar_bytes, "tar")])
@pytest.mark.parametrize("name", ["../secret", "/secret", "x/../secret", "x/./secret", "C:/secret", "C:\\secret",
                                  "\\server\\share", "x//secret", "x/a.", "x/a ", "\x01secret", "\u202esecret"])
def test_archive_unsafe_paths_never_returned(factory, fmt, name):
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(factory([(name, b"x")]), fmt)


@pytest.mark.parametrize("factory,fmt", [(zip_bytes, "zip"), (tar_bytes, "tar")])
@pytest.mark.parametrize("names", [("a", "a"), ("a", "A"), ("é", "e\u0301"), ("a", "a/b"), ("a/b", "a")])
def test_archive_duplicates_unicode_and_file_directory_conflicts(factory, fmt, names):
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(factory([(name, b"x") for name in names]), fmt)


def test_archive_pagination_validates_hidden_members():
    entries = [(f"item-{i}.txt", b"x") for i in range(405)]
    for factory, fmt in ((zip_bytes, "zip"), (tar_bytes, "tar")):
        result = reader.archive_preview(factory(entries), fmt, {"row_offset": 200})
        assert result["table"]["total_rows"] == 405
        assert result["table"]["row_offset"] == 200
        assert len(result["table"]["rows"]) == 200 and result["table"]["rows"][0][0] == "item-200.txt"
        assert result["sampled"] is True
        with pytest.raises(reader.BoundedFormatError):
            reader.archive_preview(factory(entries + [("../unsafe-tail", b"x")]), fmt)


@pytest.mark.parametrize("options", [{"row_offset": -1}, {"row_offset": True}, {"row_offset": 4096}, {"row_offset": 1.2}, {"extract": "a"}, []])
def test_archive_option_rejection(options):
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(zip_bytes([]), "zip", options)


def test_zip_central_directory_count_preflight_precedes_allocations(monkeypatch):
    data = bytearray(zip_bytes([("a", b"x"), ("b", b"x")]))
    position = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, position + 8, 1, 1)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid central directory must not allocate ZipInfo objects")
    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(bytes(data), "zip")


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFDIR])
def test_zip_special_or_mismatched_file_modes(mode):
    info = zipfile.ZipInfo("unsafe")
    info.create_system = 3
    info.external_attr = (mode | 0o755) << 16
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(zip_bytes([(info, b"x")]), "zip")


def test_zip_ratio_and_declared_size_budgets():
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(zip_bytes([("bomb", b"0" * 2_000_000)], compression=zipfile.ZIP_DEFLATED), "zip")
    data = bytearray(zip_bytes([("a", b"x")]))
    position = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, position + 24, reader.MAX_DECLARED_BYTES + 1)
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(bytes(data), "zip")


def test_zip_more_than_max_members_rejected_before_listing():
    data = zip_bytes([(f"a{i}", b"") for i in range(reader.MAX_MEMBERS + 1)])
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(data, "zip")


@pytest.mark.parametrize("mutation", ["encrypted", "wrong-name", "truncated", "prefix", "trailer", "multidisk", "zip64"])
def test_zip_unsupported_and_inconsistent_directory(mutation):
    data = bytearray(zip_bytes([("a", b"x")]))
    central = data.index(b"PK\x01\x02")
    eocd = data.index(b"PK\x05\x06")
    if mutation == "encrypted":
        struct.pack_into("<H", data, 6, 1)
        struct.pack_into("<H", data, central + 8, 1)
    elif mutation == "wrong-name":
        data[30] = ord("b")
    elif mutation == "truncated":
        data = data[:-1]
    elif mutation == "prefix":
        data = b"MZ" + data
    elif mutation == "trailer":
        data += b"junk"
    elif mutation == "multidisk":
        struct.pack_into("<H", data, eocd + 4, 1)
    elif mutation == "zip64":
        struct.pack_into("<I", data, central + 24, 0xFFFFFFFF)
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(bytes(data), "zip")


@pytest.mark.parametrize("format", [tarfile.USTAR_FORMAT, tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_real_tar_directory_never_extracts_members(monkeypatch, format):
    name = "data/" + ("long-" * 30 if format != tarfile.USTAR_FORMAT else "") + "signal.csv"
    data = tar_bytes([(name, b"x,y\n1,2"), ("nested.tar.gz", b"not inspected")], format=format)
    def forbidden(*args, **kwargs):
        pytest.fail("listing must not use TarFile extraction or data access")
    monkeypatch.setattr(tarfile, "open", forbidden)
    result = reader.archive_preview(data, "tar")
    assert result["table"]["rows"] == [[name, "file", 7, None, False], ["nested.tar.gz", "file", 13, None, True]]
    assert result["metadata"]["declared_uncompressed_bytes"] == 20


@pytest.mark.parametrize("type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE, tarfile.XGLTYPE])
def test_tar_links_sparse_devices_and_global_headers_blocked(type):
    info = tarfile.TarInfo("unsafe")
    info.type = type
    if type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        info.linkname = "other"
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(tar_bytes([(info, b"")]), "tar")


@pytest.mark.parametrize("pax", [{"path": "../secret"}, {"GNU.sparse.map": "0,100"}, {"linkpath": "target"},
                              {"size": "-1"}, {"size": "9999999999999999"}, {"SCHILY.xattr.user.note": "x"}])
def test_tar_pax_overrides_are_validated(pax):
    info = tarfile.TarInfo("safe")
    info.pax_headers = pax
    data = tar_bytes([(info, b"x")], format=tarfile.PAX_FORMAT)
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(data, "tar")


def test_tar_missing_end_or_hidden_tail_fails_closed():
    data = tar_bytes([("a", b"x")])
    for invalid in (data[:1024], data + b"hidden archive", data[:600], b"x" * 2048):
        with pytest.raises(reader.BoundedFormatError):
            reader.archive_preview(invalid, "tar")


def test_tar_member_limit_is_enforced_during_scan(monkeypatch):
    monkeypatch.setattr(reader, "MAX_MEMBERS", 2)
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(tar_bytes([("a", b""), ("b", b""), ("c", b"")]), "tar")


def test_gzip_metadata_never_decompresses_or_trusts_trailer(monkeypatch):
    data = gzip.compress(b"a" * 2_000_000)
    multi = data + gzip.compress(b"second stream")
    def forbidden(*args, **kwargs):
        pytest.fail("GZIP metadata preview must never decompress")
    monkeypatch.setattr(gzip, "decompress", forbidden)
    monkeypatch.setattr(gzip, "GzipFile", forbidden)
    monkeypatch.setattr(zlib, "decompress", forbidden)
    monkeypatch.setattr(zlib, "decompressobj", forbidden)
    for fmt in ("gz", "gzip", "tgz", "tar.gz"):
        result = reader.archive_preview(multi, fmt)
        assert result["metadata"]["stream_count"] is None
        assert result["metadata"]["declared_uncompressed_bytes"] is None
        assert result["metadata"]["compression_ratio"] is None
        assert result["metadata"]["contents_verified"] is False
        assert result["table"]["rows"][0][2] is None
        assert "不解压" in "".join(result["warnings"])


def test_gzip_original_filename_is_inert_and_validated():
    def fixture(name):
        output = io.BytesIO()
        with gzip.GzipFile(filename=name, fileobj=output, mode="wb") as stream:
            stream.write(b"x")
        return output.getvalue()
    result = reader.archive_preview(fixture("data.tar"), "gz")
    assert result["table"]["rows"][0][0] == "data.tar" and result["table"]["rows"][0][-1] is True
    # GzipFile strips forward directories, so construct an untrusted header directly.
    original = fixture("data.tar")
    for name in (b"../unsafe", b"C:\\unsafe"):
        hostile = original[:10] + name + b"\x00" + original[19:]
        with pytest.raises(reader.BoundedFormatError):
            reader.archive_preview(hostile, "gz")


@pytest.mark.parametrize("data", [b"", b"not gzip", b"\x1f\x8b\x08\xe0" + b"\x00" * 30,
                                  b"\x1f\x8b\x08\x08" + b"\x00" * 6 + b"n" * 9000 + b"\x00" * 8,
                                  b"\x1f\x8b\x08\x04" + b"\x00" * 6 + b"\xff\xff" + b"\x00" * 20])
def test_gzip_invalid_or_oversized_headers(data):
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(data, "gz")


@pytest.mark.parametrize("fmt", ["rar", "7z", "bz2", "xz", "../zip"])
def test_unimplemented_archive_formats_not_claimed(fmt):
    with pytest.raises(reader.BoundedFormatError):
        reader.archive_preview(b"x", fmt)


def test_archive_output_is_json_serializable_and_input_immutable():
    data = zip_bytes([("data.csv", b"x")])
    original = bytes(data)
    result = reader.archive_preview(data, "zip")
    assert data == original
    json.dumps(result, allow_nan=False)


def test_archive_total_declared_budget_is_distinct_from_member_budget(monkeypatch):
    monkeypatch.setattr(reader, "MAX_DECLARED_BYTES", 10)
    for factory, fmt in ((zip_bytes, "zip"), (tar_bytes, "tar")):
        with pytest.raises(reader.BoundedFormatError):
            reader.archive_preview(factory([("a", b"123456"), ("b", b"123456")]), fmt)


def test_empty_tar_and_directory_members():
    assert reader.archive_preview(tar_bytes([]), "tar")["table"]["rows"] == []
    info = tarfile.TarInfo("folder/")
    info.type = tarfile.DIRTYPE
    result = reader.archive_preview(tar_bytes([(info, b""), ("folder/a", b"x")]), "tar")
    assert result["table"]["rows"][0] == ["folder", "directory", 0, None, False]


def test_long_key_is_visibly_truncated_not_used_as_node_path():
    result = reader.structure_preview(json.dumps({"k" * 600: 1}).encode(), "json")
    assert result["sampled"] is True
    assert result["tree"][1]["path"] == "/0/0"
    assert len(result["tree"][1]["attributes"]["label"]) == 512
