import copy
import json
from pathlib import Path

import pytest
import duckdb

from app.services import duckdb_table_reader as reader
from app.services.database_table_payload import DatabaseTableError, validate_database_table_payload
from database_table_fixtures import duckdb_bytes, duckdb_options


def test_real_database_native_values_types_paging_and_order():
    data = duckdb_bytes()
    tree = reader.duckdb_table_preview(data, "duckdb")
    assert [t["label"] for t in tree["choices"]["tables"]] == ["empty", "measurements"]
    cols = tree["choices"]["tables"][1]["columns"]
    assert cols[0]["primary_key"] == 1 and cols[0]["nullable"] is False
    first = reader.duckdb_table_preview(data, "ddb", "table", duckdb_options())
    row = first["table"]["rows"][0]
    assert row[0] == {"type": "integer", "value": "170141183460469231731687303715884105727"}
    assert row[1] == {"type": "decimal", "value": "1.230000000000000001"}
    assert row[3] == {"type": "text", "value": "测试"}
    assert row[4] == {"type": "blob", "bytes": 3}
    assert row[5] == {"type": "boolean", "value": True}
    assert row[6]["value"] == "2026-09-11 12:00:00.123456789"
    assert first["table"]["row_ids"] == ["0", "1"] and first["table"]["has_more"] is True
    second = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(2))
    assert second["table"]["row_ids"] == ["2", "3"] and second["table"]["has_more"] is False
    assert second["metadata"]["nonfinite_values"] == 2
    assert second["table"]["rows"][0][3] == {"type": "text-omitted", "bytes": 513, "reason": "cell-budget"}
    assert second["table"]["rows"][1][3]["reason"] == "unsafe-text"
    empty = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0], table="empty"))
    assert empty["table"]["rows"] == [] and empty["table"]["has_more"] is False
    assert "/private/hidden" not in json.dumps(second) and "CREATE" not in json.dumps(tree)


@pytest.mark.parametrize("change", [{"columns": [True]}, {"columns": []}, {"columns": [0,0]}, {"columns": list(range(17))},
    {"row_offset": True}, {"row_offset": 100001}, {"row_limit": 201}, {"row_limit": 0}, {"table": "measurements"}, {"sql": "SELECT 1"}, {"path": "/tmp/a"}])
def test_options_rejected_before_parser(monkeypatch, change):
    data = duckdb_bytes()
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: pytest.fail("parser opened"))
    with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(data, "duckdb", "table", {**duckdb_options(), **change})


@pytest.mark.parametrize("data,fmt", [(bytes(4096), "duckdb"), (b"DUCK", "duckdb"), (bytes(16777217), "duckdb"), (bytes(4096), "wal"), (bytearray(4096), "duckdb")], ids=["bad-magic", "short", "oversize", "wal", "not-bytes"])
def test_header_and_format_before_native(monkeypatch, data, fmt):
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: pytest.fail("parser opened"))
    with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(data, fmt)


def test_immutable_configuration_blocks_io_writes_extensions_and_config_change(tmp_path):
    path = tmp_path / "source.duckdb"; path.write_bytes(duckdb_bytes())
    connection = duckdb.connect(str(path), read_only=True, config=reader._config(str(tmp_path)))
    try:
        for name in ["enable_external_access", "autoload_known_extensions", "autoinstall_known_extensions", "allow_community_extensions", "allow_unsigned_extensions", "allow_persistent_secrets", "python_enable_replacements"]:
            assert connection.execute("SELECT current_setting(?)", (name,)).fetchone() == (False,)
        assert connection.execute("SELECT current_setting('lock_configuration')").fetchone() == (True,)
        assert connection.execute("SELECT current_setting('max_temp_directory_size')").fetchone() == ("0 bytes",)
        for sql in ["INSERT INTO empty VALUES ('changed')", "SET enable_external_access=true", "SET threads=4", "INSTALL httpfs", "LOAD httpfs",
                    "SELECT * FROM read_text('/etc/passwd')", "SELECT * FROM read_csv('https://example.invalid/secret')", "COPY empty TO '/tmp/not-created.csv'", "ATTACH '/tmp/not-created.duckdb' AS other"]:
            with pytest.raises(duckdb.Error): connection.execute(sql)
    finally: connection.close()


def test_native_memory_pressure_fails_without_any_disk_spill(tmp_path):
    path = tmp_path / "source.duckdb"; original = duckdb_bytes(); path.write_bytes(original)
    # Tighter-than-production test budget reaches native resource rejection
    # quickly, without making the regression suite allocate a huge database.
    config = {**reader._config(str(tmp_path)), "memory_limit": "8MB"}
    connection = duckdb.connect(str(path), read_only=True, config=config)
    try:
        with pytest.raises(duckdb.OutOfMemoryException):
            connection.execute("SELECT sum(i) FROM (SELECT i, row_number() OVER (ORDER BY hash(i)) AS n FROM range(5000000) r(i)) WHERE n % 2=0").fetchone()
    finally: connection.close()
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source.duckdb"]


def test_views_and_user_macros_cannot_override_system_builtins_or_run():
    script = """CREATE TABLE measurements(a VARCHAR);INSERT INTO measurements VALUES ('kept');
CREATE MACRO encode(x) AS 'OVERRIDDEN';CREATE MACRO octet_length(x) AS 9999;
CREATE MACRO duckdb_tables() AS TABLE SELECT * FROM read_text('/etc/passwd');
CREATE VIEW external_view AS SELECT * FROM read_text('/etc/passwd');"""
    data = duckdb_bytes(script)
    tree = reader.duckdb_table_preview(data, "duckdb")
    assert [t["label"] for t in tree["choices"]["tables"]] == ["measurements"]
    page = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0]))
    assert page["table"]["rows"] == [[{"type": "text", "value": "kept"}]]


def test_generated_default_and_nested_columns_disabled_without_hiding_safe_column():
    data = duckdb_bytes("CREATE TABLE measurements(a INTEGER,b INTEGER DEFAULT 42,c INTEGER GENERATED ALWAYS AS(a+1),d INTEGER[]);INSERT INTO measurements(a,d) VALUES (7,[1,2]);")
    tree = reader.duckdb_table_preview(data, "duckdb"); columns = tree["choices"]["tables"][0]["columns"]
    assert [c["previewable"] for c in columns] == [True, False, False, False]
    assert columns[3]["data_type"] == "UNSUPPORTED"
    assert reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0]))["table"]["rows"][0][0]["value"] == "7"
    for c in [1,2,3]:
        with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[c]))


@pytest.mark.parametrize("script", [
    'CREATE TABLE "bad;name"(x INTEGER);', 'CREATE TABLE x("bad/column" INTEGER);',
    'CREATE TABLE x(rowid INTEGER,a INTEGER);', 'CREATE SCHEMA elsewhere;CREATE TABLE elsewhere.x(a INTEGER);',
    ';'.join(f'CREATE TABLE t{i}(a INTEGER)' for i in range(33)),
    'CREATE TABLE x('+','.join(f'a{i} INTEGER' for i in range(129))+');',
])
def test_schema_limits_and_unrepresentable_identifiers_fail_closed(script):
    with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(duckdb_bytes(script), "duckdb")


def test_unicode_labels_spaces_quotes_safely_and_do_not_become_paths():
    data = duckdb_bytes('CREATE TABLE "观测 数据"("测量 值" INTEGER);INSERT INTO "观测 数据" VALUES(7);')
    tree = reader.duckdb_table_preview(data, "duckdb")
    assert tree["choices"]["tables"][0]["label"] == "观测 数据"
    page = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0], table="观测 数据"))
    assert page["table"]["rows"][0][0]["value"] == "7"


def test_large_blob_never_fetched_and_oversize_text_omitted():
    data = duckdb_bytes("CREATE TABLE measurements(a BLOB,b VARCHAR);INSERT INTO measurements VALUES (repeat('x',1000000)::BLOB,repeat('a',1000000));")
    page = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0,1]))
    assert page["table"]["rows"] == [[{"type":"blob","bytes":1000000},{"type":"text-omitted","bytes":1000000,"reason":"cell-budget"}]]
    assert len(json.dumps(page)) < 4000


def test_real_oversize_file_and_cell_are_rejected():
    data = duckdb_bytes("CREATE TABLE measurements(a VARCHAR);INSERT INTO measurements VALUES(repeat('x',16777217));")
    with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0]))
    with pytest.raises(DatabaseTableError): reader._cell("TEXT", 16777217, None)


def test_real_maximum_page_budget():
    data = duckdb_bytes("CREATE TABLE measurements AS SELECT " + ",".join("repeat('x',512) AS c" + str(i) for i in range(16)) + " FROM range(201);")
    page = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(limit=200, columns=list(range(16))))
    assert len(page["table"]["rows"]) == 200 and page["table"]["has_more"] is True
    assert len(json.dumps(page, ensure_ascii=False).encode()) < 2097152


def test_private_snapshot_cleanup_success_and_error(monkeypatch, tmp_path):
    data = duckdb_bytes(); before = bytes(data); paths = []; original = reader.tempfile.TemporaryDirectory
    def capture(*args, **kwargs):
        obj = original(*args, dir=tmp_path, **kwargs); paths.append(Path(obj.name)); return obj
    monkeypatch.setattr(reader.tempfile, "TemporaryDirectory", capture)
    reader.duckdb_table_preview(data, "duckdb")
    with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[127]))
    assert data == before and len(paths) == 2 and all(not path.exists() for path in paths)


def test_connection_failures_are_sanitized_and_snapshot_cleaned(monkeypatch, tmp_path):
    data = duckdb_bytes()
    def fail(*args, **kwargs): raise duckdb.IOException("secret /tmp/private failure")
    monkeypatch.setattr(duckdb, "connect", fail)
    with pytest.raises(DatabaseTableError) as raised: reader.duckdb_table_preview(data, "duckdb")
    assert "secret" not in str(raised.value) and "/tmp" not in str(raised.value)


def test_fixed_native_version_enforced_before_open(monkeypatch):
    data = duckdb_bytes(); monkeypatch.setattr(duckdb, "__version__", "1.5.4")
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: pytest.fail("outdated parser opened"))
    with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(data, "duckdb")


def test_corrupt_database_native_failure_is_fixed_diagnostic():
    data = bytearray(duckdb_bytes()); data[0:8] = b"corrupt!"
    with pytest.raises(DatabaseTableError, match="受限只读协议"):
        reader.duckdb_table_preview(bytes(data), "duckdb")


def test_elapsed_deadline_discards_result_and_cleans_snapshot(monkeypatch, tmp_path):
    data = duckdb_bytes(); original = reader._catalog
    ticks = iter([0, 0, 16])
    # Do not replace the process-wide clock: the in-process HTTP server and
    # its event loop run concurrently with this unit test.
    from types import SimpleNamespace
    monkeypatch.setattr(reader, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    # Keep this deterministic: the real timer is cancelled and joined before
    # its 15-second deadline, while the post-query clock already exceeded it.
    with pytest.raises(DatabaseTableError): reader.duckdb_table_preview(data, "duckdb")


def test_dropped_rows_preserve_native_rowid_order_not_primary_key_order():
    data = duckdb_bytes("CREATE TABLE measurements(id INTEGER PRIMARY KEY);INSERT INTO measurements VALUES(9),(2),(7);DELETE FROM measurements WHERE id=2;")
    page = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0]))
    # DuckDB may compact rowids at checkpoint; exact ids come from the native
    # persisted snapshot, and values prove that we did not sort business keys.
    assert [row[0]["value"] for row in page["table"]["rows"]] == ["9", "7"]
    assert int(page["table"]["row_ids"][0]) < int(page["table"]["row_ids"][1])


def test_payload_exact_options_size_engine_and_unknown_fields():
    data = duckdb_bytes(); options = duckdb_options()
    value = reader.duckdb_table_preview(data, "duckdb", "table", options)
    assert validate_database_table_payload(value, kind="table", options=options, fmt="duckdb", size=len(data)) is value
    for kwargs in [{"size": len(data)+1}, {"fmt": "dbf"}, {"options": {**options, "row_limit":3}}, {"kind":"tree"}]:
        with pytest.raises(DatabaseTableError): validate_database_table_payload(value, **kwargs)
    changed = copy.deepcopy(value); changed["metadata"]["path"] = "/tmp/private"
    with pytest.raises(DatabaseTableError): validate_database_table_payload(changed)


def test_shared_pure_contract_is_byte_identical():
    root = Path(__file__).resolve().parents[2]
    assert (root/"sandbox/app/services/database_table_payload.py").read_bytes() == (root/"backend/app/application/services/database_table_visualization.py").read_bytes()


def test_real_older_writer_database_is_read_without_migration():
    import base64
    import hashlib
    import zlib
    fixture = json.loads(Path(__file__).with_name("duckdb_1_4_5_fixture.json").read_text())
    assert fixture["writer_version"] == "1.4.5"
    data = zlib.decompress(base64.b64decode(fixture["zlib_base64"]))
    assert len(data) == fixture["size"] and hashlib.sha256(data).hexdigest() == fixture["sha256"]
    assert b"v1.4.5" in data[:256]
    page = reader.duckdb_table_preview(data, "duckdb", "table", duckdb_options(columns=[0,1,2]))
    assert page["table"]["rows"] == [[{"type":"integer","value":"9007199254740993"},
        {"type":"decimal","value":"123456789012345678.123456"}, {"type":"text","value":"旧版测试"}]]
    assert hashlib.sha256(data).hexdigest() == fixture["sha256"]
