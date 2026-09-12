"""TOC format tests and malicious-input guards; native oracles are separate."""
import copy
import hashlib
import json
import random
import tarfile

import pytest

from app.services.pg_dump_payload import LIMITS, MAX_INPUT, PGDumpError, WARNING
from app.services.pg_dump_reader import pg_dump_preview
from pg_dump_fixtures import archive, entries, tar_archive


@pytest.mark.parametrize("version", [(1, 14, 0), (1, 15, 0), (1, 16, 0)])
@pytest.mark.parametrize("fmt", ["pgdump", "dump", "backup", "tar"])
def test_supported_custom_versions_and_suffixes(version, fmt):
    data = archive(version=version)
    result = pg_dump_preview(data, fmt)
    assert result["metadata"]["archive_version"] == ".".join(map(str, version))
    assert result["metadata"]["format"] == fmt
    assert result["metadata"]["objects_total"] == 6
    assert result["tree"][3]["attributes"] == {"object_type": "TABLE", "schema": "science lab", "name": "测量 data"}
    assert result["warnings"] == [WARNING]
    assert result["metadata"]["data_verified"] is False
    assert result["sampled"] is False


@pytest.mark.parametrize("version", [(1, 14, 0), (1, 15, 0), (1, 16, 0)])
def test_regular_tar_directory_is_not_extracted(version):
    data = tar_archive(archive(version=version, archive_format=3), members=[("5.dat", b"opaque data"), ("restore.sql", b"\\! never-execute")])
    result = pg_dump_preview(data, "tar")
    assert result["metadata"]["container"] == "PostgreSQL tar archive"
    assert result["metadata"]["source_bytes"] == len(data)
    assert result["tree"][4]["attributes"]["object_type"] == "TABLE DATA"


@pytest.mark.parametrize("compression", [0, 1, 2, 3])
def test_custom_compression_flag_does_not_decompress_any_data(compression, monkeypatch):
    import zlib
    monkeypatch.setattr(zlib, "decompress", lambda *a, **kw: pytest.fail("must not decompress"))
    result = pg_dump_preview(archive(compression=compression, body=b"invalid compressed block"))
    assert result["metadata"]["data_verified"] is False


def test_only_directory_no_file_sql_process_or_network_access(monkeypatch):
    import builtins
    import socket
    import subprocess
    def denied(*args, **kwargs):
        pytest.fail("reader cannot access files/processes/network")
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    value = pg_dump_preview(archive())
    serialized = json.dumps(value, ensure_ascii=False)
    assert all(secret not in serialized for secret in ["owner with spaces", "private_database_name", "should-never-run", "CREATE TABLE", "set_config"])


@pytest.mark.parametrize("label", ["/Users/private.db", "https://secret", "x\\y", "<img src=x>", "x\x00y", "a\nb", "a\u202eb", "x" * 129, " a", "a "])
def test_unsafe_labels_hidden_without_interpreting_as_paths(label):
    source = entries()
    source[3].update(tag=label, schema=label)
    result = pg_dump_preview(archive(source))
    assert result["tree"][3]["attributes"] == {"object_type": "TABLE", "schema": "命名空间已隐藏", "name": "名称已隐藏"}


def test_unrecognized_object_type_is_explicit_other():
    source = entries()
    source[3]["desc"] = "FUTURE TYPE"
    assert pg_dump_preview(archive(source))["tree"][3]["attributes"] == {"object_type": "OTHER", "schema": "无命名空间", "name": "名称已隐藏"}


@pytest.mark.parametrize("object_type", ["DATABASE", "DATABASE PROPERTIES", "COMMENT", "ACL", "DEFAULT ACL", "USER MAPPING", "SUBSCRIPTION", "FOREIGN SERVER", "FOREIGN DATA WRAPPER", "SECURITY LABEL", "OTHER"])
def test_sensitive_composite_tags_never_reveal_database_owner_or_server(object_type):
    source = entries()
    source[3].update(desc=object_type, tag="private_database_name owner_remote server_remote", schema="private_database_name")
    result = pg_dump_preview(archive(source))
    serialized = json.dumps(result, ensure_ascii=False)
    assert all(secret not in serialized for secret in ["private_database_name", "owner_remote", "server_remote"])
    assert result["tree"][3]["attributes"]["name"] == "名称已隐藏"


@pytest.mark.parametrize("value", ["SET client_encoding = 'LATIN1';\n", "SET client_encoding = 'SQL_ASCII';\n", "SET client_encoding = 'UTF8';\nSELECT 1;", "UTF8", ""])
def test_unsupported_or_ambiguous_encoding_rejected(value):
    with pytest.raises(PGDumpError):
        pg_dump_preview(archive(encoding=value))


def test_missing_duplicate_and_invalid_utf8_encoding_rejected():
    source = entries()
    source[0]["desc"] = "OTHER"
    with pytest.raises(PGDumpError): pg_dump_preview(archive(source))
    source = entries()
    source.append({**source[0], "id": 90})
    with pytest.raises(PGDumpError): pg_dump_preview(archive(source))
    source = entries()
    source[3]["tag"] = b"\xff"
    with pytest.raises(PGDumpError): pg_dump_preview(archive(source))


@pytest.mark.parametrize("field,value", [
    ("id", 1), ("id", 0), ("had_dumper", 2), ("tableoid", "4294967296"), ("oid", "00"),
    ("desc", "x" * 65), ("section", 0), ("section", 5), ("relkind", 256),
    ("with_oids", "other"), ("deps", [3, 3]), ("deps", [4]), ("deps", [0]),
    ("deps", list(range(100, 357))), ("offset_flag", 0), ("offset_flag", 2),
    ("offset", 9), ("sql", "x" * (1024**2 + 1)),
], ids=lambda value: str(value)[:60])
def test_invalid_toc_fields_fail_closed(field, value):
    source = entries()
    source[3][field] = value
    with pytest.raises(PGDumpError): pg_dump_preview(archive(source))


def test_valid_custom_offset_must_target_after_toc_and_within_snapshot():
    source = entries()
    end = len(archive(source))
    source[4].update(offset_flag=2, offset=end)
    assert pg_dump_preview(archive(source, body=b"not verified"))["metadata"]["data_verified"] is False
    for offset in [0, end - 1, end + 20]:
        source[4]["offset"] = offset
        with pytest.raises(PGDumpError): pg_dump_preview(archive(source, body=b"body"))


@pytest.mark.parametrize("index,value", [(5, 2), (6, 13), (6, 17), (7, 1), (8, 8), (9, 16), (10, 5), (11, 4), (12, 2)])
def test_unknown_header_values_rejected(index, value):
    data = bytearray(archive())
    data[index] = value
    with pytest.raises(PGDumpError): pg_dump_preview(bytes(data))


def test_every_directory_truncation_fails_without_partial_tree():
    data = archive()
    for size in range(len(data)):
        with pytest.raises(PGDumpError): pg_dump_preview(data[:size])


def test_unknown_non_sql_non_pg_bytes_and_oversize_fail():
    for data in [b"CREATE TABLE x (id int);" * 8, b"REDIS0011" + b"x" * 80, b"PGDMP" + b"x" * 200, b"x" * (MAX_INPUT + 1), bytearray(archive())]:
        with pytest.raises(PGDumpError): pg_dump_preview(data)
    for count in [-1, 0, 1025]:
        with pytest.raises(PGDumpError): pg_dump_preview(archive(count=count))


@pytest.mark.parametrize("kind,options", [("table", {}), ("tree", {"sql": "SELECT 1"}), ("tree", {"path": "/tmp/x"}), ("tree", []), ("text", {}), (False, {})])
def test_options_cannot_select_paths_sql_or_other_modes(kind, options):
    with pytest.raises(PGDumpError): pg_dump_preview(archive(), kind=kind, options=options)


@pytest.mark.parametrize("fmt", ["db", "bson", "sql", "../dump", True])
def test_only_declared_extensions(fmt):
    with pytest.raises(PGDumpError): pg_dump_preview(archive(), fmt=fmt)


@pytest.mark.parametrize("name", ["../toc.dat", "/toc.dat", "a/toc.dat", "a\\toc.dat", "toc.dat "])
def test_tar_path_aliases_are_rejected(name):
    with pytest.raises(PGDumpError): pg_dump_preview(tar_archive(toc_name=name), "tar")


@pytest.mark.parametrize("typ", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE])
def test_tar_nonregular_members_rejected(typ):
    with pytest.raises(PGDumpError): pg_dump_preview(tar_archive(toc_type=typ), "tar")


def test_tar_duplicate_extension_missing_checksum_and_padding_guards():
    invalid = [
        tar_archive(members=[("toc.dat", b"duplicate")]),
        tar_archive(members=[("unrelated.bin", b"x")]),
        tar_archive(toc_name="a" * 120, tar_format=tarfile.PAX_FORMAT),
        tar_archive(archive(archive_format=1)),
        tar_archive(archive(archive_format=3, compression=1)),
        tar_archive(archive(archive_format=3) + b"trailing"),
        tar_archive()[:-1],
    ]
    checksum = bytearray(tar_archive()); checksum[148] ^= 1; invalid.append(bytes(checksum))
    trailing = bytearray(tar_archive()); trailing[-1] = 1; invalid.append(bytes(trailing))
    for data in invalid:
        with pytest.raises(PGDumpError): pg_dump_preview(data, "tar")


def test_tar_internal_member_reference_cannot_escape():
    source = entries()
    source[4]["filename"] = "../../outside"
    with pytest.raises(PGDumpError): pg_dump_preview(tar_archive(archive(source, archive_format=3)), "tar")


def test_input_is_immutable_and_ids_bound_to_entire_snapshot():
    data = archive()
    before = hashlib.sha256(data).hexdigest()
    a = pg_dump_preview(data)
    assert pg_dump_preview(data) == a
    assert hashlib.sha256(data).hexdigest() == before
    b = pg_dump_preview(data + b"different data segment")
    assert a["tree"][0]["path"] != b["tree"][0]["path"]


def test_random_mutations_return_only_valid_payload_or_fixed_error():
    source = archive()
    rng = random.Random(3014)
    for _ in range(250):
        data = bytearray(source)
        for _ in range(rng.randint(1, 4)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            result = pg_dump_preview(bytes(data))
            assert result["metadata"]["data_verified"] is False
        except PGDumpError:
            pass


def test_full_object_budget_supported_but_not_silently_truncated():
    source = entries()
    source += [{"id": number, "tag": "table" + str(number), "desc": "TABLE", "schema": "public"} for number in range(10, 10 + 1024 - len(source))]
    result = pg_dump_preview(archive(source))
    assert len(result["tree"]) == result["metadata"]["objects_total"] == 1024
    assert result["sampled"] is False
    source.append({"id": 9000, "tag": "too_many", "desc": "TABLE"})
    with pytest.raises(PGDumpError): pg_dump_preview(archive(source))


def test_total_toc_and_dependency_budgets_are_independent():
    source = entries()
    source += [{"id": number, "tag": "table", "desc": "TABLE", "sql": "x" * (1024**2)} for number in range(10, 19)]
    raw = archive(source)
    assert len(raw) < MAX_INPUT
    with pytest.raises(PGDumpError): pg_dump_preview(raw)
    source = entries()
    source += [{"id": number, "tag": "table", "desc": "TABLE", "deps": list(range(10000, 10256))} for number in range(100, 165)]
    with pytest.raises(PGDumpError): pg_dump_preview(archive(source))


def test_tar_member_budget_and_no_tail_acceptance():
    # Each member is empty, so file size is small while member count is large.
    data = tar_archive(members=[(str(number) + ".dat", b"") for number in range(1, 4097)])
    assert len(data) < MAX_INPUT
    with pytest.raises(PGDumpError): pg_dump_preview(data, "tar")
    original = tar_archive()
    # A nonzero member after an EOF block must never be treated as a second tar.
    with pytest.raises(PGDumpError): pg_dump_preview(original + original, "tar")
