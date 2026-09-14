"""Trusted dispatch only: independent private schemas under public v2."""
READERS = frozenset({"sql-dump", "pg-dump", "bson", "redis-rdb", "mysql-sdi", "sst-records"})


def validate_options(reader, kind, options):
    if reader in {"mysql-sdi", "sst-records"}:
        from .physical_database_visualization import options_for
        return options_for(reader, kind, options)
    if reader == "sql-dump":
        from .sql_dump_visualization import validate_sql_dump_options
        return validate_sql_dump_options(kind, options)
    if reader == "pg-dump":
        from .pg_dump_visualization import validate_pg_dump_options
        return validate_pg_dump_options(kind, options)
    if reader in {"bson", "redis-rdb"}:
        from .database_records_visualization import validate_database_records_options
        return validate_database_records_options(kind, options)
    raise ValueError("Unregistered database file reader")


def validate_payload(payload, reader, kind, **kwargs):
    if reader in {"mysql-sdi", "sst-records"}:
        from .physical_database_visualization import validate_physical_payload
        return validate_physical_payload(payload, reader=reader, kind=kind, **kwargs)
    if reader == "sql-dump":
        from .sql_dump_visualization import validate_sql_dump_payload
        return validate_sql_dump_payload(payload, kind=kind, **kwargs)
    if reader == "pg-dump":
        from .pg_dump_visualization import validate_pg_dump_payload
        return validate_pg_dump_payload(payload, kind=kind, **kwargs)
    if reader in {"bson", "redis-rdb"}:
        from .database_records_visualization import validate_database_records_payload
        return validate_database_records_payload(payload, reader=reader, kind=kind, **kwargs)
    raise ValueError("Unregistered database file reader")
