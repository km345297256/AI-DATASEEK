import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services import access_table_reader as reader
from app.services.database_table_payload import DatabaseTableError, table_id

FIXTURES = Path(__file__).parent / "fixtures" / "database"


def data(fmt="mdb"):
    return (FIXTURES / ("synthetic-v2000.mdb" if fmt == "mdb" else "synthetic-v2010.accdb")).read_bytes()


def options(offset=0, limit=2, columns=None, table="Samples"):
    return {"table": table_id("access", table), "columns": list(range(8)) if columns is None else columns,
            "row_offset": offset, "row_limit": limit}


@pytest.fixture
def native():
    if not Path(reader.NATIVE_READER).is_file():
        if os.environ.get("AI_DATASEEK_REQUIRE_DATABASE_READERS") == "1": pytest.fail("required libmdb native helper is missing")
        pytest.skip("libmdb 1.0.1 native helper unavailable on this host")


@pytest.mark.parametrize("fmt", ["mdb", "accdb"])
def test_access_native_against_independent_jackcess_oracle(native, fmt):
    source = data(fmt); tree = reader.access_table_preview(source, fmt)
    assert [t["label"] for t in tree["choices"]["tables"]] == ["Empty", "Samples"]
    assert tree["metadata"]["rows_returned"] == 0
    first = reader.access_table_preview(source, fmt, "table", options())
    rows = first["table"]["rows"]
    assert rows[0] == [{"type": "integer", "value": "1"}, {"type": "text", "value": "科学数据"},
        {"type": "decimal", "value": "1234.5678"}, {"type": "decimal", "value": "123456789012345678901234.5678"},
        {"type": "boolean", "value": True}, {"type": "timestamp", "value": "2024-02-29T12:34:56"},
        {"type": "real", "value": 1.25}, {"type": "blob", "bytes": 4}]
    assert rows[1][1] == {"type": "text", "value": ""}
    assert rows[1][2] == {"type": "decimal", "value": "-0.0001"}
    assert rows[1][3] == {"type": "decimal", "value": "-9007199254740993.0001"}
    assert rows[1][4] == {"type": "boolean", "value": False}
    assert first["table"]["row_ids"] == ["0", "1"] and first["table"]["has_more"] is True
    last = reader.access_table_preview(source, fmt, "table", options(2))
    assert last["table"]["rows"][0][1] == {"type": "null", "value": None}
    assert last["table"]["rows"][1][2] == {"type": "decimal", "value": "922337203685477.5807"}
    assert last["table"]["rows"][1][1]["value"] == "<script>inert</script>"
    assert last["table"]["has_more"] is False
    assert reader.access_table_preview(source, fmt, "table", options(0, 2, [0], "Empty"))["table"]["rows"] == []


def test_access_memo_ole_disabled_but_scalar_projection_works(native):
    source = (FIXTURES / "synthetic-memo-ole-v2010.accdb").read_bytes()
    tree = reader.access_table_preview(source, "accdb")
    table = tree["choices"]["tables"][0]
    assert [c["previewable"] for c in table["columns"]] == [True, False, False]
    value = reader.access_table_preview(source, "accdb", "table", options(0, 2, [0], table["label"]))
    assert value["table"]["rows"] == [[{"type": "integer", "value": "1"}]]
    for index in [1, 2]:
        with pytest.raises(DatabaseTableError): reader.access_table_preview(source, "accdb", "table", options(0, 2, [index], table["label"]))


def test_access_linked_tables_are_never_opened_and_target_never_disclosed(native):
    source = (FIXTURES / "synthetic-linked-v2000.mdb").read_bytes()
    with pytest.raises(DatabaseTableError) as error: reader.access_table_preview(source, "mdb")
    assert "synthetic" not in str(error.value) and "/" not in str(error.value)


@pytest.mark.parametrize("change", [{"table": "Samples"}, {"columns": [True]}, {"columns": [0, 0]}, {"columns": []}, {"row_limit": 201}, {"row_offset": 100001}, {"sql": "SELECT 1"}, {"path": "/tmp/file"}])
def test_access_options_fail_before_native(monkeypatch, change):
    monkeypatch.setattr(reader, "_run", lambda *a: pytest.fail("native opened"))
    with pytest.raises(DatabaseTableError): reader.access_table_preview(data(), "mdb", "table", {**options(), **change})


@pytest.mark.parametrize("offset,value", [(0,b"evil"),(4,b"Standard JetXX"),(20,b"\x00"),(20,b"\x04")])
def test_access_signature_and_supported_version_preflight(monkeypatch, offset, value):
    source = bytearray(data()); source[offset:offset + len(value)] = value
    monkeypatch.setattr(reader, "_run", lambda *a: pytest.fail("native opened"))
    with pytest.raises(DatabaseTableError): reader.access_table_preview(bytes(source), "mdb")


def test_access_single_readonly_snapshot_no_sidecars_and_cleanup(native, monkeypatch, tmp_path):
    original = reader.tempfile.TemporaryDirectory; paths = []
    def temporary(*args, **kwargs):
        obj = original(*args, dir=tmp_path, **kwargs); paths.append(Path(obj.name)); return obj
    monkeypatch.setattr(reader.tempfile, "TemporaryDirectory", temporary)
    source = data(); before = bytes(source); reader.access_table_preview(source, "mdb", "table", options())
    with pytest.raises(DatabaseTableError): reader.access_table_preview(source, "mdb", "table", options(columns=[127]))
    assert before == source and len(paths) == 2 and all(not p.exists() for p in paths)


def test_native_helper_rejects_writable_path_and_untrusted_operation(native, tmp_path):
    snapshot = tmp_path / "snapshot.mdb"; snapshot.write_bytes(data())
    result = subprocess.run([reader.NATIVE_READER, str(snapshot), "catalog"], capture_output=True, timeout=15)
    assert result.returncode != 0 and not result.stdout
    snapshot.chmod(0o400)
    result = subprocess.run([reader.NATIVE_READER, str(snapshot), "query"], capture_output=True, timeout=15)
    assert result.returncode != 0 and not result.stdout


def test_access_native_error_is_generic_not_path_or_stderr(monkeypatch):
    monkeypatch.setattr(reader, "_run", lambda *a: (_ for _ in ()).throw(OSError("/private/user-secret")))
    with pytest.raises(DatabaseTableError) as error: reader.access_table_preview(data(), "mdb")
    assert "secret" not in str(error.value)


def test_access_text_omission_preserves_null_empty_nul_and_budget():
    assert reader._cell({"type": "null", "value": None}) == {"type": "null", "value": None}
    assert reader._cell({"type": "text", "value": ""}) == {"type": "text", "value": ""}
    assert reader._cell({"type": "text", "value": "a\0b"}) == {"type": "text-omitted", "bytes": 3, "reason": "unsafe-text"}
    assert reader._cell({"type": "text", "value": "x" * 513}) == {"type": "text-omitted", "bytes": 513, "reason": "cell-budget"}


def test_access_source_no_convenience_export_sql_blob_or_catalog_loading():
    source = (Path(reader.__file__).with_name("access_table_native.c")).read_text()
    for prohibited in ["mdb_read_catalog(mdb", "mdb_read_table_by_name(", "mdb_ole_read_full(", "mdb_sql_run_query(", "MDB_WRITABLE)", "while (mdb_fetch_row("]:
        assert prohibited not in source


@pytest.mark.parametrize("column", [b"L\x00a\x00b\x00e\x00l\x00", b"O\x00b\x00s\x00e\x00r\x00v\x00e\x00d\x00"])
def test_access_embedded_nul_in_column_metadata_rejected_before_truncation(native, column):
    source = data(); offset = source.find(column)
    assert offset > 0
    altered = bytearray(source); altered[offset + 4:offset + 6] = b"\0\0"
    with pytest.raises(DatabaseTableError): reader.access_table_preview(bytes(altered), "mdb")


@pytest.mark.parametrize("mutation", ["columns-zero", "columns-oversize", "variable-oversize"])
def test_access_nullmask_and_variable_allocation_preflight(native, mutation):
    source = bytearray(data())
    # First data page belonging to MSysObjects, the fixed directory table.
    page = next(p for p in range(4096, len(source), 4096) if source[p] == 1 and int.from_bytes(source[p+4:p+8], "little") == 2)
    offset = int.from_bytes(source[page+14:page+16], "little") & 0x1fff
    row = page + offset
    cols = int.from_bytes(source[row:row+2], "little")
    if mutation == "columns-zero": source[row:row+2] = b"\0\0"
    elif mutation == "columns-oversize": source[row:row+2] = b"\xff\xff"
    else:
        mask = (cols + 7) // 8
        source[page+4096-mask-2:page+4096-mask] = b"\xff\xff"
    with pytest.raises(DatabaseTableError): reader.access_table_preview(bytes(source), "mdb")


@pytest.mark.parametrize("program", [
    "import os; os.write(1, b'x' * (2 * 1024 * 1024 + 1))",
    "import os; os.write(2, b'/private/native-secret' * 1000)",
    "import os; os.write(1, b'{}'); os.write(2, b'/private/native-secret')",
    "import os; os.write(1, b'{}'); raise SystemExit(3)",
])
def test_access_subprocess_stream_budgets_and_stderr_fail_closed(monkeypatch, tmp_path, program):
    # Exercise real OS pipes, not a mock returning an already-buffered result.
    script = tmp_path / "synthetic-reader.py"; script.write_text(program)
    monkeypatch.setattr(reader, "NATIVE_READER", sys.executable)
    with pytest.raises(DatabaseTableError) as error: reader._run(script, [])
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_access_subprocess_deadline_kills_child_and_no_environment_inheritance(monkeypatch, tmp_path):
    script = tmp_path / "synthetic-reader.py"; script.write_text("import time; time.sleep(30)")
    monkeypatch.setattr(reader, "NATIVE_READER", sys.executable)
    clock = iter([0, 100])
    monkeypatch.setattr(reader.time, "monotonic", lambda: next(clock))
    original = reader.subprocess.Popen; processes = []
    def start(argv, **kwargs):
        assert "MDBPATH" not in kwargs["env"] and "HOME" not in kwargs["env"]
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["close_fds"] is True
        process = original(argv, **kwargs); processes.append(process); return process
    monkeypatch.setattr(reader.subprocess, "Popen", start)
    with pytest.raises(DatabaseTableError): reader._run(script, [])
    assert len(processes) == 1 and processes[0].poll() is not None
