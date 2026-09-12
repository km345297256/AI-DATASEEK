"""Dispatch only approved database file readers; no engine inferred from SQL."""
from .database_table_payload import FORMATS, MAX_INPUT, need, validate_database_table_options


def database_table_preview(data, fmt, kind="tree", options=None):
    options = {} if options is None else options
    validate_database_table_options(kind, options)
    need(type(data) is bytes and 32 <= len(data) <= MAX_INPUT and type(fmt) is str and fmt in FORMATS)
    if FORMATS[fmt] == "duckdb":
        from .duckdb_table_reader import duckdb_table_preview
        return duckdb_table_preview(data, fmt, kind, options)
    if fmt == "dbf":
        from .dbf_table_reader import dbf_table_preview
        return dbf_table_preview(data, fmt, kind, options)
    from .access_table_reader import access_table_preview
    return access_table_preview(data, fmt, kind, options)
