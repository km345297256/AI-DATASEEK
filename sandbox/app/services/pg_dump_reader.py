"""Bounded PostgreSQL 1.14--1.16 TOC reader; no SQL, decompression or engine.

Archive field order is based on PostgreSQL REL_18_6 pg_backup_archiver.c,
pg_backup_custom.c and pg_backup_tar.c. Only the directory is interpreted.
An accepted directory is deliberately NOT a statement of data integrity.
"""
from __future__ import annotations

import hashlib
import re

from .pg_dump_payload import (
    FORMATS, LIMITS, MAX_INPUT, OBJECT_TYPES, PUBLIC_NAME_TYPES, WARNING, display_label, need,
    validate_pg_dump_options, validate_pg_dump_payload,
)

_MEMBER = re.compile(rb"(?:toc\.dat|restore\.sql|[1-9][0-9]*\.dat|blob_[1-9][0-9]*\.dat|blobs(?:_[1-9][0-9]*)?\.toc)\Z")
_UNSIGNED = re.compile(rb"(?:0|[1-9][0-9]{0,9})\Z")
_ENCODING = re.compile(rb"SET client_encoding = 'UTF8';\n?\Z")


class _Cursor:
    def __init__(self, data):
        self.data = memoryview(data)
        self.pos = 0
        self.integer_size = 4
        self.offset_size = 8

    def take(self, size):
        need(type(size) is int and 0 <= size <= len(self.data) - self.pos)
        need(self.pos + size <= LIMITS["max_toc_bytes"])
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def byte(self):
        return self.take(1)[0]

    def integer(self):
        sign = self.byte()
        need(sign in (0, 1))
        value = int.from_bytes(self.take(self.integer_size), "little")
        need(value <= 2**31 - 1)
        return -value if sign else value

    def string(self, *, keep=False, required=False, maximum=None):
        size = self.integer()
        if size == -1:
            need(not required)
            return None
        need(0 <= size <= (LIMITS["max_field_bytes"] if maximum is None else maximum))
        value = self.take(size)
        return value.tobytes() if keep else None


def _octal(field):
    need(field and not field[0] & 128)
    raw = field.strip(b" \0")
    need(bool(raw) and all(48 <= byte <= 55 for byte in raw))
    return int(raw, 8)


def _tar_toc(data):
    """Inspect regular PG tar headers without extraction or tarfile helpers."""
    need(len(data) % 512 == 0 and len(data) >= 1536)
    pos = 0
    seen = set()
    toc = None
    terminated = False
    while pos < len(data):
        header = data[pos:pos + 512]
        need(len(header) == 512)
        if not any(header):
            need(pos + 1024 <= len(data) and not any(data[pos:]))
            terminated = True
            break
        need(len(seen) < LIMITS["max_tar_members"])
        checksum = _octal(header[148:156])
        need(checksum == sum(header[:148]) + 8 * 32 + sum(header[156:]))
        need(header[156:157] in (b"0", b"\0"))
        need(header[257:263] == b"ustar\0" and header[263:265] == b"00")
        need(not any(header[157:257]) and not any(header[345:500]))
        # PG emits flat ASCII member names; no PAX/GNU/prefix/sparse aliases.
        name_field = header[:100]
        end = name_field.find(b"\0")
        name = name_field if end < 0 else name_field[:end]
        need(end < 0 or not any(name_field[end:]))
        need(_MEMBER.fullmatch(name) is not None and name not in seen)
        seen.add(name)
        size = _octal(header[124:136])
        start = pos + 512
        finish = start + size
        padded = start + ((size + 511) // 512) * 512
        need(finish <= len(data) and padded <= len(data))
        need(not any(data[finish:padded]))
        if name == b"toc.dat":
            need(size <= LIMITS["max_toc_bytes"])
            toc = data[start:finish]
        pos = padded
    need(terminated and toc is not None)
    return toc


def _oid(cursor):
    value = cursor.string(keep=True, required=True, maximum=10)
    need(_UNSIGNED.fullmatch(value) is not None and int(value) <= 2**32 - 1)


def _text(raw):
    if raw is None:
        return None
    # The archive ENCODING entry is checked before this strict decoding.
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeError:
        need(False)


def _directory(data, archive_format):
    cursor = _Cursor(data)
    need(cursor.take(5) == b"PGDMP")
    version = tuple(cursor.byte() for _ in range(3))
    need(version in {(1, 14, 0), (1, 15, 0), (1, 16, 0)})
    cursor.integer_size = cursor.byte()
    cursor.offset_size = cursor.byte()
    need(cursor.integer_size == 4 and cursor.offset_size in (4, 8))
    need(cursor.byte() == archive_format)
    compression = cursor.byte() if version >= (1, 15, 0) else cursor.integer()
    need(compression in (0, 1, 2, 3) if version >= (1, 15, 0) else -1 <= compression <= 9)
    need(archive_format != 3 or compression == 0)
    # Do not render timestamps, source DB name or source version strings.
    for _ in range(7):
        cursor.integer()
    cursor.string()  # database name
    cursor.string()  # server version
    cursor.string()  # pg_dump version
    count = cursor.integer()
    need(1 <= count <= LIMITS["max_objects"])
    entries = []
    ids = set()
    offsets = []
    dependencies_total = 0
    encodings = []
    for _ in range(count):
        dump_id = cursor.integer()
        need(1 <= dump_id <= 2**31 - 1 and dump_id not in ids)
        ids.add(dump_id)
        had_dumper = cursor.integer()
        need(had_dumper in (0, 1))
        _oid(cursor)
        _oid(cursor)
        tag = cursor.string(keep=True, required=True)
        desc = cursor.string(keep=True, required=True, maximum=64)
        need(cursor.integer() in (1, 2, 3, 4))
        if desc == b"ENCODING":
            encodings.append(cursor.string(keep=True, required=True, maximum=64))
        else:
            cursor.string()  # SQL definition is never parsed or returned.
        cursor.string()  # DROP SQL
        cursor.string()  # COPY SQL
        namespace = cursor.string(keep=True)
        cursor.string()  # tablespace
        cursor.string()  # table access method
        if version >= (1, 16, 0):
            need(0 <= cursor.integer() <= 255)
        cursor.string()  # owner
        with_oids = cursor.string(keep=True, required=True, maximum=5)
        need(with_oids in (b"true", b"false"))
        deps = set()
        while True:
            dep = cursor.string(keep=True, maximum=10)
            if dep is None:
                break
            need(_UNSIGNED.fullmatch(dep) is not None)
            number = int(dep)
            need(1 <= number <= 2**31 - 1 and number not in deps and number != dump_id)
            deps.add(number)
            dependencies_total += 1
            need(len(deps) <= LIMITS["max_dependencies_per_object"])
            need(dependencies_total <= LIMITS["max_dependencies_total"])
        if archive_format == 1:
            flag = cursor.byte()
            offset = int.from_bytes(cursor.take(cursor.offset_size), "little")
            need(flag in (1, 2, 3))
            need(flag == 2 and had_dumper == 1 and offset > 0 or flag in (1, 3) and offset == 0)
            need(had_dumper == 1 or flag == 3)
            if flag == 2:
                offsets.append(offset)
        else:
            filename = cursor.string(keep=True, required=True, maximum=100)
            need(not filename or _MEMBER.fullmatch(filename) is not None and filename not in {b"toc.dat", b"restore.sql"})
            need(had_dumper == 1 or not filename)
        entries.append((dump_id, desc, namespace, tag))
    need(len(encodings) == 1 and _ENCODING.fullmatch(encodings[0]) is not None)
    need(all(cursor.pos <= offset < len(data) for offset in offsets))
    need(len(set(offsets)) == len(offsets))
    if archive_format == 3:
        need(cursor.pos == len(data))
    return version, entries


def pg_dump_preview(data, fmt="dump", kind="tree", options=None):
    """Read only directory metadata from one already-authorized byte snapshot."""
    validate_pg_dump_options(kind, {} if options is None else options)
    need(type(data) is bytes and 64 <= len(data) <= MAX_INPUT)
    need(type(fmt) is str and fmt in FORMATS)
    is_custom = data.startswith(b"PGDMP")
    source = data if is_custom else _tar_toc(data)
    version, entries = _directory(source, 1 if is_custom else 3)
    digest = hashlib.sha256(data).digest()
    tree = []
    for dump_id, desc, schema, name in entries:
        object_type = _text(desc)
        object_type = object_type if object_type in OBJECT_TYPES else "OTHER"
        public_name = object_type in PUBLIC_NAME_TYPES
        tree.append({
            "path": "/o-" + hashlib.sha256(digest + str(dump_id).encode("ascii")).hexdigest()[:24],
            "node_type": "group",
            "attributes": {
                "object_type": object_type,
                "schema": display_label(_text(schema), schema=True) if public_name else "无命名空间",
                "name": display_label(_text(name)) if public_name else "名称已隐藏",
            },
        })
    result = {
        "type": "pg-dump", "reader": "pg-dump", "kind": "tree",
        "contract_version": 2, "media_type": "application/json",
        "tree": tree, "metadata": {
            "engine": "pg-dump", "format": fmt,
            "container": "PostgreSQL custom archive" if is_custom else "PostgreSQL tar archive",
            "archive_version": ".".join(map(str, version)),
            "source_bytes": len(data), "input_mode": "whole",
            "objects_returned": len(tree), "objects_total": len(tree),
            "data_verified": False, "limits": dict(LIMITS),
        },
        "warnings": [WARNING], "sampled": False, "choices": {}, "selected": {},
    }
    return validate_pg_dump_payload(result, kind=kind, options={}, fmt=fmt, size=len(data))
