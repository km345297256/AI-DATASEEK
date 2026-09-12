"""Synthetic SQLite snapshots from Python's independent system SQLite engine."""
import sqlite3
import tempfile
from pathlib import Path


def sqlite_bytes(script=None, rows=None):
    with tempfile.TemporaryDirectory(prefix="sqlite-fixture-") as directory:
        path = Path(directory)/"fixture.sqlite"
        connection = sqlite3.connect(path)
        try:
            if script is not None: connection.executescript(script)
            else:
                connection.execute("CREATE TABLE measurements (id INTEGER PRIMARY KEY, count BIGINT, signal REAL, label TEXT, payload BLOB, optional)")
                values = rows if rows is not None else [
                    (-5, -(2**63), 1.25, "control", bytes([0,1,255]), None),
                    (0, 2**63-1, -2.5, "测试", b"", 3.5),
                    (9, 9007199254740993, 0.0, "A"*513, b"abc", "mixed"),
                    (25, 42, float("inf"), "/private/hidden/value", None, 7),
                    (100, 0, None, "<img src=x onerror=alert(1)>", None, None),
                ]
                connection.executemany("INSERT INTO measurements VALUES (?,?,?,?,?,?)", values)
                connection.execute("CREATE TABLE empty (name TEXT)")
                connection.execute("CREATE INDEX signal_index ON measurements(signal)")
            connection.commit()
        finally: connection.close()
        return path.read_bytes()


def sqlite_options(offset=0, limit=2, columns=None, label="measurements"):
    from app.services.sqlite_table_payload import table_id
    return {"table": table_id(label), "columns": [0,1,2,3,4,5] if columns is None else columns, "row_offset": offset, "row_limit": limit}


def sqlite_payloads():
    from app.services.sqlite_table_reader import sqlite_table_preview
    data = sqlite_bytes()
    result = {"tree": sqlite_table_preview(data,"sqlite"),
        "first": sqlite_table_preview(data,"sqlite","table",sqlite_options()),
        "second": sqlite_table_preview(data,"sqlite","table",sqlite_options(2)),
        "last": sqlite_table_preview(data,"sqlite","table",sqlite_options(4)),
        "empty": sqlite_table_preview(data,"sqlite","table",sqlite_options(0,2,[0],"empty")),
        "columns": sqlite_table_preview(data,"sqlite","table",sqlite_options(0,2,[1,3]))}
    return result
