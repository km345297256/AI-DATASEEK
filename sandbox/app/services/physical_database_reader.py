"""Two fixed offline programs, in the existing networkless worker only.

No shell, database open, raw-dump command, configuration file or dynamic plugin.
Native stdout/stderr, input, CPU time and the returned projection are bounded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time

from .physical_database_payload import MAX_INPUT, ERROR, need, exact, integer, display, options_for, build_payload

BIN = Path("/opt/dataseek-physical/bin")


def run_native(program, args, directory):
    need(program in {"ibd2sdi", "sst_dump"})
    process = subprocess.Popen([str(BIN / program), *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, cwd=directory,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + 12
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                need(time.monotonic() < deadline)
                for key, _ in selector.select(.05):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk: selector.unregister(key.fileobj); continue
                    target = output[key.data]
                    need(len(target) + len(chunk) <= (8 * 1024**2 if key.data == "stdout" else 8192))
                    target.extend(chunk)
        need(process.wait(timeout=max(.001, deadline-time.monotonic())) == 0)
        need(not output["stderr"])
        return bytes(output["stdout"])
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def unique_object(pairs):
    result = {}
    for k, v in pairs:
        need(k not in result)
        result[k] = v
    return result


def sdi_rows(raw):
    need(type(raw) is bytes and len(raw) <= 2 * 1024**2)
    # Bound nesting before the JSON decoder can recurse; escaped quotes handled.
    depth = 0
    quoted = escaped = False
    for c in raw:
        if escaped: escaped = False; continue
        if quoted and c == 92: escaped = True; continue
        if c == 34: quoted = not quoted
        elif not quoted:
            if c in (91, 123): depth += 1; need(depth <= 32)
            elif c in (93, 125): depth -= 1; need(depth >= 0)
    need(depth == 0 and not quoted)
    data = json.loads(raw, object_pairs_hook=unique_object, parse_constant=lambda _: need(False))
    need(type(data) is list and 2 <= len(data) <= 34)
    need(type(data[0]) is str and data[0] == "ibd2sdi")
    rows = []
    seen, record_ids = set(), set()
    for record in data[1:]:
        need(exact(record, "type id object") and type(record["object"]) is dict)
        need(integer(record["type"], 1, 2) and integer(record["id"], 1, 2**64-1))
        identity = (record["type"], record["id"])
        need(identity not in record_ids); record_ids.add(identity)
        obj = record["object"]
        need(integer(obj.get("mysqld_version_id"), 80000, 80046) and type(obj.get("sdi_version")) is int and obj["sdi_version"] == 80019)
        need(type(obj.get("dd_version")) is int and obj["dd_version"] == 80023)
        need(obj.get("dd_object_type") in {"Table", "Tablespace"})
        need(record["type"] == (1 if obj["dd_object_type"] == "Table" else 2))
        if obj["dd_object_type"] == "Tablespace": continue
        table = obj.get("dd_object")
        need(type(table) is dict and table.get("engine") == "InnoDB")
        original = table.get("name")
        need(type(original) is str and original not in seen)
        seen.add(original); need(len(seen) <= 32)
        name = display(original)
        rows.append(["table", "-", name, "InnoDB"])
        columns, indexes = table.get("columns"), table.get("indexes")
        need(type(columns) is list and 1 <= len(columns) <= 128 and type(indexes) is list and len(indexes) <= 128)
        for ordinal, column in enumerate(columns, 1):
            need(type(column) is dict and column.get("ordinal_position") == ordinal)
            need(type(column.get("is_nullable")) is bool and integer(column.get("hidden"), 1, 4))
            need(integer(column.get("type"), 1, 64))
            sql_type = column.get("column_type_utf8")
            # No ENUM values, expressions, paths or arbitrary DDL reach the UI.
            safe_type = (sql_type if type(sql_type) is str and len(sql_type) <= 64 and
                re.fullmatch(r"(?:tinyint|smallint|mediumint|int|bigint|float|double|decimal|char|varchar|binary|varbinary|date|datetime|timestamp|time|year|text|blob|json|geometry)(?:\([0-9]{1,5}(?:,[0-9]{1,5})?\))?(?: unsigned)?", sql_type)
                else "mysql_type_" + str(column["type"]))
            definition = f"type={safe_type};nullable={str(column['is_nullable']).lower()};ordinal={ordinal};hidden={column['hidden']}"
            rows.append(["column", name, display(column.get("name")), definition])
        for index in indexes:
            need(type(index) is dict and integer(index.get("type"), 1, 5))
            elements = index.get("elements")
            need(type(elements) is list and 1 <= len(elements) <= 128)
            offsets = []
            for element in elements:
                need(type(element) is dict and integer(element.get("column_opx"), 0, len(columns)-1))
                offsets.append(str(element["column_opx"]))
            rows.append(["index", name, display(index.get("name")), f"type={index['type']};columns={','.join(offsets)}"])
        need(len(rows) <= 1024)
    need(bool(rows))
    return rows


def physical_database_preview(data, reader, fmt, kind="tree", options=None):
    options_for(reader, kind, {} if options is None else options)
    need(type(data) is bytes and 48 <= len(data) <= MAX_INPUT)
    try:
        with tempfile.TemporaryDirectory(prefix="physical-preview-") as directory:
            if reader == "mysql-sdi":
                need(fmt == "ibd" and len(data) >= 5*16384 and len(data) % 16384 == 0)
                need(data[4:8] == bytes(4) and int.from_bytes(data[24:26], "big") == 8)
                flags = int.from_bytes(data[54:58], "big")
                need(flags == 0x4021)  # 16 KiB, persistent file-per-table SDI, no encryption/compression.
                path = Path(directory) / "input.ibd"
            else:
                need(fmt in {"sst", "ldb"})
                need(data[-8:] in {bytes.fromhex("57fb808b247547db"), bytes.fromhex("f7cff485b741e288")})
                path = Path(directory) / "input.sst"
            with path.open("xb") as f: f.write(data)
            path.chmod(0o400)
            if reader == "mysql-sdi":
                raw = run_native("ibd2sdi", ["--skip-pretty", "--strict-check=crc32", str(path)], directory)
                rows = sdi_rows(raw)
            else:
                legacy = data[-8:] == bytes.fromhex("57fb808b247547db")
                expected = None
                if legacy:
                    from .legacy_sst_guard import legacy_rows
                    expected = legacy_rows(data)
                raw = run_native("sst_dump", ["--file=" + str(path), "--command=scan", "--output_hex", "--verify_checksum", *([] if legacy else ["--show_properties"])], directory)
                rows = sst_rows(raw, legacy=legacy)
                if legacy: need(rows == expected)
            need(sorted(p.name for p in Path(directory).iterdir()) == [path.name])
            need(path.read_bytes() == data)
            return build_payload(reader, fmt, len(data), rows)
    except (OSError, ValueError, TypeError, OverflowError, MemoryError, RecursionError, subprocess.SubprocessError):
        raise ValueError(ERROR) from None


def sst_rows(raw, *, legacy=False):
    need(type(raw) is bytes and len(raw) <= 8 * 1024**2)
    lines = raw.decode("utf8").splitlines()
    need(3 <= len(lines) <= 8192 and lines[0].startswith("Process "))
    if legacy:
        need(lines[1:4] == ["Not able to read table properties", "Sst file format: block-based(old version)", "from [] to []"])
        lines = [lines[0], "Sst file format: block-based", "from [] to []", *lines[4:], "Table Properties:"]
    else:
        need(lines[1:3] == ["Sst file format: block-based", "from [] to []"])
    need(lines.count("Table Properties:") == 1)
    boundary = lines.index("Table Properties:")
    need(0 <= boundary - 3 <= 1024)
    rows = []
    types = {0: "deletion", 1: "value", 2: "merge", 7: "single-deletion"}
    for line in lines[3:boundary]:
        match = re.fullmatch(r"'([0-9A-Fa-f]*)' seq:(0|[1-9][0-9]{0,16}), type:([0127]) => ([0-9A-Fa-f]*)", line)
        need(match is not None)
        key, seq, typ, val = match.groups()
        need(len(key) % 2 == len(val) % 2 == 0 and int(seq) < 2**56)
        kb, vb = len(key)//2, len(val)//2
        key_missing, value_missing = kb > 128, vb > 512
        omission = "both" if key_missing and value_missing else "key" if key_missing else "value" if value_missing else "none"
        rows.append(["" if key_missing else key.lower(), str(kb), seq, types[int(typ)], "" if value_missing else val.lower(), str(vb), omission])
    if legacy: return rows  # All legacy blocks/metadata were independently checked.
    # Human-readable optional/user metadata is never forwarded. Every required
    # fixed property must occur exactly once, so embedded newlines cannot shadow it.
    def prop(name):
        values = [line[len(name)+2:] for line in lines[boundary+1:] if line.startswith("  " + name)]
        need(len(values) == 1)
        return values[0]
    for key, expected in (("# entries: ", len(rows)), ("# range deletions: ", 0),
            ("# deletions: ", sum(r[3] in {"deletion", "single-deletion"} for r in rows)),
            ("# merge operands: ", sum(r[3] == "merge" for r in rows))):
        need(prop(key) == str(expected))
    need(prop("comparator name: ") == "leveldb.BytewiseComparator")
    need(prop("SST file compression algo: ") in {"NoCompression", "Snappy", "Zlib", "BZip2", "LZ4", "LZ4HC", "ZSTD"})
    sequence = [line for line in lines[boundary+1:] if line.startswith("  # rocksdb.external_sst_file.global_seqno:")]
    need(not sequence or sequence == ["  # rocksdb.external_sst_file.global_seqno: 0x0000000000000000"])
    return rows
