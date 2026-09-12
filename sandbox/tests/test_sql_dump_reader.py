import json
import sqlite3
from pathlib import Path

import pytest
import sqlglot

from app.services import sql_dump_reader as reader
from app.services.sql_dump_payload import ERROR, SqlDumpError, table_id, validate_sql_dump_payload
from sql_dump_fixtures import options, sqlite_dump_bytes


def preview(sql, dialect="sqlite", *, page=None):
    data = sql.encode("utf8") if isinstance(sql, str) else sql
    return reader.sql_dump_preview(data, kind="table" if page else "tree", options=page or {"dialect": dialect})


def test_native_sqlite_iterdump_structure_exact_literals_paging_empty_and_no_execution(monkeypatch):
    data = sqlite_dump_bytes()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("must not open any database"))
    tree = preview(data)
    assert [t["label"] for t in tree["choices"]["tables"]] == ["empty", "measurements"]
    assert tree["metadata"]["source_rows"] == 3
    assert tree["metadata"]["ignored_statement_count"] == 2  # BEGIN/COMMIT, neither executed
    first = preview(data, page=options())
    cells = first["table"]["rows"][0]
    assert cells[0] == {"type": "number-literal", "value": "9223372036854775807"}
    assert cells[1] == {"type": "text", "value": "1.230000000000000001"}  # no declared-type coercion
    assert cells[2] == {"type": "number-literal", "value": "7.5"}
    assert cells[3] == {"type": "text", "value": "观测一"}
    assert cells[4] == {"type": "null", "value": None}
    assert cells[5] == {"type": "blob", "bytes": 2}
    assert first["table"]["rows"][1][4] == {"type": "text", "value": ""}
    assert first["table"]["row_ids"] == ["0", "1"] and first["table"]["has_more"] is True
    last = preview(data, page=options(2, columns=[3, 0]))
    assert last["table"]["columns"] == ["label", "id"]
    assert last["table"]["row_ids"] == ["2"] and last["table"]["has_more"] is False
    empty = preview(data, page=options(table="empty", columns=[0]))
    assert empty["table"]["rows"] == [] and empty["sampled"] is False
    assert validate_sql_dump_payload(first, fmt="sql", size=len(data), options=options()) == first


def test_numeric_literals_retain_scale_leading_zero_sign_exponent_not_float():
    sql = "CREATE TABLE t(n NUMERIC); INSERT INTO t VALUES (+0001.230000000000000001),(-99999999999999999999.123456789012345678),(.0001),(1.),(1e+400);"
    got = preview(sql, page=options(table="t", columns=[0], limit=10))
    assert [r[0]["value"] for r in got["table"]["rows"]] == ["+0001.230000000000000001", "-99999999999999999999.123456789012345678", ".0001", "1.", "1e+400"]
    assert all(r[0]["type"] == "number-literal" for r in got["table"]["rows"])


def test_copy_literal_text_null_empty_unicode_and_standard_backslash_escapes():
    sql = """-- Original minimal pg_dump-compatible COPY fragment
SET standard_conforming_strings = on;
CREATE TABLE public.measurements (id bigint, amount numeric(38,18), label text);
COPY public.measurements (label, amount, id) FROM stdin;
观测一\t1.230000000000000001\t9007199254740993
\t\\N\t2
tab\\tline\\nbackslash\\\\\\141\\x62\t3.00\t3
\\.
"""
    first = preview(sql, page=options(dialect="postgres", table="public.measurements", columns=[0, 1, 2]))
    assert first["table"]["rows"] == [
        [{"type": "text", "value": "9007199254740993"}, {"type": "text", "value": "1.230000000000000001"}, {"type": "text", "value": "观测一"}],
        [{"type": "text", "value": "2"}, {"type": "null", "value": None}, {"type": "text", "value": ""}],
    ]
    second = preview(sql, page=options(2, dialect="postgres", table="public.measurements", columns=[2]))
    assert second["table"]["rows"][0][0]["value"] == "tab\tline\nbackslash\\ab"
    assert first["metadata"]["source_rows"] == 3


def test_mysql_multirow_hex_blobs_and_simple_table_options():
    sql = """# original synthetic mysqldump-compatible fragment
/*!40101 SET NAMES utf8mb4 */;
CREATE TABLE `t` (`id` bigint NOT NULL AUTO_INCREMENT, `amount` decimal(38,18), `payload` blob,
PRIMARY KEY (`id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
LOCK TABLES `t` WRITE;
INSERT INTO `t` VALUES (9007199254740993,1.230000000000000001,0x0011),(2,NULL,X'');
UNLOCK TABLES;
"""
    page = preview(sql, page=options(dialect="mysql", table="t", columns=[0, 1, 2]))
    assert page["table"]["rows"][0][1] == {"type": "number-literal", "value": "1.230000000000000001"}
    assert page["table"]["rows"][0][2] == {"type": "blob", "bytes": 2}
    assert page["table"]["rows"][1][2] == {"type": "blob", "bytes": 0}
    assert page["metadata"]["ignored_statement_count"] == 3


def test_literal_boolean_empty_text_quote_semicolon_and_comment_are_not_commands():
    sql = "CREATE TABLE t(a TEXT,b BOOLEAN); /* INSERT INTO t VALUES('bad',false); */ INSERT INTO t VALUES ('quote''; -- stays text', TRUE),('',false);"
    rows = preview(sql, page=options(table="t", columns=[0, 1]))["table"]["rows"]
    assert rows[0] == [{"type": "text", "value": "quote'; -- stays text"}, {"type": "boolean", "value": True}]
    assert rows[1] == [{"type": "text", "value": ""}, {"type": "boolean", "value": False}]


def test_postgres_dollar_quoted_do_and_psql_control_are_inert_not_fake_rows():
    sql = """\\restrict generated_safe_token
CREATE TABLE t(a TEXT);
DO $body$ BEGIN INSERT INTO t VALUES('not top-level'); END; $body$;
INSERT INTO t VALUES($value$actual ' value; still text$value$);
\\unrestrict generated_safe_token
"""
    page = preview(sql, page=options(dialect="postgres", table="t", columns=[0]))
    assert page["metadata"]["source_rows"] == 1 and page["metadata"]["ignored_statement_count"] == 3
    assert page["table"]["rows"][0][0]["value"] == "actual ' value; still text"


@pytest.mark.parametrize("suffix", [
    "INSERT INTO t VALUES (1+2);", "INSERT INTO t VALUES (load_extension('unsafe'));", "INSERT INTO t SELECT 1;",
    "INSERT INTO t VALUES ((1));", "INSERT INTO t VALUES (CAST('1' AS INTEGER));", "INSERT INTO t VALUES (DEFAULT);",
    "INSERT INTO t VALUES (1::integer);", "INSERT OR REPLACE INTO t VALUES (1);", "INSERT IGNORE INTO t VALUES (1);",
    "INSERT INTO t VALUES (1) ON CONFLICT DO NOTHING;", "INSERT INTO t VALUES (1) RETURNING a;",
    "INSERT INTO missing VALUES (1);", "INSERT INTO t(a,a) VALUES (1,2);", "INSERT INTO t VALUES (1,2);",
    "INSERT INTO t VALUES (0x0011);", "INSERT INTO t VALUES (X'0');", "INSERT INTO t VALUES (X'gg');",
    "INSERT INTO t VALUES (1_000);", "INSERT INTO t VALUES (NaN);", "INSERT INTO t VALUES (Inf);",
    "INSERT INTO t VALUES (1e);", "INSERT INTO t VALUES ('a' 'b');", "INSERT INTO t VALUES (E'escape');",
    "COPY t FROM '/private/secret';", "COPY t FROM PROGRAM 'touch /private/marker';", "COPY t TO STDOUT;",
    "COPY t FROM STDIN WITH (FORMAT CSV);", "COPY t FROM STDIN;\n1\n", "COPY t FROM STDIN;\n1\n\\.",
    "DELIMITER $$;", "IF true BEGIN SELECT 1; INSERT INTO t VALUES (2); END;", "CREATE VIEW v AS SELECT 1;",
    "CREATE FUNCTION f() RETURNS INT AS 'SELECT 1' LANGUAGE SQL;", "CREATE TRIGGER x AFTER INSERT ON t BEGIN SELECT 1; END;",
])
def test_unsupported_syntax_rejects_without_false_complete_rows(suffix):
    dialect = "postgres" if suffix.startswith("COPY") else "sqlite"
    with pytest.raises(SqlDumpError, match=ERROR): preview("CREATE TABLE t(a INTEGER);" + suffix, dialect)


@pytest.mark.parametrize("declaration", [
    "CREATE TABLE t AS SELECT 1;", "CREATE TEMP TABLE t(a INTEGER);", "CREATE VIRTUAL TABLE t USING x;",
    "CREATE TABLE t(a INTEGER GENERATED ALWAYS AS (load_extension('x')));", "CREATE TABLE t(a INTEGER DEFAULT random());",
    "CREATE TABLE t(a INTEGER CHECK(a > 1));", "CREATE TABLE t(a INTEGER, a INTEGER);", "CREATE TABLE t();",
    "CREATE TABLE t(a custom_type);", "CREATE TABLE t(a INT[]);", 'CREATE TABLE "../unsafe"(a INTEGER);',
    'CREATE TABLE t("/Users/private" INTEGER);', "CREATE TABLE t(a INTEGER); CREATE TABLE t(a INTEGER);",
])
def test_unsupported_create_or_unsafe_catalog_is_rejected(declaration):
    with pytest.raises(SqlDumpError): preview(declaration)


@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
def test_ambiguous_session_dependent_backslash_strings_are_not_guessed(dialect):
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a TEXT); INSERT INTO t VALUES ('a\\n');", dialect)


def test_sqlite_backslash_is_literal_and_unsafe_text_is_redacted():
    sql = "CREATE TABLE t(a TEXT); INSERT INTO t VALUES ('ordinary\\n'),('/private/secret'),('" + "x" * 513 + "');"
    rows = preview(sql, page=options(table="t", columns=[0], limit=10))["table"]["rows"]
    assert rows[0][0]["value"] == "ordinary\\n"
    assert rows[1][0] == {"type": "text-omitted", "bytes": 15, "reason": "unsafe-text"}
    assert rows[2][0] == {"type": "text-omitted", "bytes": 513, "reason": "cell-budget"}
    assert "/private/secret" not in json.dumps(rows)


@pytest.mark.parametrize("data", [b"", b"\xff", b"\x00CREATE TABLE t(a INT);", b"CREATE TABLE t(a INT)",
    b"CREATE TABLE t(a TEXT); INSERT INTO t VALUES ('open);", b"/* unterminated", b"-- only comment\n", b";", b"SELECT 1;",
    b"CREATE TABLE t(a INT); INSERT INTO t VALUES (1" ])
def test_empty_truncated_invalid_encoding_and_no_table_fail(data):
    with pytest.raises(SqlDumpError): preview(data)


@pytest.mark.parametrize("bad", [None, {}, {"dialect": "auto"}, {"dialect": "postgres", "sql": "SELECT 1"}, {"dialect": True}])
def test_options_validated_before_sqlglot(monkeypatch, bad):
    monkeypatch.setattr(sqlglot, "parse_one", lambda *a, **k: pytest.fail("must reject options before parser"))
    with pytest.raises(SqlDumpError): reader.sql_dump_preview(b"CREATE TABLE t(a INT);", options=bad)


@pytest.mark.parametrize("changes", [{"columns": [True]}, {"columns": []}, {"columns": [0, 0]}, {"row_offset": -1},
    {"row_offset": 100001}, {"row_limit": 201}, {"table": "t"}, {"columns": list(range(17))}, {"path": "/tmp/t"}])
def test_bad_page_selection_rejected_before_parser(monkeypatch, changes):
    monkeypatch.setattr(sqlglot, "parse_one", lambda *a, **k: pytest.fail("must reject selection before parser"))
    with pytest.raises(SqlDumpError): reader.sql_dump_preview(b"CREATE TABLE t(a INT);", kind="table", options={**options(), **changes})


def test_missing_columns_and_unknown_selection_are_not_filled_as_null():
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT,b INT DEFAULT 9); INSERT INTO t(a) VALUES(1);")
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT);", page=options(table="missing", columns=[0]))
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT);", page=options(table="t", columns=[1]))


@pytest.mark.parametrize("setting,value,sql", [
    ("MAX_TOKENS", 1, "CREATE TABLE t(a INT);"), ("MAX_STATEMENT_TOKENS", 1, "CREATE TABLE t(a INT);"),
    ("MAX_SCHEMA_TOKENS", 1, "CREATE TABLE t(a INT);"), ("MAX_COPY_ROW_BYTES", 1, "CREATE TABLE t(a TEXT); COPY t FROM stdin;\nab\n\\.\n"),
])
def test_independent_lexer_and_copy_budgets(monkeypatch, setting, value, sql):
    monkeypatch.setattr(reader, setting, value)
    with pytest.raises(SqlDumpError): preview(sql, "postgres")


def test_size_depth_row_width_statement_and_number_budgets(monkeypatch):
    with pytest.raises(SqlDumpError): preview(bytes(16 * 1024**2 + 1))
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT); INSERT INTO t VALUES(" + "(" * 33 + "1" + ")" * 33 + ");")
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT);" + ";" * 4096)
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a TEXT); INSERT INTO t VALUES ('" + "x" * 1048576 + "');")
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT); INSERT INTO t VALUES (" + "1" * 129 + ");")
    monkeypatch.setitem(reader.LIMITS, "max_source_rows", 1)
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT); INSERT INTO t VALUES (1),(2);")


def test_deadline_and_dependency_version_fail_closed(monkeypatch):
    monkeypatch.setattr(sqlglot, "__version__", "other")
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT);")
    monkeypatch.undo()
    times = iter([0.0, 16.0])
    monkeypatch.setattr(reader.time, "monotonic", lambda: next(times, 16.0))
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT);")


def test_nested_comments_are_bounded_and_dialect_specific():
    sql = "/* outer /* nested INSERT INTO t VALUES(9); */ outer */ CREATE TABLE t(a INT);"
    assert preview(sql, "postgres")["metadata"]["source_rows"] == 0
    with pytest.raises(SqlDumpError): preview(sql, "mysql")
    with pytest.raises(SqlDumpError): preview("/*" * 33 + "*/" * 33 + "CREATE TABLE t(a INT);", "postgres")


def test_mysql_dash_comment_requires_whitespace_and_do_requires_quoted_body():
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT); INSERT INTO t VALUES (1--2\n);", "mysql")
    assert preview("CREATE TABLE t(a INT); INSERT INTO t VALUES (1-- comment\n);", "mysql")["metadata"]["source_rows"] == 1
    with pytest.raises(SqlDumpError): preview("CREATE TABLE t(a INT); DO BEGIN SELECT 1; INSERT INTO t VALUES(9); END;", "postgres")


def test_native_pg_dump_18_6_copy_matches_independent_server_oracle_without_type_conversion():
    fixtures = Path(__file__).parent / "fixtures/database-dumps"
    data = (fixtures / "synthetic-postgres.sql").read_bytes()
    oracle = json.loads((fixtures / "synthetic-postgres-rows.json").read_text())
    page = preview(data, page=options(dialect="postgres", table="public.measurements", columns=list(range(6)), limit=10))
    records = {r[0]["value"]: r for r in page["table"]["rows"]}
    assert page["metadata"]["source_rows"] == 3
    assert page["table"]["row_ids"] == ["0", "1", "2"] and page["table"]["has_more"] is False
    assert [r[0]["value"] for r in page["table"]["rows"]] == ["1", "9007199254740993", "3"]  # dump order, not oracle sorting
    for original in oracle:
        row = records[original["id"]]
        for i, field in enumerate(["id", "amount", "signal", "label", "active", "optional"]):
            value = original[field]
            if value is None: assert row[i] == {"type": "null", "value": None}
            else:
                expected = ("t" if value else "f") if type(value) is bool else str(value)
                assert row[i] == {"type": "text", "value": expected}
    empty = preview(data, page=options(dialect="postgres", table="public.empty", columns=[0]))
    assert empty["table"]["rows"] == [] and empty["table"]["has_more"] is False


def test_original_parser_error_context_is_never_returned_or_logged(monkeypatch, caplog):
    def fail(*a, **k): raise ValueError("/Users/private/secret.sql FROM PROGRAM do_bad_things")
    monkeypatch.setattr(sqlglot, "parse_one", fail)
    with pytest.raises(SqlDumpError) as error: preview("CREATE TABLE t(a INT);")
    assert str(error.value) == ERROR and "/Users/private" not in caplog.text
