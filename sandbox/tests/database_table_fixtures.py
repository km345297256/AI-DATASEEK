"""Synthetic public fixtures only; no user files or external sources."""
from pathlib import Path
import tempfile

from app.services.database_table_payload import table_id


def duckdb_options(offset=0, limit=2, columns=None, table="measurements"):
    return {"table": table_id("duckdb", table), "columns": list(range(8)) if columns is None else columns, "row_offset": offset, "row_limit": limit}


def duckdb_bytes(script=None):
    import duckdb
    if script is None:
        script = """
CREATE TABLE empty (label VARCHAR);
CREATE TABLE measurements (count HUGEINT PRIMARY KEY, amount DECIMAL(38,18), signal DOUBLE, label VARCHAR, payload BLOB, active BOOLEAN, observed TIMESTAMP_NS, optional INTEGER);
INSERT INTO measurements VALUES
 (170141183460469231731687303715884105727,1.230000000000000001,2.5,'测试',from_hex('00ff7f'),true,'2026-09-11 12:00:00.123456789',NULL),
 (-170141183460469231731687303715884105728,-99999999999999999999.123456789012345678,7,'next',''::BLOB,false,'2026-09-11 12:00:01.000000001',1),
 (3,0.000000000000000001,'inf'::DOUBLE,repeat('a',513),NULL,NULL,NULL,NULL),
 (4,0,'nan'::DOUBLE,'/private/hidden',NULL,true,NULL,2);
"""
    with tempfile.TemporaryDirectory(prefix="test-duckdb-writer-") as directory:
        path = Path(directory) / "synthetic.duckdb"
        connection = duckdb.connect(str(path))
        try: connection.execute(script)
        finally: connection.close()
        return path.read_bytes()


def database_payloads():
    from app.services.duckdb_table_reader import duckdb_table_preview
    data = duckdb_bytes("""
CREATE TABLE Empty(label VARCHAR);
CREATE TABLE Measurements(id HUGEINT, amount DECIMAL(38,18), signal DOUBLE, label VARCHAR, optional VARCHAR, payload BLOB, nested INTEGER[]);
INSERT INTO Measurements VALUES
 (170141183460469231731687303715884105727,1.230000000000000001,7.5,'观测一',NULL,from_hex('00ff'),[1,2]),
 (9007199254740993,-99999999999999999999.123456789012345678,9,'<img src=x onerror=alert(1)>','',NULL,[]),
 (3,0.000000000000000001,12,'观测三','完成',''::BLOB,NULL);
""")
    first = duckdb_options(columns=list(range(6)), table="Measurements")
    second = {**first, "row_offset": 2}
    empty = duckdb_options(columns=[0], table="Empty")
    columns = {**first, "columns": [0,2]}
    return {"tree": duckdb_table_preview(data, "duckdb"),
        **{key: duckdb_table_preview(data, "duckdb", "table", options)
           for key, options in (("first", first), ("second", second), ("empty", empty), ("columns", columns))}}
