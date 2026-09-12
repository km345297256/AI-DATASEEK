"""Original synthetic dump fragments; no downloaded/private database data."""
import sqlite3

from app.services.sql_dump_payload import table_id


def sqlite_dump_bytes():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE empty (note TEXT)")
        connection.execute("CREATE TABLE measurements (id INTEGER, amount TEXT, signal REAL, label TEXT, optional TEXT, payload BLOB)")
        connection.executemany("INSERT INTO measurements VALUES (?,?,?,?,?,?)", [
            (9223372036854775807, "1.230000000000000001", 7.5, "观测一", None, b"\x00\x01"),
            (9007199254740993, "-99999999999999999999.123456789012345678", 9.0, "<img src=x onerror=alert(1)>", "", None),
            (3, "0.000000000000000001", 12.0, "观测三", "完成", b""),
        ])
        return ("\n".join(connection.iterdump()) + "\n").encode("utf8")
    finally:
        connection.close()


def options(offset=0, *, dialect="sqlite", table="measurements", columns=None, limit=2):
    return {"dialect": dialect, "table": table_id(dialect, table), "columns": list(range(6)) if columns is None else columns,
            "row_offset": offset, "row_limit": limit}
