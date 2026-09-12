"""Small format-level fixtures for mutation tests, not native-writer oracles."""
import io
import tarfile


def archive_integer(value):
    return bytes([1 if value < 0 else 0]) + abs(value).to_bytes(4, "little")


def archive_string(value):
    if value is None:
        return archive_integer(-1)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return archive_integer(len(value)) + value


def entries():
    return [
        {"id": 1, "tag": "ENCODING", "desc": "ENCODING", "sql": "SET client_encoding = 'UTF8';\n"},
        {"id": 2, "tag": "STDSTRINGS", "desc": "STDSTRINGS", "sql": "SET standard_conforming_strings = 'on';\n"},
        {"id": 3, "tag": "SEARCHPATH", "desc": "SEARCHPATH", "sql": "SELECT pg_catalog.set_config('search_path', '', false);\n"},
        {"id": 4, "tag": "测量 data", "schema": "science lab", "desc": "TABLE", "sql": "CREATE TABLE x(id integer);", "section": 2},
        {"id": 5, "tag": "测量 data", "schema": "science lab", "desc": "TABLE DATA", "had_dumper": 1, "section": 3, "deps": [4]},
        {"id": 6, "tag": "f()", "schema": "science lab", "desc": "FUNCTION", "sql": "COPY x FROM PROGRAM 'should-never-run';", "section": 2},
    ]


def archive(items=None, *, version=(1, 16, 0), compression=None, archive_format=1,
            body=b"", count=None, dbname="private_database_name", encoding=None):
    items = entries() if items is None else items
    if encoding is not None:
        items[0]["sql"] = encoding
    compression = (1 if archive_format == 1 else 0) if compression is None else compression
    result = bytearray(b"PGDMP" + bytes(version) + bytes([4, 8, archive_format]))
    result += bytes([compression]) if version >= (1, 15, 0) else archive_integer(compression)
    result += b"".join(archive_integer(value) for value in (0, 0, 0, 11, 8, 126, -1))
    result += archive_string(dbname) + archive_string("18.6") + archive_string("18.6")
    result += archive_integer(len(items) if count is None else count)
    for item in items:
        had_dumper = item.get("had_dumper", 0)
        result += archive_integer(item["id"]) + archive_integer(had_dumper)
        result += archive_string(item.get("tableoid", "0")) + archive_string(item.get("oid", "0"))
        result += archive_string(item.get("tag", "object")) + archive_string(item.get("desc", "TABLE"))
        result += archive_integer(item.get("section", 1))
        result += archive_string(item.get("sql", "")) + archive_string(item.get("drop", "")) + archive_string(item.get("copy", ""))
        result += archive_string(item.get("schema")) + archive_string(item.get("tablespace")) + archive_string(item.get("tableam"))
        if version >= (1, 16, 0):
            result += archive_integer(item.get("relkind", 0))
        result += archive_string(item.get("owner", "owner with spaces")) + archive_string(item.get("with_oids", "false"))
        result += b"".join(archive_string(str(dep)) for dep in item.get("deps", [])) + archive_string(None)
        if archive_format == 1:
            result += bytes([item.get("offset_flag", 1 if had_dumper else 3)])
            result += item.get("offset", 0).to_bytes(8, "little")
        else:
            result += archive_string(item.get("filename", str(item["id"]) + ".dat" if had_dumper else ""))
    return bytes(result) + body


def tar_archive(toc=None, *, members=None, toc_name="toc.dat", toc_type=tarfile.REGTYPE,
                tar_format=tarfile.USTAR_FORMAT):
    out = io.BytesIO()
    toc = archive(archive_format=3) if toc is None else toc
    with tarfile.open(fileobj=out, mode="w", format=tar_format) as target:
        main = tarfile.TarInfo(toc_name)
        main.size = len(toc)
        main.type = toc_type
        if toc_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            main.linkname = "outside"
        target.addfile(main, io.BytesIO(toc))
        for name, data in (members or []):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    return out.getvalue()
